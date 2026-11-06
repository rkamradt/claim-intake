"""Unit tests for the extract Lambda.

Tests cover:
- mock_extract correctly parses well-formed documents
- mock_extract tolerates missing fields (returns None, not an exception)
- make_idempotency_key is stable and unique
- lambda_handler handles malformed AI output gracefully (goes to needs_review path)
"""
import hashlib
import importlib
import json
import os
import sys
import types
import unittest.mock as mock

import pytest

# ── Import the Lambda module with env vars pre-set ───────────────────────────
# Lambda modules read os.environ at import time, so set vars before importing.

@pytest.fixture(autouse=True)
def lambda_env(monkeypatch):
    monkeypatch.setenv("UPLOAD_BUCKET", "claim-intake-uploads")
    monkeypatch.setenv("USE_REAL_AI", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    # Re-import to pick up env changes
    if "lambdas.extract.handler" in sys.modules:
        del sys.modules["lambdas.extract.handler"]


def get_handler():
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[2]))
    import importlib
    spec = importlib.util.spec_from_file_location(
        "handler",
        __import__("pathlib").Path(__file__).parents[2] / "lambdas/extract/handler.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── make_idempotency_key ──────────────────────────────────────────────────────

class TestIdempotencyKey:
    def setup_method(self):
        self.h = get_handler()

    def test_stable_for_same_inputs(self):
        key1 = self.h.make_idempotency_key("uploads/abc/claim.txt", '"etag123"')
        key2 = self.h.make_idempotency_key("uploads/abc/claim.txt", '"etag123"')
        assert key1 == key2

    def test_different_key_for_different_s3_key(self):
        k1 = self.h.make_idempotency_key("uploads/aaa/claim.txt", '"etag"')
        k2 = self.h.make_idempotency_key("uploads/bbb/claim.txt", '"etag"')
        assert k1 != k2

    def test_different_key_for_different_etag(self):
        k1 = self.h.make_idempotency_key("uploads/abc/claim.txt", '"etag1"')
        k2 = self.h.make_idempotency_key("uploads/abc/claim.txt", '"etag2"')
        assert k1 != k2

    def test_strips_etag_quotes(self):
        # S3 wraps ETag in double-quotes; both forms should produce same key
        k1 = self.h.make_idempotency_key("key", '"abc"')
        k2 = self.h.make_idempotency_key("key", "abc")
        assert k1 == k2

    def test_output_is_32_hex_chars(self):
        key = self.h.make_idempotency_key("key", "etag")
        assert len(key) == 32
        assert all(c in "0123456789abcdef" for c in key)


# ── mock_extract ──────────────────────────────────────────────────────────────

CLEAN_DOC = """\
INSURANCE CLAIM FORM
Date: 2024-03-15
Claimant: Jane Smith
LINE ITEMS:
1. Emergency room visit: $850.00
2. X-ray imaging: $320.00
3. Prescription medication: $45.50
TOTAL AMOUNT: $1215.50
"""

MISSING_CLAIMANT_DOC = """\
INSURANCE CLAIM FORM
Date: 2024-03-12
LINE ITEMS:
1. Dental cleaning: $120.00
2. Dental X-ray: $80.00
TOTAL AMOUNT: $200.00
"""

BAD_TOTAL_DOC = """\
INSURANCE CLAIM FORM
Date: 2024-03-10
Claimant: Bob Johnson
LINE ITEMS:
1. Specialist consultation: $200.00
2. Lab work: $175.00
3. Physical therapy session: $95.00
TOTAL AMOUNT: $500.00
"""


class TestMockExtract:
    def setup_method(self):
        self.h = get_handler()

    def test_clean_claim(self):
        result = self.h.mock_extract(CLEAN_DOC)
        assert result["claimant_name"] == "Jane Smith"
        assert result["date"] == "2024-03-15"
        assert result["total_amount"] == 1215.50
        assert len(result["line_items"]) == 3
        assert result["line_items"][0] == {"description": "Emergency room visit", "amount": 850.00}

    def test_missing_claimant_returns_none(self):
        result = self.h.mock_extract(MISSING_CLAIMANT_DOC)
        assert result["claimant_name"] is None
        assert result["date"] == "2024-03-12"
        assert result["total_amount"] == 200.00

    def test_bad_total_parsed_as_stated(self):
        # The extractor reports what the document says; verify Lambda catches the mismatch
        result = self.h.mock_extract(BAD_TOTAL_DOC)
        assert result["total_amount"] == 500.00
        actual_sum = sum(i["amount"] for i in result["line_items"])
        assert actual_sum == 470.00  # doesn't match total — verify will flag this

    def test_empty_document_returns_nones(self):
        result = self.h.mock_extract("")
        assert result["claimant_name"] is None
        assert result["date"] is None
        assert result["total_amount"] is None
        assert result["line_items"] == []


# ── lambda_handler: malformed AI output ──────────────────────────────────────

class TestHandlerMalformedOutput:
    def setup_method(self):
        self.h = get_handler()

    def _make_s3_event(self, key="uploads/test/claim.txt", etag="abc123"):
        return {
            "Records": [{
                "s3": {
                    "bucket": {"name": "claim-intake-uploads"},
                    "object": {"key": key, "eTag": etag},
                }
            }]
        }

    def test_malformed_real_ai_json_goes_to_error_field(self):
        """If AI returns invalid JSON, handler catches it and sets _extraction_error."""
        h = self.h

        def bad_real_extract(text):
            raise json.JSONDecodeError("bad json", "", 0)

        with mock.patch.object(h, "real_extract", bad_real_extract), \
             mock.patch.object(h, "USE_REAL_AI", True), \
             mock.patch("boto3.client") as mock_boto:

            mock_s3 = mock.MagicMock()
            mock_s3.get_object.return_value = {
                "Body": mock.MagicMock(read=lambda: b"some content")
            }
            mock_boto.return_value = mock_s3

            results = h.lambda_handler(self._make_s3_event(), {})
            assert len(results) == 1
            assert "_extraction_error" in results[0]
            assert results[0]["claimant_name"] is None

    def test_result_always_contains_idempotency_key(self):
        """Every result — even error cases — must carry an idempotency key."""
        h = self.h
        with mock.patch("boto3.client") as mock_boto:
            mock_s3 = mock.MagicMock()
            mock_s3.get_object.return_value = {
                "Body": mock.MagicMock(read=lambda: CLEAN_DOC.encode())
            }
            mock_boto.return_value = mock_s3

            results = h.lambda_handler(self._make_s3_event(), {})
            assert "idempotency_key" in results[0]
            assert len(results[0]["idempotency_key"]) == 32
