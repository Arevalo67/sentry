import logging

from arroyo.types import Message

from sentry.sentry_metrics.configuration import (
    CLOUDSPANNER_DB,
    POSTGRES_DB,
    MetricsIngestConfiguration,
)
from sentry.sentry_metrics.consumers.indexer.batch import IndexerBatch
from sentry.sentry_metrics.consumers.indexer.common import MessageBatch
from sentry.sentry_metrics.indexer.cloudspanner.cloudspanner import CloudSpannerIndexer
from sentry.sentry_metrics.indexer.postgres.postgres_v2 import PostgresIndexer
from sentry.utils import metrics

logger = logging.getLogger(__name__)

DB_INDEXER_MAP = {CLOUDSPANNER_DB: CloudSpannerIndexer, POSTGRES_DB: PostgresIndexer}


def process_messages(
    config: MetricsIngestConfiguration,
    outer_message: Message[MessageBatch],
) -> MessageBatch:
    """
    We have an outer_message Message() whose payload is a batch of Message() objects.

        Message(
            partition=...,
            offset=...
            timestamp=...
            payload=[Message(...), Message(...), etc]
        )

    The inner messages payloads are KafkaPayload's that have:
        * key
        * headers
        * value

    The value of the message is what we need to parse and then translate
    using the indexer.
    """
    batch = IndexerBatch(config.use_case_id, outer_message)

    org_strings = batch.extract_strings()

    indexer = DB_INDEXER_MAP[config.db_backend]()

    with metrics.timer("metrics_consumer.bulk_record"):
        record_result = indexer.bulk_record(use_case_id=config.use_case_id, org_strings=org_strings)

    mapping = record_result.get_mapped_results()
    bulk_record_meta = record_result.get_fetch_metadata()

    new_messages = batch.reconstruct_messages(mapping, bulk_record_meta)
    return new_messages
