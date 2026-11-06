"""Tests for schema validation and completion checks."""

import pytest
from src.validator import (
    validate_schema,
    validate_extraction,
    get_missing_facts,
    is_complete,
    is_start_after_end,
    has_conflicting_details,
    has_identifier_mismatch,
    has_instruction_override,
)
from src.mock_provider import MOCK_RESPONSES


class TestSchemaValidation:
    def test_all_mock_responses_valid(self, schema):
        for doc_id, extraction in MOCK_RESPONSES.items():
            errors = validate_schema(extraction, schema)
            assert errors == [], f"{doc_id}: {errors}"

    def test_valid_extraction_passes(self, schema):
        ext = MOCK_RESPONSES["IT-001.json"]
        result = validate_extraction(ext, schema)
        assert result["valid"] is True

    def test_missing_required_field_fails(self, schema):
        ext = {"request_id": "IT-X", "request_date": None, "kind": None,
               "requester": None, "team": None, "system": None,
               "environment": None, "permission": None, "business_purpose": None,
               "start_date": None, "end_date": None, "manager": None,
               "supporting_quotes": [], "issues": []}
        result = validate_extraction(ext, schema)
        assert result["valid"] is True  # schema allows nulls
        assert result["is_complete"] is False


class TestCompletionChecks:
    def test_it001_is_complete(self):
        ext = MOCK_RESPONSES["IT-001.json"]
        assert is_complete(ext) is True

    def test_it003_is_not_complete(self):
        ext = MOCK_RESPONSES["IT-003.json"]
        assert is_complete(ext) is False

    def test_it005_withdrawal_not_complete(self):
        ext = MOCK_RESPONSES["IT-005.json"]
        assert is_complete(ext) is False

    def test_missing_facts_it003(self):
        ext = MOCK_RESPONSES["IT-003.json"]
        missing = get_missing_facts(ext)
        assert "environment" in missing
        assert "permission" in missing
        assert "start_date" in missing
        assert "end_date" in missing


class TestIssueDetection:
    def test_it006_has_instruction_override(self):
        ext = MOCK_RESPONSES["IT-006.json"]
        assert has_instruction_override(ext) is True

    def test_it001_no_override(self):
        ext = MOCK_RESPONSES["IT-001.json"]
        assert has_instruction_override(ext) is False

    def test_it004_has_conflict(self):
        ext = MOCK_RESPONSES["IT-004.json"]
        assert has_conflicting_details(ext) is True

    def test_it016_has_mismatch(self):
        ext = MOCK_RESPONSES["IT-016.json"]
        assert has_identifier_mismatch(ext) is True

    def test_it014_start_after_end(self):
        ext = MOCK_RESPONSES["IT-014.json"]
        assert is_start_after_end(ext) is True

    def test_it001_not_start_after_end(self):
        ext = MOCK_RESPONSES["IT-001.json"]
        assert is_start_after_end(ext) is False
