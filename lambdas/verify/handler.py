"""Verify Lambda — consumes ExtractQueue, stores claims in DynamoDB.

Verification rules (all must pass for status=accepted):
  1. Required fields present: claimant_name, date, total_amount, line_items
  2. Line items sum to total_amount (to the cent)
  3. Date parses as ISO-8601 and is not in the future
  4. Every line-item amount appears in the source document text

Idempotency:
  DynamoDB write uses ConditionExpression=attribute_not_exists(pk).
  If the same SQS message is delivered twice (at-least-once delivery),
  the second write raises ConditionalCheckFailedException and is ignored.
  One record is created regardless of how many times the event fires.

PR 4 adds:
  - DLQ on ExtractQueue (after 3 failed receives)
  - NeedsReviewQueue: failed claims are also published there
  - ReconcileFunction: scheduled scan for gaps + DLQ depth reporting
"""
import json
import logging
import os
from datetime import date, datetime

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CLAIMS_TABLE = os.environ["CLAIMS_TABLE"]
REQUIRED_FIELDS = ["claimant_name", "date", "total_amount", "line_items"]


# ── Verification rules ────────────────────────────────────────────────────────

def verify(extracted: dict, source_text: str) -> tuple[bool, list[str]]:
    """Run all code checks on the extracted claim data.

    Returns (accepted: bool, reasons: list[str]).
    reasons is empty when accepted=True.
    """
    reasons = []

    # Rule 1: required fields present
    for field in REQUIRED_FIELDS:
        if extracted.get(field) is None:
            reasons.append(f"missing_{field}")

    # Rule 2: line items sum to total (to the cent)
    total = extracted.get("total_amount")
    items = extracted.get("line_items") or []
    if total is not None and items is not None:
        items_sum = round(sum(item["amount"] for item in items), 2)
        if round(total, 2) != items_sum:
            reasons.append(
                f"total_mismatch: line items sum to {items_sum:.2f}, "
                f"claimed total is {round(total, 2):.2f}"
            )

    # Rule 3: date parses and is not in the future
    raw_date = extracted.get("date")
    if raw_date:
        try:
            claim_date = date.fromisoformat(raw_date)
            if claim_date > date.today():
                reasons.append(f"date_in_future: {raw_date}")
        except ValueError:
            reasons.append(f"date_invalid: {raw_date!r}")

    # Rule 4: every line-item amount appears in the source text
    if items and source_text:
        for item in items:
            # Format as "850.00" — present in source as "$850.00" or "850.00"
            amount_str = f"{item['amount']:.2f}"
            if amount_str not in source_text:
                reasons.append(f"amount_not_in_source: {amount_str}")

    return len(reasons) == 0, reasons


# ── DynamoDB store ────────────────────────────────────────────────────────────

def store_claim(extracted: dict, status: str, reasons: list[str]) -> bool:
    """Write a claim record to DynamoDB.

    Uses a conditional put so duplicate SQS deliveries never create a second
    record. Returns True if the record was written, False if it already existed.

    The condition attribute_not_exists(pk) succeeds only when no item with this
    pk exists yet. On a retry (same idempotency key), DynamoDB raises
    ConditionalCheckFailedException — we catch it and treat it as success.
    """
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(CLAIMS_TABLE)

    item = {
        "pk": extracted["idempotency_key"],
        "s3_key": extracted["s3_key"],
        "status": status,
        "claimant_name": extracted.get("claimant_name"),
        "date": extracted.get("date"),
        "total_amount": str(extracted.get("total_amount") or ""),
        "line_items": json.dumps(extracted.get("line_items") or []),
        "created_at": datetime.utcnow().isoformat(),
    }
    if reasons:
        item["review_reasons"] = reasons

    try:
        table.put_item(
            Item=item,
            ConditionExpression=Attr("pk").not_exists(),
        )
        logger.info("Stored claim pk=%s status=%s", item["pk"], status)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info("Duplicate delivery — pk=%s already exists, skipping", item["pk"])
            return False
        raise


# ── Handler ───────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    """Process a batch of SQS messages.

    Uses ReportBatchItemFailures: returns the IDs of messages that raised
    exceptions so SQS retries only those. A claim that fails verification
    is NOT a processing error — it is stored with status=needs_review and
    its message ID is NOT in the failure list.
    """
    failed_ids = []

    for record in event.get("Records", []):
        message_id = record["messageId"]
        try:
            extracted = json.loads(record["body"])
            source_text = extracted.get("source_text", "")

            accepted, reasons = verify(extracted, source_text)
            status = "accepted" if accepted else "needs_review"

            store_claim(extracted, status, reasons)

            if not accepted:
                logger.info(
                    "Claim pk=%s needs_review: %s",
                    extracted.get("idempotency_key"),
                    reasons,
                )
                # PR 4: publish to NeedsReviewQueue

        except Exception as exc:
            # Unexpected error (bad JSON, DynamoDB unavailable, etc.)
            # Report as a batch item failure so SQS retries the message.
            logger.error("Error processing message %s: %s", message_id, exc, exc_info=True)
            failed_ids.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failed_ids}
