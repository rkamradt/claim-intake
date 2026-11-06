"""Claim Lambda — GET /claims/{id}

Stub in PR 2. Returns 404 until DynamoDB is wired in PR 3.
"""
import json


def lambda_handler(event, context):
    claim_id = event.get("pathParameters", {}).get("id", "unknown")
    return {
        "statusCode": 404,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {"error": "not_found", "id": claim_id, "note": "DynamoDB added in PR 3"}
        ),
    }
