"""Reconcile Lambda — scheduled gap detection and DLQ depth reporting.

Runs on an EventBridge schedule (rate(10 minutes)).

What it does:
  1. Lists all S3 objects under the uploads/ prefix.
  2. For each object older than GAP_THRESHOLD_MINUTES that has no matching
     DynamoDB record (queried via the s3_key-index GSI), records a gap.
  3. Fetches the approximate message count from the ExtractDLQ.
  4. Logs a single JSON report — suitable for Loki ingestion via Promtail.

The gap check catches the failure mode where S3 received an upload but the
extract Lambda never fired (or failed before putting the SQS message), leaving
an orphaned object with no DynamoDB record.

The DLQ depth check catches the failure mode where extract succeeded but verify
kept crashing, exhausting the redrive limit.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET"]
CLAIMS_TABLE = os.environ["CLAIMS_TABLE"]
EXTRACT_DLQ_URL = os.environ["EXTRACT_DLQ_URL"]
GAP_THRESHOLD_MINUTES = int(os.environ.get("GAP_THRESHOLD_MINUTES", "10"))


def _list_old_objects(s3, cutoff: datetime) -> list[dict]:
    """Return all objects under uploads/ last modified before cutoff."""
    paginator = s3.get_paginator("list_objects_v2")
    old_objects = []
    for page in paginator.paginate(Bucket=UPLOAD_BUCKET, Prefix="uploads/"):
        for obj in page.get("Contents", []):
            last_modified = obj["LastModified"]
            # S3 timestamps are timezone-aware (UTC)
            if last_modified < cutoff:
                old_objects.append({
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "last_modified": last_modified.isoformat(),
                })
    return old_objects


def _has_dynamodb_record(table, s3_key: str) -> bool:
    """Query the s3_key-index GSI to check if a record exists for this key."""
    resp = table.query(
        IndexName="s3_key-index",
        KeyConditionExpression=Key("s3_key").eq(s3_key),
        Limit=1,
    )
    return resp["Count"] > 0


def _get_dlq_depth(sqs, queue_url: str) -> int:
    """Return the approximate number of messages in the DLQ."""
    resp = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["ApproximateNumberOfMessages"],
    )
    return int(resp["Attributes"].get("ApproximateNumberOfMessages", 0))


def lambda_handler(event, context):
    """Scan for orphaned S3 objects and report DLQ depth."""
    s3 = boto3.client("s3")
    sqs = boto3.client("sqs")
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(CLAIMS_TABLE)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=GAP_THRESHOLD_MINUTES)

    old_objects = _list_old_objects(s3, cutoff)

    gaps = []
    for obj in old_objects:
        if not _has_dynamodb_record(table, obj["key"]):
            gaps.append(obj)
            logger.warning(
                "Gap detected: s3_key=%s last_modified=%s",
                obj["key"],
                obj["last_modified"],
            )

    dlq_depth = _get_dlq_depth(sqs, EXTRACT_DLQ_URL)

    report = {
        "event": "reconcile_report",
        "timestamp": now.isoformat(),
        "gap_threshold_minutes": GAP_THRESHOLD_MINUTES,
        "s3_objects_checked": len(old_objects),
        "gap_count": len(gaps),
        "gaps": gaps,
        "dlq_depth": dlq_depth,
    }

    # Single structured log line — Loki/Promtail can ingest this directly.
    logger.info(json.dumps(report))

    return report
