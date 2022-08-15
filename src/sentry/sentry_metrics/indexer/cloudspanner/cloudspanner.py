import logging
import re
from datetime import datetime
from http import HTTPStatus
from typing import Any, Mapping, Optional, Sequence, Set

import google.api_core.exceptions
from django.conf import settings
from google.cloud import spanner

from sentry.sentry_metrics.configuration import UseCaseKey
from sentry.sentry_metrics.indexer.base import (
    FetchType,
    KeyCollection,
    KeyResult,
    KeyResults,
    StringIndexer,
)
from sentry.sentry_metrics.indexer.cache import CachingIndexer, StringIndexerCache
from sentry.sentry_metrics.indexer.cloudspanner.cloudspanner_model import (
    SpannerIndexerModel,
    get_column_names,
)
from sentry.sentry_metrics.indexer.id_generator import get_id, reverse_bits
from sentry.sentry_metrics.indexer.ratelimiters import writes_limiter
from sentry.sentry_metrics.indexer.strings import StaticStringIndexer
from sentry.utils import metrics
from sentry.utils.codecs import Codec

logger = logging.getLogger(__name__)

EncodedId = int
DecodedId = int

_INDEXER_DB_METRIC = "sentry_metrics.indexer.cloudspanner"
_INDEXER_DB_INSERT_METRIC = "sentry_metrics.indexer.cloudspanner.insert"
_INDEXER_DB_INSERT_ROWS_METRIC = "sentry_metrics.indexer.cloudspanner.insert_rows"
_DEFAULT_RETENTION_DAYS = 90
_PARTITION_KEY = "cs"

# Captures the details of spanner unique constraint violation.
# The capture group is the combination of organization_id,string,id.
# Example error message is
# Unique index violation on index unique_organization_string_index at index key
# [100000,dBdOCvybLK,6866628476861885949]. It conflicts with row
# [6866628476861885949] in table perfstringindexer_v2.
_UNIQUE_INDEX_VIOLATION_STRING = "Unique index violation"
_UNIQUE_VIOLATION_EXISITING_RE = re.compile(r"\[([^]]+)]")

indexer_cache = StringIndexerCache(
    **settings.SENTRY_STRING_INDEXER_CACHE_OPTIONS, partition_key=_PARTITION_KEY
)


class CloudSpannerRowAlreadyExists(Exception):
    """
    Exception raised when we insert a row that already exists.
    """

    pass


class IdCodec(Codec[DecodedId, EncodedId]):
    """
    Encodes 63 bit IDs generated by the id_generator so that they are well distributed for CloudSpanner.

    Given an ID, this codec does the following:
    - reverses the bits and shifts to the left by one
    - Subtract 2^63 so that that the unsigned 64 bit integer now fits in a signed 64 bit field
    """

    def encode(self, value: DecodedId) -> EncodedId:
        return reverse_bits(value, 64) - 2**63

    def decode(self, value: EncodedId) -> DecodedId:
        return reverse_bits(value + 2**63, 64)


