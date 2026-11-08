"""Unit tests for the reconcile Lambda.

Tests cover:
  - Gap detection: objects older than threshold with no DynamoDB record
  - No-gap case: objects with matching DynamoDB records are not flagged
  - Recent objects within the threshold window are ignored
  - DLQ depth reporting
  - Report structure
"""
import importlib.util
import sys
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ── Import helper ─────────────────────────────────────────────────────────────

MODULE_NAME = "reconcile_handler"


def get_handler(monkeypatch):
    monkeypatch.setenv("UPLOAD_BUCKET", "claim-intake-uploads")
    monkeypatch.setenv("CLAIMS_TABLE", "claims")
    monkeypatch.setenv(
        "EXTRACT_DLQ_URL",
        "http://localhost:4566/000000000000/claim-intake-extract-dlq",
    )
    monkeypatch.setenv("GAP_THRESHOLD_MINUTES", "10")
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        Path(__file__).parents[2] / "lambdas/reconcile/handler.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = mod  # required for mock.patch to resolve the name
    spec.loader.exec_module(mod)
    return mod


# ── Shared test time constants ────────────────────────────────────────────────

NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(minutes=20)      # older than 10-min threshold → checked
RECENT = NOW - timedelta(minutes=5)   # within threshold → ignored


# ── Helpers ───────────────────────────────────────────────────────────────────

def _s3_obj(key: str, last_modified: datetime, size: int = 100) -> dict:
    return {"Key": key, "Size": size, "LastModified": last_modified}


def _make_mocks(s3_contents, dlq_depth=0, dynamo_count=0):
    """Return (s3_mock, sqs_mock, dynamodb_mock) pre-configured."""
    s3_mock = mock.MagicMock()
    paginator = mock.MagicMock()
    paginator.paginate.return_value = [{"Contents": s3_contents}]
    s3_mock.get_paginator.return_value = paginator

    sqs_mock = mock.MagicMock()
    sqs_mock.get_queue_attributes.return_value = {
        "Attributes": {"ApproximateNumberOfMessages": str(dlq_depth)}
    }

    table_mock = mock.MagicMock()
    table_mock.query.return_value = {"Count": dynamo_count}
    dynamodb_mock = mock.MagicMock()
    dynamodb_mock.Table.return_value = table_mock

    return s3_mock, sqs_mock, dynamodb_mock, table_mock


def _run(mod, s3_mock, sqs_mock, dynamodb_mock):
    """Invoke lambda_handler with boto3 and datetime patched."""
    with mock.patch(
        "boto3.client",
        side_effect=lambda svc: s3_mock if svc == "s3" else sqs_mock,
    ):
        with mock.patch("boto3.resource", return_value=dynamodb_mock):
            with mock.patch(f"{MODULE_NAME}.datetime") as dt_mock:
                dt_mock.now.return_value = NOW
                return mod.lambda_handler({}, None)


# ── Gap detection ─────────────────────────────────────────────────────────────

class TestGapDetection:
    def test_old_object_with_no_record_is_a_gap(self, monkeypatch):
        mod = get_handler(monkeypatch)
        s3_mock, sqs_mock, dynamodb_mock, _ = _make_mocks(
            [_s3_obj("uploads/abc/claim.txt", OLD)],
            dynamo_count=0,  # no DynamoDB record
        )
        report = _run(mod, s3_mock, sqs_mock, dynamodb_mock)

        assert report["gap_count"] == 1
        assert report["gaps"][0]["key"] == "uploads/abc/claim.txt"

    def test_old_object_with_existing_record_is_not_a_gap(self, monkeypatch):
        mod = get_handler(monkeypatch)
        s3_mock, sqs_mock, dynamodb_mock, _ = _make_mocks(
            [_s3_obj("uploads/abc/claim.txt", OLD)],
            dynamo_count=1,  # record exists
        )
        report = _run(mod, s3_mock, sqs_mock, dynamodb_mock)

        assert report["gap_count"] == 0
        assert report["gaps"] == []

    def test_recent_objects_are_ignored(self, monkeypatch):
        """Objects modified within the threshold window should not be checked."""
        mod = get_handler(monkeypatch)
        s3_mock, sqs_mock, dynamodb_mock, table_mock = _make_mocks(
            [_s3_obj("uploads/abc/claim.txt", RECENT)],
        )
        report = _run(mod, s3_mock, sqs_mock, dynamodb_mock)

        # Recent objects skipped — DynamoDB never queried
        table_mock.query.assert_not_called()
        assert report["s3_objects_checked"] == 0
        assert report["gap_count"] == 0

    def test_multiple_objects_mixed(self, monkeypatch):
        mod = get_handler(monkeypatch)
        s3_mock, sqs_mock, dynamodb_mock, table_mock = _make_mocks(
            [
                _s3_obj("uploads/a/x.txt", OLD),   # old, no record → gap
                _s3_obj("uploads/b/y.txt", OLD),   # old, has record → no gap
                _s3_obj("uploads/c/z.txt", RECENT),  # recent → skipped
            ],
        )
        # First query returns no record; second returns a record
        table_mock.query.side_effect = [{"Count": 0}, {"Count": 1}]

        report = _run(mod, s3_mock, sqs_mock, dynamodb_mock)

        assert report["s3_objects_checked"] == 2  # only the two old objects
        assert report["gap_count"] == 1
        assert report["gaps"][0]["key"] == "uploads/a/x.txt"


# ── DLQ depth ─────────────────────────────────────────────────────────────────

class TestDLQDepth:
    def test_dlq_depth_reported(self, monkeypatch):
        mod = get_handler(monkeypatch)
        s3_mock, sqs_mock, dynamodb_mock, _ = _make_mocks([], dlq_depth=7)
        report = _run(mod, s3_mock, sqs_mock, dynamodb_mock)

        assert report["dlq_depth"] == 7

    def test_empty_dlq(self, monkeypatch):
        mod = get_handler(monkeypatch)
        s3_mock, sqs_mock, dynamodb_mock, _ = _make_mocks([], dlq_depth=0)
        report = _run(mod, s3_mock, sqs_mock, dynamodb_mock)

        assert report["dlq_depth"] == 0


# ── Report structure ──────────────────────────────────────────────────────────

class TestReportStructure:
    def test_report_has_required_keys(self, monkeypatch):
        mod = get_handler(monkeypatch)
        s3_mock, sqs_mock, dynamodb_mock, _ = _make_mocks([])
        report = _run(mod, s3_mock, sqs_mock, dynamodb_mock)

        required_keys = {
            "event", "timestamp", "gap_threshold_minutes",
            "s3_objects_checked", "gap_count", "gaps", "dlq_depth",
        }
        assert required_keys.issubset(report.keys())
        assert report["event"] == "reconcile_report"
        assert report["gap_threshold_minutes"] == 10
