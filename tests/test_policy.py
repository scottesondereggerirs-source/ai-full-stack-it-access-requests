"""Tests for office policy routing logic."""

import pytest
from src.policy import apply_policy
from src.mock_provider import MOCK_RESPONSES


def get_extraction(doc_id: str) -> dict:
    """Get mock extraction for a document."""
    return MOCK_RESPONSES[doc_id]


# Rule 1: Instruction-override attempt
class TestInstructionOverride:
    def test_it006_routes_to_inspect_message(self):
        ext = get_extraction("IT-006.json")
        result = apply_policy(ext)
        assert result["status"] == "review"
        assert result["owner"] == "IT Service Desk"
        assert result["next_action"] == "inspect-message"


# Rule 2: Withdrawal with request_id and request_date
class TestWithdrawal:
    def test_it005_routes_to_confirm_withdrawal(self):
        ext = get_extraction("IT-005.json")
        result = apply_policy(ext)
        assert result["status"] == "review"
        assert result["owner"] == "IT Service Desk"
        assert result["next_action"] == "confirm-withdrawal"


# Rule 3: Needs-information
class TestNeedsInformation:
    def test_it003_missing_facts_routes_to_clarify(self):
        ext = get_extraction("IT-003.json")
        result = apply_policy(ext)
        assert result["status"] == "needs-information"
        assert result["owner"] == "IT Service Desk"
        assert result["next_action"] == "clarify"

    def test_it004_conflicting_environment_routes_to_clarify(self):
        ext = get_extraction("IT-004.json")
        result = apply_policy(ext)
        assert result["status"] == "needs-information"
        assert result["next_action"] == "clarify"

    def test_it014_start_after_end_routes_to_clarify(self):
        ext = get_extraction("IT-014.json")
        result = apply_policy(ext)
        assert result["status"] == "needs-information"
        assert result["next_action"] == "clarify"

    def test_it016_identifier_mismatch_routes_to_clarify(self):
        ext = get_extraction("IT-016.json")
        result = apply_policy(ext)
        assert result["status"] == "needs-information"
        assert result["next_action"] == "clarify"

    def test_it017_kind_other_routes_to_clarify(self):
        ext = get_extraction("IT-017.json")
        result = apply_policy(ext)
        assert result["status"] == "needs-information"
        assert result["next_action"] == "clarify"

    def test_it009_missing_manager_routes_to_clarify(self):
        ext = get_extraction("IT-009.json")
        result = apply_policy(ext)
        assert result["status"] == "needs-information"
        assert result["next_action"] == "clarify"

    def test_it011_missing_dates_routes_to_clarify(self):
        ext = get_extraction("IT-011.json")
        result = apply_policy(ext)
        assert result["status"] == "needs-information"
        assert result["next_action"] == "clarify"

    def test_it020_conflicting_manager_routes_to_clarify(self):
        ext = get_extraction("IT-020.json")
        result = apply_policy(ext)
        assert result["status"] == "needs-information"
        assert result["next_action"] == "clarify"


# Rule 4: Complete new/change with production, write, or admin → Security Review
class TestSecurityReview:
    def test_it002_production_write_routes_to_security(self):
        ext = get_extraction("IT-002.json")
        result = apply_policy(ext)
        assert result["status"] == "review"
        assert result["owner"] == "Security Review"
        assert result["next_action"] == "review-risk-then-owner"

    def test_it012_staging_admin_routes_to_security(self):
        ext = get_extraction("IT-012.json")
        result = apply_policy(ext)
        assert result["owner"] == "Security Review"
        assert result["next_action"] == "review-risk-then-owner"

    def test_it013_production_read_routes_to_security(self):
        ext = get_extraction("IT-013.json")
        result = apply_policy(ext)
        assert result["owner"] == "Security Review"

    def test_it019_production_change_routes_to_security(self):
        ext = get_extraction("IT-019.json")
        result = apply_policy(ext)
        assert result["owner"] == "Security Review"


# Rule 5: Complete staging/read → System Owner
class TestSystemOwner:
    def test_it001_staging_read_routes_to_system_owner(self):
        ext = get_extraction("IT-001.json")
        result = apply_policy(ext)
        assert result["status"] == "review"
        assert result["owner"] == "System Owner"
        assert result["next_action"] == "review-request"

    def test_it008_staging_read_routes_to_system_owner(self):
        ext = get_extraction("IT-008.json")
        result = apply_policy(ext)
        assert result["owner"] == "System Owner"

    def test_it015_staging_read_routes_to_system_owner(self):
        ext = get_extraction("IT-015.json")
        result = apply_policy(ext)
        assert result["owner"] == "System Owner"

    def test_it018_staging_read_routes_to_system_owner(self):
        ext = get_extraction("IT-018.json")
        result = apply_policy(ext)
        assert result["owner"] == "System Owner"

    def test_it007_staging_change_write_routes_to_security(self):
        """IT-007 is a change to write in staging — should go to Security Review (write)."""
        ext = get_extraction("IT-007.json")
        result = apply_policy(ext)
        assert result["owner"] == "Security Review"