class RawCloudSpannerIndexer(StringIndexer):
    """
    Provides integer IDs for metric names, tag keys and tag values
    and the corresponding reverse lookup.
    """

    def __init__(
        self,
        instance_id: str,
        database_id: str,
        table_name: str,
        unique_organization_string_index: str,
    ) -> None:
        self.instance_id = instance_id
        self.database_id = database_id
        spanner_client = spanner.Client()
        self.instance = spanner_client.instance(self.instance_id)
        self.database = self.instance.database(self.database_id)
        self._table_name = table_name
        self._unique_organization_string_index = unique_organization_string_index
        self.__codec = IdCodec()

    def validate(self) -> None:
        """
        Run a simple query to ensure the database is accessible.
        """
        with self.database.snapshot() as snapshot:
            try:
                snapshot.execute_sql("SELECT 1")
            except ValueError:
                # TODO: What is the correct way to handle connection errors?
                pass

    def _get_db_records(self, db_keys: KeyCollection) -> Sequence[KeyResult]:
        spanner_keyset = []
        for organization_id, string in db_keys.as_tuples():
            spanner_keyset.append([organization_id, string])

        with self.database.snapshot() as snapshot:
            results = snapshot.read(
                table=self._table_name,
                columns=["organization_id", "string", "id"],
                keyset=spanner.KeySet(keys=spanner_keyset),
                index=self._unique_organization_string_index,
            )

        results_list = list(results)
        return [
            KeyResult(org_id=row[0], string=row[1], id=self.__codec.decode(row[2]))
            for row in results_list
        ]

    def _create_db_records(self, db_keys: KeyCollection) -> Sequence[SpannerIndexerModel]:
        """
        Create the set of database records from the provided dk_keys collection.
        """
        rows_to_insert = []
        now = datetime.now()
        for organization_id, string in db_keys.as_tuples():
            new_id = get_id()
            model = SpannerIndexerModel(
                id=self.__codec.encode(new_id),
                decoded_id=new_id,
                string=string,
                organization_id=organization_id,
                date_added=now,
                last_seen=now,
                retention_days=_DEFAULT_RETENTION_DAYS,
            )
            rows_to_insert.append(model)

        return rows_to_insert

    def _insert_db_records(
        self, rows_to_insert: Sequence[SpannerIndexerModel], key_results: KeyResults
    ) -> None:
        """
        Insert a bunch of db_keys records into the database. When there is a
        success of the insert, we update the key_results with the records
        that were inserted. This is different from the postgres implementation
        because postgres implementation uses ignore_conflicts=True. With spanner,
        there is no such option. Once a write is successful, we can assume that
        there were no conflicts and can avoid performing the additional lookup
        to check whether DB records match with what we tried to insert.
        """
        try:
            self._insert_batched_records(rows_to_insert, key_results)
        except CloudSpannerRowAlreadyExists:
            self._insert_individual_records(rows_to_insert, key_results)
        except Exception as e:
            logger.exception(e)
            raise e

    def _insert_batched_records(
        self, rows_to_insert: Sequence[SpannerIndexerModel], key_results: KeyResults
    ) -> None:
        """
        Insert a batch of records in a transaction. This is the preferred
        way of inserting records as it will reduce the number of operations
        which need to be performed in a transaction.

        The transaction could fail if there are collisions with existing
        records. In such a case, none of the records which we are trying to
        batch insert will be inserted. We would need to do individual insert
        using insert_individual_records.
        """

        def insert_batch_transaction_uow(transaction: Any) -> None:
            transaction.insert(
                table=self._table_name, columns=get_column_names(), values=rows_to_insert
            )

        try:
            self.database.run_in_transaction(insert_batch_transaction_uow)
        except google.api_core.exceptions.AlreadyExists as exc:
            if exc.code == HTTPStatus.CONFLICT:
                raise CloudSpannerRowAlreadyExists
        else:
            metrics.incr(
                _INDEXER_DB_INSERT_METRIC,
                tags={"batch": "true"},
            )
            metrics.incr(
                _INDEXER_DB_INSERT_ROWS_METRIC,
                amount=len(rows_to_insert),
                tags={"batch": "true"},
            )
            key_results.add_key_results(
                [
                    KeyResult(
                        org_id=row.organization_id,
                        string=row.string,
                        id=row.decoded_id,
                    )
                    for row in rows_to_insert
                ],
                fetch_type=FetchType.FIRST_SEEN,
            )

    def _insert_individual_records(
        self, rows_to_insert: Sequence[SpannerIndexerModel], key_results: KeyResults
    ) -> None:
        """
        Insert the individual records in a transaction. We would need
        to do this in scenarios where batch insert of the transaction
        failed. Which can happen during collisions when multiple consumers
        try to write records with same organization_id and string.

        During an individual insert, we would get a unique constraint violation
        exception for the existing record. The exception provides the
        existing record id on which the record conflicts. We use this id
        and decode it to populate the key_results.

        decoded_id would be one of the following:
        1. The id we generated for the record.
        2. The id of the existing record on which the record conflicts
        based on
          a. The id received from the exception message when record was
          being inserted.
          b. The id of the existing record by performing a transaction read
          for matching organization_id and string.

        # TODO: Validate whether we should perform transaction read when
        there is a conflict or we using the decoded it from the exception
        is enough.
        """

        def insert_individual_record_uow(transaction: Any) -> None:
            transaction.insert(self._table_name, columns=get_column_names(), values=[row])

        metrics.incr(
            _INDEXER_DB_INSERT_METRIC,
            tags={"batch": "false"},
        )
        for row in rows_to_insert:
            try:
                self.database.run_in_transaction(insert_individual_record_uow)
                metrics.incr(
                    _INDEXER_DB_INSERT_ROWS_METRIC,
                    tags={"batch": "false"},
                )
                key_results.add_key_result(
                    KeyResult(
                        org_id=row.organization_id,
                        string=row.string,
                        id=row.decoded_id,
                    ),
                    fetch_type=FetchType.FIRST_SEEN,
                )
            except google.api_core.exceptions.AlreadyExists as exc:
                if exc.code == HTTPStatus.CONFLICT and exc.message.startswith(
                    _UNIQUE_INDEX_VIOLATION_STRING
                ):
                    regex_match = re.search(_UNIQUE_VIOLATION_EXISITING_RE, str(exc.message))
                    if regex_match:
                        group = regex_match.group(1)
                        _, _, existing_encoded_id = group.split(",")
                        key_results.add_key_result(
                            KeyResult(
                                org_id=row.organization_id,
                                string=row.string,
                                id=self.__codec.decode(int(existing_encoded_id)),
                            ),
                            fetch_type=FetchType.FIRST_SEEN,
                        )
                    else:
                        with self.database.snapshot() as snapshot:
                            result = snapshot.read(
                                table=self._table_name,
                                columns=["id"],
                                keyset=spanner.KeySet(keys=[[row.organization_id, row.string]]),
                                index=self._unique_organization_string_index,
                            )
                        result_list = list(result)
                        existing_encoded_id = result_list[0][0]
                        key_results.add_key_result(
                            KeyResult(
                                org_id=row.organization_id,
                                string=row.string,
                                id=self.__codec.decode(int(existing_encoded_id)),
                            ),
                            fetch_type=FetchType.FIRST_SEEN,
                        )

    def bulk_record(
        self, use_case_id: UseCaseKey, org_strings: Mapping[int, Set[str]]
    ) -> KeyResults:
        db_read_keys = KeyCollection(org_strings)

        db_read_key_results = KeyResults()
        db_read_key_results.add_key_results(
            self._get_db_records(db_read_keys),
            FetchType.DB_READ,
        )
        db_write_keys = db_read_key_results.get_unmapped_keys(db_read_keys)

        metrics.incr(
            _INDEXER_DB_METRIC,
            tags={"db_hit": "true"},
            amount=(db_read_keys.size - db_write_keys.size),
        )
        metrics.incr(
            _INDEXER_DB_METRIC,
            tags={"db_hit": "false"},
            amount=db_write_keys.size,
        )

        if db_write_keys.size == 0:
            return db_read_key_results

        with writes_limiter.check_write_limits(use_case_id, db_write_keys) as writes_limiter_state:
            # After the DB has successfully committed writes, we exit this
            # context manager and consume quotas. If the DB crashes we
            # shouldn't consume quota.
            filtered_db_write_keys = writes_limiter_state.accepted_keys
            del db_write_keys

            rate_limited_key_results = KeyResults()
            for dropped_string in writes_limiter_state.dropped_strings:
                rate_limited_key_results.add_key_result(
                    dropped_string.key_result,
                    fetch_type=dropped_string.fetch_type,
                    fetch_type_ext=dropped_string.fetch_type_ext,
                )

            if filtered_db_write_keys.size == 0:
                return db_read_key_results.merge(rate_limited_key_results)

            new_records = self._create_db_records(filtered_db_write_keys)
            db_write_key_results = KeyResults()
            with metrics.timer("sentry_metrics.indexer.pg_bulk_create"):
                self._insert_db_records(new_records, db_write_key_results)

        return db_read_key_results.merge(db_write_key_results).merge(rate_limited_key_results)

    def record(self, use_case_id: UseCaseKey, org_id: int, string: str) -> Optional[int]:
        """Store a string and return the integer ID generated for it"""
        result = self.bulk_record(use_case_id=use_case_id, org_strings={org_id: {string}})
        return result[org_id][string]

    def resolve(self, use_case_id: UseCaseKey, org_id: int, string: str) -> Optional[int]:
        """Resolve a string to an integer ID"""
        with self.database.snapshot() as snapshot:
            results = snapshot.read(
                table=self._table_name,
                columns=["id"],
                keyset=spanner.KeySet(keys=[[org_id, string]]),
                index=self._unique_organization_string_index,
            )

        results_list = list(results)
        if len(results_list) == 0:
            return None
        else:
            return int(self.__codec.decode(results_list[0][0]))

    def reverse_resolve(self, use_case_id: UseCaseKey, id: int) -> Optional[str]:
        """Resolve an integer ID to a string"""
        with self.database.snapshot() as snapshot:
            results = snapshot.read(
                table=self._table_name,
                columns=["string"],
                keyset=spanner.KeySet(keys=[[id]]),
            )

        results_list = list(results)
        if len(results_list) == 0:
            return None
        else:
            return str(results_list[0][0])


class CloudSpannerIndexer(StaticStringIndexer):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(CachingIndexer(indexer_cache, RawCloudSpannerIndexer(**kwargs)))
