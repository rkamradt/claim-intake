"""Health Lambda — returns 200 with service status.

PR 1 baseline: validates that SAM routing, Lambda execution,
and the LocalStack API Gateway integration are all wired correctly.
Subsequent PRs add the real Lambdas (extract, verify, reconcile).
"""
import json


def lambda_handler(event, context):
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"status": "ok", "service": "claim-intake"}),
    }
