"""Claim Lambda — GET /claims/{id}

Returns a stored claim record from DynamoDB. The {id} path parameter is the
32-char idempotency key (sha256(s3_key:etag)[:32]) computed by the extract
Lambda and used as the DynamoDB partition key.

The idempotency key is returned by the upload endpoint after the client PUTs
the document to S3 and reads the ETag from the response header:
  key = sha256(s3_key + ":" + etag.strip('"'))[:32]
"""
import json
import os

import boto3
from botocore.exceptions import ClientError

CLAIMS_TABLE = os.environ["CLAIMS_TABLE"]


def lambda_handler(event, context):
    claim_id = (event.get("pathParameters") or {}).get("id", "")
    if not claim_id:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "missing_id"}),
        }

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(CLAIMS_TABLE)

    try:
        resp = table.get_item(Key={"pk": claim_id})
    except ClientError as exc:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(exc)}),
        }

    item = resp.get("Item")
    if not item:
        return {
            "statusCode": 404,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "not_found", "id": claim_id}),
        }

    # Deserialise stored JSON strings back to objects
    if "line_items" in item:
        try:
            item["line_items"] = json.loads(item["line_items"])
        except (json.JSONDecodeError, TypeError):
            pass
    if "total_amount" in item:
        try:
            item["total_amount"] = float(item["total_amount"])
        except (ValueError, TypeError):
            pass

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(item),
    }
