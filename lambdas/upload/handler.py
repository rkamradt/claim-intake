"""Upload Lambda — POST /uploads

Returns a presigned S3 PUT URL. The client uploads the document directly
to S3 without routing the bytes through API Gateway (10 MB limit) or Lambda.

Request body (JSON, optional):
  { "filename": "claim.txt" }

Response:
  {
    "upload_url": "https://...",   # PUT to this URL with the document bytes
    "s3_key": "uploads/<uuid>/claim.txt",
    "expires_in": 300
  }
"""
import json
import os
import uuid

import boto3


BUCKET = os.environ["UPLOAD_BUCKET"]
EXPIRES = 300  # seconds


def lambda_handler(event, context):
    body = {}
    if event.get("body"):
        try:
            body = json.loads(event["body"])
        except (json.JSONDecodeError, TypeError):
            pass

    filename = body.get("filename", "claim.txt")
    s3_key = f"uploads/{uuid.uuid4()}/{filename}"

    s3 = boto3.client("s3")
    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": s3_key},
        ExpiresIn=EXPIRES,
    )

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {"upload_url": upload_url, "s3_key": s3_key, "expires_in": EXPIRES}
        ),
    }
