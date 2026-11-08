"""Extract Lambda — triggered by S3 ObjectCreated

Reads the uploaded claim document, extracts structured fields (via Anthropic
or a deterministic mock), generates an idempotency key, and logs the result.

PR 3 adds: puts the result to SQS for the verify Lambda to consume.

Extracted schema:
  {
    "claimant_name": str | None,
    "date": str | None,          # ISO-8601 YYYY-MM-DD
    "total_amount": float | None,
    "line_items": [{"description": str, "amount": float}],
    "idempotency_key": str,      # sha256(s3_key:etag)[:32]
    "s3_key": str,
    "source_text": str           # kept for verify Lambda's amount-in-source check
  }
"""
import hashlib
import json
import logging
import os
import re

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

USE_REAL_AI = os.environ.get("USE_REAL_AI", "false").lower() == "true"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
EXTRACT_QUEUE_URL = os.environ.get("EXTRACT_QUEUE_URL", "")


# ── Idempotency key ───────────────────────────────────────────────────────────

def make_idempotency_key(s3_key: str, etag: str) -> str:
    """Derive a stable record key from the S3 object key and its ETag.

    Using both prevents collisions if the same filename is re-uploaded with
    different content, while ensuring a true re-upload of identical bytes maps
    to the same key (idempotent retry).  The ETag is an MD5 of the object body
    for non-multipart uploads; we strip quotes S3 wraps around it.
    """
    raw = f"{s3_key}:{etag.strip(chr(34))}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── Mock extractor ────────────────────────────────────────────────────────────

def mock_extract(text: str) -> dict:
    """Parse a structured plain-text claim document without calling AI.

    The sample documents use a fixed format so a few regexes are sufficient.
    This keeps CI free, fast, and deterministic.
    """
    claimant_match = re.search(r"Claimant:\s*(.+)", text)
    date_match = re.search(r"Date:\s*(\d{4}-\d{2}-\d{2})", text)
    total_match = re.search(r"TOTAL AMOUNT:\s*\$(\d+(?:\.\d{2})?)", text)

    line_items = [
        {"description": m.group(1).strip(), "amount": float(m.group(2))}
        for m in re.finditer(r"\d+\.\s+(.+?):\s+\$(\d+(?:\.\d{2})?)", text)
    ]

    return {
        "claimant_name": claimant_match.group(1).strip() if claimant_match else None,
        "date": date_match.group(1) if date_match else None,
        "total_amount": float(total_match.group(1)) if total_match else None,
        "line_items": line_items,
    }


# ── Real AI extractor ─────────────────────────────────────────────────────────

def real_extract(text: str) -> dict:
    """Call Anthropic to extract structured fields from the document."""
    import anthropic  # imported lazily — not installed in mock/CI mode

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = (
        "Extract the following fields from this insurance claim document and "
        "return valid JSON only — no markdown, no explanation:\n"
        '{"claimant_name": string|null, "date": "YYYY-MM-DD"|null, '
        '"total_amount": number|null, '
        '"line_items": [{"description": string, "amount": number}]}\n\n'
        f"Document:\n{text}"
    )
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    return json.loads(raw)


# ── Handler ───────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        s3_key = record["s3"]["object"]["key"]
        etag = record["s3"]["object"].get("eTag", "")

        s3 = boto3.client("s3")
        obj = s3.get_object(Bucket=bucket, Key=s3_key)
        text = obj["Body"].read().decode("utf-8", errors="replace")

        try:
            fields = real_extract(text) if USE_REAL_AI else mock_extract(text)
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            # Malformed AI output — still emit a record so verify can mark it
            # needs_review rather than silently dropping it.
            logger.warning("Extraction produced invalid output: %s", exc)
            fields = {
                "claimant_name": None,
                "date": None,
                "total_amount": None,
                "line_items": [],
                "_extraction_error": str(exc),
            }

        result = {
            **fields,
            "idempotency_key": make_idempotency_key(s3_key, etag),
            "s3_key": s3_key,
            "source_text": text,
        }

        logger.info("Extracted: %s", json.dumps({k: v for k, v in result.items()
                                                   if k != "source_text"}))
        sqs = boto3.client("sqs")
        sqs.send_message(QueueUrl=EXTRACT_QUEUE_URL, MessageBody=json.dumps(result))
        results.append(result)

    return results
