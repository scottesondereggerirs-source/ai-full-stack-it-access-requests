"""Tests for state management: versioning, unchanged reruns, retry after success."""

import pytest
from src.state import StateManager


class TestStateBasics:
    def test_empty_state(self, state_manager):
        records = state_manager.load_all_records()
        assert records == []
        assert state_manager.get_current_records() == {}

    def test_save_and_load(self, state_manager):
        record = {
            "document_id": "IT-001.json",
            "source_sha256": "abc123",
            "extraction": {"request_id": "IT-001"},
            "status": "review",
            "owner": "System Owner",
            "next_action": "review-request",
            "error": None,
            "response_source": "mock",
        }
        state_manager.save_record(record)
        current = state_manager.get_current_records()
        assert "IT-001.json" in current
        assert current["IT-001.json"]["status"] == "review"


class TestUnchangedRerun:
    def test_unchanged_detected(self, state_manager):
        record = {
            "document_id": "IT-001.json",
            "source_sha256": "abc123",
            "extraction": {},
            "status": "review",
            "owner": "System Owner",
            "next_action": "review-request",
            "error": None,
            "response_source": "mock",
        }
        state_manager.save_record(record)
        unchanged, existing = state_manager.is_unchanged("IT-001.json", "abc123")
        assert unchanged is True
        assert existing is not None

    def test_changed_source_detected(self, state_manager):
        record = {
            "document_id": "IT-001.json",
            "source_sha256": "abc123",
            "extraction": {},
            "status": "review",
            "owner": "System Owner",
            "next_action": "review-request",
            "error": None,
            "response_source": "mock",
        }
        state_manager.save_record(record)
        unchanged, existing = state_manager.is_unchanged("IT-001.json", "different_hash")
        assert unchanged is False
        assert existing is not None


class TestHistoryRetention:
    def test_prior_version_archived(self, state_manager):
        # Save first version
        record1 = {
            "document_id": "IT-001.json",
            "source_sha256": "hash1",
            "extraction": {"request_id": "IT-001", "start_date": "2026-01-01"},
            "status": "review",
            "owner": "System Owner",
            "next_action": "review-request",
            "error": None,
            "response_source": "mock",
        }
        state_manager.save_record(record1)

        # Save second version (changed source)
        record2 = {
            "document_id": "IT-001.json",
            "source_sha256": "hash2",
            "extraction": {"request_id": "IT-001", "start_date": "2026-02-01"},
            "status": "review",
            "owner": "System Owner",
            "next_action": "review-request",
            "error": None,
            "response_source": "mock",
        }
        state_manager.save_record(record2)

        # Check current is the second version
        current = state_manager.get_current_records()
        assert current["IT-001.json"]["source_sha256"] == "hash2"

        # Check history has the first version
        history = state_manager.get_history("IT-001.json")
        assert len(history) == 1
        assert history[0]["source_sha256"] == "hash1"


class TestRetryAfterSuccess:
    def test_failed_retry_preserves_prior_success(self, state_manager):
        # First attempt succeeds
        success_record = {
            "document_id": "IT-001.json",
            "source_sha256": "hash1",
            "extraction": {"request_id": "IT-001"},
            "status": "review",
            "owner": "System Owner",
            "next_action": "review-request",
            "error": None,
            "response_source": "mock",
        }
        state_manager.save_record(success_record)

        # Second attempt fails (same source, different settings)
        failed_record = {
            "document_id": "IT-001.json",
            "source_sha256": "hash1",
            "extraction": None,
            "status": "error",
            "owner": "IT Service Desk",
            "next_action": "retry-extraction",
            "error": "Provider timeout",
            "response_source": "mock",
        }
        state_manager.save_record(failed_record)

        # Current should show the error
        current = state_manager.get_current_records()
        assert current["IT-001.json"]["status"] == "error"

        # History should have the prior success
        history = state_manager.get_history("IT-001.json")
        assert len(history) == 1
        assert history[0]["status"] == "review"

    def test_retry_command_is_reproducible(self, state_manager):
        cmd = state_manager.get_retry_command("IT-006.json")
        assert cmd == "python -m src.main retry IT-006.json"


class TestPendingDetection:
    def test_get_pending_unprocessed(self, state_manager):
        pending = state_manager.get_pending_documents(["IT-001.json", "IT-002.json"])
        assert pending == ["IT-001.json", "IT-002.json"]

    def test_get_pending_with_error(self, state_manager):
        record = {
            "document_id": "IT-001.json",
            "source_sha256": "hash1",
            "extraction": {},
            "status": "error",
            "owner": "IT Service Desk",
            "next_action": "retry-extraction",
            "error": "timeout",
            "response_source": "mock",
        }
        state_manager.save_record(record)
        pending = state_manager.get_pending_documents(["IT-001.json", "IT-002.json"])
        assert "IT-001.json" in pending
        assert "IT-002.json" in pending
