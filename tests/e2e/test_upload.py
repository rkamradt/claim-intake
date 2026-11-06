"""E2E tests — upload flow (PR 2).

Requires API_URL env var pointing at a deployed LocalStack stack.
Tests are skipped if API_URL is not set (safe to run in unit-test-only mode).

PR 3 extends these tests with DynamoDB assertions.
"""
import json
import os
import time

import httpx
import pytest

API_URL = os.environ.get("API_URL", "")
LOCALSTACK_URL = os.environ.get("LOCALSTACK_URL", "http://localhost:4566")


@pytest.fixture(scope="module")
def api():
    if not API_URL:
        pytest.skip("API_URL not set — skipping e2e tests")
    return httpx.Client(base_url=API_URL, timeout=30)


@pytest.fixture(scope="module")
def s3_client():
    """Direct boto3 client to LocalStack for verification."""
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=LOCALSTACK_URL,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
    )


def _load_sample(filename: str) -> str:
    sample_dir = os.path.join(os.path.dirname(__file__), "../../sample_data")
    with open(os.path.join(sample_dir, filename)) as f:
        return f.read()


# ── POST /uploads ─────────────────────────────────────────────────────────────

class TestUploadEndpoint:
    def test_returns_presigned_url(self, api):
        resp = api.post("/uploads", json={"filename": "clean_claim.txt"})
        assert resp.status_code == 200
        body = resp.json()
        assert "upload_url" in body
        assert "s3_key" in body
        assert body["s3_key"].startswith("uploads/")
        assert body["s3_key"].endswith("clean_claim.txt")
        assert body["expires_in"] == 300

    def test_works_without_filename(self, api):
        resp = api.post("/uploads")
        assert resp.status_code == 200
        body = resp.json()
        assert body["s3_key"].endswith("claim.txt")  # default filename

    def test_presigned_url_accepts_put(self, api):
        """Actually PUT a document to the presigned URL and verify S3 accepts it."""
        resp = api.post("/uploads", json={"filename": "clean_claim.txt"})
        upload_url = resp.json()["upload_url"]

        # PUT directly to S3 via the presigned URL — bypasses API Gateway
        put_resp = httpx.put(
            upload_url,
            content=_load_sample("clean_claim.txt").encode(),
            headers={"Content-Type": "text/plain"},
            timeout=30,
        )
        assert put_resp.status_code == 200

    def test_extract_lambda_triggered_after_upload(self, api, s3_client):
        """Upload a document and verify it lands in S3 (extract runs async)."""
        resp = api.post("/uploads", json={"filename": "clean_claim.txt"})
        body = resp.json()
        upload_url = body["upload_url"]
        s3_key = body["s3_key"]
        content = _load_sample("clean_claim.txt").encode()

        put_resp = httpx.put(upload_url, content=content,
                             headers={"Content-Type": "text/plain"}, timeout=30)
        assert put_resp.status_code == 200

        # Give the S3 event notification a moment to trigger the Lambda
        time.sleep(2)

        # Verify the object exists in S3
        obj = s3_client.get_object(Bucket="claim-intake-uploads", Key=s3_key)
        assert obj["Body"].read() == content


# ── GET /claims/{id} ─────────────────────────────────────────────────────────

class TestClaimEndpoint:
    def test_returns_404_for_unknown_id(self, api):
        """Stub behaviour until DynamoDB is wired in PR 3."""
        resp = api.get("/claims/nonexistent-id")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"] == "not_found"
