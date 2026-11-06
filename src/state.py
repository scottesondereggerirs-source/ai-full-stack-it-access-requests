"""State persistence: extractions, history, resume, source versioning."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import read_jsonl_file, write_jsonl_file

logger = logging.getLogger("it_access_requests")


class StateManager:
    """Manages extraction records with source versioning and resume support.

    Storage layout:
      <deliverables_dir>/extractions.jsonl  — one current record per document
      <deliverables_dir>/history/             — prior versions keyed by document_id

    Each record in extractions.jsonl:
      document_id, source_sha256, extraction, status, owner, next_action,
      error, response_source, timestamp, processing_settings
    """

    def __init__(self, deliverables_dir: str, processing_settings: dict[str, Any] | None = None) -> None:
        self.deliverables_dir = Path(deliverables_dir)
        self.deliverables_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir = self.deliverables_dir / "history"
        self.history_dir.mkdir(exist_ok=True)
        self.extractions_path = self.deliverables_dir / "extractions.jsonl"
        self.processing_settings = processing_settings or {}

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def load_all_records(self) -> list[dict[str, Any]]:
        """Load all records from extractions.jsonl."""
        return read_jsonl_file(self.extractions_path)

    def get_current_records(self) -> dict[str, dict[str, Any]]:
        """Return the latest record for each document_id."""
        all_records = self.load_all_records()
        current: dict[str, dict[str, Any]] = {}
        for record in all_records:
            doc_id = record["document_id"]
            current[doc_id] = record
        return current

    def get_history(self, document_id: str) -> list[dict[str, Any]]:
        """Return prior versions for a document_id from the history file."""
        history_path = self.history_dir / f"{document_id}.jsonl"
        return read_jsonl_file(history_path)

    def is_unchanged(
        self,
        document_id: str,
        source_sha256: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Check if the source is unchanged since last processing.

        Returns (unchanged, existing_record).
        """
        current = self.get_current_records()
        if document_id not in current:
            return False, None

        existing = current[document_id]
        if existing["source_sha256"] == source_sha256:
            # Also check processing settings match
            if existing.get("processing_settings") == self.processing_settings:
                return True, existing
        return False, existing

    def save_record(self, record: dict[str, Any]) -> None:
        """Save a new extraction record.

        If a prior record exists for the same document_id, archive it to history.
        """
        document_id = record["document_id"]

        # Archive prior record to history if it exists
        current = self.get_current_records()
        if document_id in current:
            prior = current[document_id]
            self._archive_to_history(document_id, prior)

        # Replace the current record for this document (one current record per
        # document); the prior version was archived to history above.
        record["timestamp"] = self._now()
        record["processing_settings"] = self.processing_settings

        current[document_id] = record
        write_jsonl_file(self.extractions_path, list(current.values()))
        logger.info("Saved record for %s (status=%s)", document_id, record.get("status"))

    def _archive_to_history(self, document_id: str, record: dict[str, Any]) -> None:
        """Append a prior record to the document's history file."""
        history_path = self.history_dir / f"{document_id}.jsonl"
        history = read_jsonl_file(history_path)
        history.append(record)
        write_jsonl_file(history_path, history)
        logger.debug("Archived prior record for %s to history", document_id)

    def get_pending_documents(
        self,
        all_document_ids: list[str],
    ) -> list[str]:
        """Return document_ids that have no current record or have errors."""
        current = self.get_current_records()
        pending: list[str] = []
        for doc_id in all_document_ids:
            if doc_id not in current:
                pending.append(doc_id)
            elif current[doc_id].get("status") == "error":
                pending.append(doc_id)
        return pending

    def get_retry_command(self, document_id: str) -> str:
        """Return a reproducible retry command for a failed document."""
        return f"python -m src.main retry {document_id}"

    def get_status_summary(self, all_document_ids: list[str]) -> dict[str, Any]:
        """Return a summary of processing status."""
        current = self.get_current_records()
        summary: dict[str, Any] = {
            "total": len(all_document_ids),
            "processed": 0,
            "pending": 0,
            "errors": 0,
            "by_status": {},
            "by_owner": {},
        }
        for doc_id in all_document_ids:
            if doc_id in current:
                record = current[doc_id]
                status = record.get("status", "unknown")
                owner = record.get("owner", "unknown")
                summary["processed"] += 1
                summary["by_status"][status] = summary["by_status"].get(status, 0) + 1
                summary["by_owner"][owner] = summary["by_owner"].get(owner, 0) + 1
                if status == "error":
                    summary["errors"] += 1
            else:
                summary["pending"] += 1
        return summary
