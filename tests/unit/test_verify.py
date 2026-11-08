"""Unit tests for the verify Lambda's business rules.

Tests are grouped by rule so failures point directly at the broken check.
No AWS resources are needed — verify() and store_claim() are tested in isolation.
"""
import importlib.util
import json
import sys
import unittest.mock as mock
from pathlib import Path
from datetime import date, timedelta

import pytest

# ── Import helper ─────────────────────────────────────────────────────────────

def get_handler(monkeypatch):
    monkeypatch.setenv("CLAIMS_TABLE", "claims")
    spec = importlib.util.spec_from_file_location(
        "verify_handler",
        Path(__file__).parents[2] / "lambdas/verify/handler.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CLEAN_SOURCE = """\
Date: 2024-03-15
Claimant: Jane Smith
1. Emergency room visit: $850.00
2. X-ray imaging: $320.00
3. Prescription medication: $45.50
TOTAL AMOUNT: $1215.50
"""

CLEAN_EXTRACTED = {
    "claimant_name": "Jane Smith",
    "date": "2024-03-15",
    "total_amount": 1215.50,
    "line_items": [
        {"description": "Emergency room visit", "amount": 850.00},
        {"description": "X-ray imaging", "amount": 320.00},
        {"description": "Prescription medication", "amount": 45.50},
    ],
}


# ── Rule 1: required fields ───────────────────────────────────────────────────

class TestRequiredFields:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.h = get_handler(monkeypatch)

    def test_clean_claim_passes(self):
        ok, reasons = self.h.verify(CLEAN_EXTRACTED, CLEAN_SOURCE)
        assert ok is True
        assert reasons == []

    def test_missing_claimant_name(self):
        data = {**CLEAN_EXTRACTED, "claimant_name": None}
        ok, reasons = self.h.verify(data, CLEAN_SOURCE)
        assert ok is False
        assert any("missing_claimant_name" in r for r in reasons)

    def test_missing_date(self):
        data = {**CLEAN_EXTRACTED, "date": None}
        ok, reasons = self.h.verify(data, CLEAN_SOURCE)
        assert ok is False
        assert any("missing_date" in r for r in reasons)

    def test_missing_total_amount(self):
        data = {**CLEAN_EXTRACTED, "total_amount": None}
        ok, reasons = self.h.verify(data, CLEAN_SOURCE)
        assert ok is False
        assert any("missing_total_amount" in r for r in reasons)

    def test_missing_line_items(self):
        data = {**CLEAN_EXTRACTED, "line_items": None}
        ok, reasons = self.h.verify(data, CLEAN_SOURCE)
        assert ok is False
        assert any("missing_line_items" in r for r in reasons)

    def test_multiple_missing_fields_all_reported(self):
        data = {**CLEAN_EXTRACTED, "claimant_name": None, "date": None}
        ok, reasons = self.h.verify(data, CLEAN_SOURCE)
        assert ok is False
        assert len([r for r in reasons if r.startswith("missing_")]) == 2


# ── Rule 2: line items sum to total ──────────────────────────────────────────

class TestTotalMismatch:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.h = get_handler(monkeypatch)

    def test_matching_total_passes(self):
        ok, reasons = self.h.verify(CLEAN_EXTRACTED, CLEAN_SOURCE)
        assert ok is True

    def test_overstated_total_flagged(self):
        data = {**CLEAN_EXTRACTED, "total_amount": 500.00,
                "line_items": [
                    {"description": "Consult", "amount": 200.00},
                    {"description": "Lab", "amount": 175.00},
                    {"description": "PT", "amount": 95.00},
                ]}
        source = "Consult: $200.00\nLab: $175.00\nPT: $95.00\nTOTAL: $500.00"
        ok, reasons = self.h.verify(data, source)
        assert ok is False
        assert any("total_mismatch" in r for r in reasons)
        assert "470.00" in " ".join(reasons)

    def test_cent_precision(self):
        # 3 × $0.10 = $0.30 exactly — floating-point naive sum gives 0.30000000000000004
        data = {**CLEAN_EXTRACTED,
                "total_amount": 0.30,
                "line_items": [{"description": f"item{i}", "amount": 0.10} for i in range(3)]}
        source = "0.10 0.10 0.10 TOTAL 0.30"
        ok, reasons = self.h.verify(data, source)
        assert ok is True, f"Expected pass but got: {reasons}"

    def test_empty_line_items_with_nonzero_total(self):
        data = {**CLEAN_EXTRACTED, "total_amount": 100.0, "line_items": []}
        ok, reasons = self.h.verify(data, CLEAN_SOURCE)
        assert ok is False
        assert any("total_mismatch" in r for r in reasons)


# ── Rule 3: date validity ─────────────────────────────────────────────────────

class TestDateValidity:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.h = get_handler(monkeypatch)

    def test_past_date_passes(self):
        ok, reasons = self.h.verify(CLEAN_EXTRACTED, CLEAN_SOURCE)
        assert ok is True

    def test_future_date_flagged(self):
        future = (date.today() + timedelta(days=30)).isoformat()
        data = {**CLEAN_EXTRACTED, "date": future}
        ok, reasons = self.h.verify(data, CLEAN_SOURCE)
        assert ok is False
        assert any("date_in_future" in r for r in reasons)

    def test_invalid_date_format_flagged(self):
        data = {**CLEAN_EXTRACTED, "date": "15/03/2024"}
        ok, reasons = self.h.verify(data, CLEAN_SOURCE)
        assert ok is False
        assert any("date_invalid" in r for r in reasons)

    def test_today_passes(self):
        data = {**CLEAN_EXTRACTED, "date": date.today().isoformat()}
        ok, reasons = self.h.verify(data, CLEAN_SOURCE)
        # today is not in the future
        assert not any("date_in_future" in r for r in reasons)


# ── Rule 4: amounts in source text ───────────────────────────────────────────

class TestAmountsInSource:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.h = get_handler(monkeypatch)

    def test_all_amounts_present(self):
        ok, reasons = self.h.verify(CLEAN_EXTRACTED, CLEAN_SOURCE)
        assert ok is True

    def test_fabricated_amount_flagged(self):
        data = {**CLEAN_EXTRACTED,
                "line_items": [{"description": "Invented charge", "amount": 999.99}],
                "total_amount": 999.99}
        ok, reasons = self.h.verify(data, CLEAN_SOURCE)
        assert ok is False
        assert any("amount_not_in_source" in r and "999.99" in r for r in reasons)

    def test_amount_present_as_dollar_string(self):
        # Source has "$850.00" — verify should find "850.00" within it
        source = "Emergency: $850.00 TOTAL $850.00"
        data = {**CLEAN_EXTRACTED,
                "line_items": [{"description": "Emergency", "amount": 850.00}],
                "total_amount": 850.00}
        ok, reasons = self.h.verify(data, source)
        assert not any("amount_not_in_source" in r for r in reasons)


# ── Idempotency (store_claim) ─────────────────────────────────────────────────

class TestIdempotency:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.h = get_handler(monkeypatch)

    def _base_extracted(self):
        return {
            **CLEAN_EXTRACTED,
            "idempotency_key": "abc123def456abc123def456abc12345",
            "s3_key": "uploads/uuid/clean_claim.txt",
            "source_text": CLEAN_SOURCE,
        }

    def test_first_write_returns_true(self):
        with mock.patch("boto3.resource") as mock_res:
            mock_table = mock.MagicMock()
            mock_res.return_value.Table.return_value = mock_table
            mock_table.put_item.return_value = {}

            result = self.h.store_claim(self._base_extracted(), "accepted", [])
            assert result is True
            mock_table.put_item.assert_called_once()

    def test_duplicate_delivery_returns_false(self):
        """ConditionalCheckFailedException → silent skip, returns False."""
        from botocore.exceptions import ClientError
        with mock.patch("boto3.resource") as mock_res:
            mock_table = mock.MagicMock()
            mock_res.return_value.Table.return_value = mock_table
            mock_table.put_item.side_effect = ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
                "PutItem",
            )
            result = self.h.store_claim(self._base_extracted(), "accepted", [])
            assert result is False

    def test_conditional_expression_used(self):
        """put_item must include ConditionExpression to enforce idempotency."""
        with mock.patch("boto3.resource") as mock_res:
            mock_table = mock.MagicMock()
            mock_res.return_value.Table.return_value = mock_table
            mock_table.put_item.return_value = {}

            self.h.store_claim(self._base_extracted(), "accepted", [])
            call_kwargs = mock_table.put_item.call_args.kwargs
            assert "ConditionExpression" in call_kwargs
