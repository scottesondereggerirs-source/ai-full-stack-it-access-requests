"""End-to-end tests: full pipeline from intake to queue."""

import json
import pytest
from pathlib import Path

from src.extractor import Extractor
from src.state import StateManager
from src.queue import build_queue, save_queue
from src.utils import compute_sha256, read_json_file
from src.validator import load_schema
from src.policy import apply_policy


class TestFullPipeline:
    def test_intake_all_messages(self, requests_dir, tmp_path):
        """Process all 20 messages through the full pipeline."""
        schema = load_schema(Path(__file__).parent.parent / "snapshot.schema.json")
        config = {
            "model": {
                "provider": "databricks",
                "model_id": "test-model",
                "base_url": "http://localhost",
                "temperature": 0.0,
                "max_tokens": 1024,
            },
            "response_source": "mock",
        }
        extractor = Extractor(config, schema, use_mock=True)
        state = StateManager(
            deliverables_dir=str(tmp_path / "deliverables"),
            processing_settings={"model_id": "test-model", "response_source": "mock"},
        )

        request_files = sorted(requests_dir.glob("*.json"))
        all_doc_ids = [f.name for f in request_files]

        for file_path in request_files:
            doc_id = file_path.name
            sha256 = compute_sha256(file_path)
            data = read_json_file(file_path)
            message = data.get("message", "")
            record = extractor.extract_with_metadata(doc_id, message, sha256)
            state.save_record(record)

        # Verify all 20 processed
        current = state.get_current_records()
        assert len(current) == 20

        # Verify queue builds correctly
        queue = build_queue(state, all_doc_ids)
        assert len(queue) == 20

        # Verify no pending entries
        pending = [e for e in queue if e["status"] == "pending"]
        assert len(pending) == 0

    def test_unchanged_rerun_produces_same_queue(self, requests_dir, tmp_path):
        """Running intake twice on unchanged sources should reuse results."""
        schema = load_schema(Path(__file__).parent.parent / "snapshot.schema.json")
        config = {
            "model": {
                "provider": "databricks",
                "model_id": "test-model",
                "base_url": "http://localhost",
            },
            "response_source": "mock",
        }
        extractor = Extractor(config, schema, use_mock=True)
        state = StateManager(
            deliverables_dir=str(tmp_path / "deliverables"),
            processing_settings={"model_id": "test-model", "response_source": "mock"},
        )

        request_files = sorted(requests_dir.glob("*.json"))
        all_doc_ids = [f.name for f in request_files]

        # First run
        for file_path in request_files:
            doc_id = file_path.name
            sha256 = compute_sha256(file_path)
            data = read_json_file(file_path)
            record = extractor.extract_with_metadata(doc_id, data["message"], sha256)
            state.save_record(record)

        first_run = state.get_current_records()

        # Second run (unchanged)
        for file_path in request_files:
            doc_id = file_path.name
            sha256 = compute_sha256(file_path)
            unchanged, existing = state.is_unchanged(doc_id, sha256)
            assert unchanged is True, f"{doc_id} should be unchanged"

    def test_changed_source_creates_new_result_and_retains_history(self, requests_dir, tmp_path):
        """A changed source file should create a new result and archive the old one."""
        schema = load_schema(Path(__file__).parent.parent / "snapshot.schema.json")
        config = {"model": {"model_id": "test", "base_url": "http://localhost"}, "response_source": "mock"}
        extractor = Extractor(config, schema, use_mock=True)
        state = StateManager(
            deliverables_dir=str(tmp_path / "deliverables"),
            processing_settings={"model_id": "test", "response_source": "mock"},
        )

        # Process IT-001
        file_path = requests_dir / "IT-001.json"
        sha256 = compute_sha256(file_path)
        data = read_json_file(file_path)
        record = extractor.extract_with_metadata("IT-001.json", data["message"], sha256)
        state.save_record(record)

        # Simulate changed source with a different hash
        record2 = extractor.extract_with_metadata("IT-001.json", data["message"], "changed_hash")
        state.save_record(record2)

        current = state.get_current_records()["IT-001.json"]
        assert current["source_sha256"] == "changed_hash"

        history = state.get_history("IT-001.json")
        assert len(history) == 1
        assert history[0]["source_sha256"] == sha256

    def test_complete_request_routing(self, requests_dir):
        """IT-001 (staging/read) should route to System Owner."""
        from src.mock_provider import MOCK_RESPONSES
        ext = MOCK_RESPONSES["IT-001.json"]
        result = apply_policy(ext)
        assert result["owner"] == "System Owner"
        assert result["next_action"] == "review-request"

    def test_missing_dates_routing(self):
        """IT-011 (relative dates) should route to needs-information."""
        from src.mock_provider import MOCK_RESPONSES
        ext = MOCK_RESPONSES["IT-011.json"]
        result = apply_policy(ext)
        assert result["status"] == "needs-information"

    def test_conflicting_details_routing(self):
        """IT-020 (conflicting manager) should route to needs-information."""
        from src.mock_provider import MOCK_RESPONSES
        ext = MOCK_RESPONSES["IT-020.json"]
        result = apply_policy(ext)
        assert result["status"] == "needs-information"

    def test_embedded_instructions_routing(self):
        """IT-006 (instruction override) should route to inspect-message."""
        from src.mock_provider import MOCK_RESPONSES
        ext = MOCK_RESPONSES["IT-006.json"]
        result = apply_policy(ext)
        assert result["next_action"] == "inspect-message"
