"""Queue building from current extraction records."""

from pathlib import Path
from typing import Any

from .state import StateManager
from .utils import write_json_file


def build_queue(
    state: StateManager,
    all_document_ids: list[str],
) -> list[dict[str, Any]]:
    """Build the review queue from current extraction records.

    Returns a list of queue entries, each with:
      document_id, source_sha256, status, owner, next_action,
      request_id, kind, system, environment, permission, error
    """
    current = state.get_current_records()
    queue: list[dict[str, Any]] = []

    for doc_id in all_document_ids:
        if doc_id not in current:
            queue.append({
                "document_id": doc_id,
                "source_sha256": None,
                "status": "pending",
                "owner": None,
                "next_action": None,
                "request_id": None,
                "kind": None,
                "system": None,
                "environment": None,
                "permission": None,
                "error": None,
            })
            continue

        record = current[doc_id]
        extraction = record.get("extraction") or {}
        queue.append({
            "document_id": doc_id,
            "source_sha256": record.get("source_sha256"),
            "status": record.get("status"),
            "owner": record.get("owner"),
            "next_action": record.get("next_action"),
            "request_id": extraction.get("request_id"),
            "kind": extraction.get("kind"),
            "system": extraction.get("system"),
            "environment": extraction.get("environment"),
            "permission": extraction.get("permission"),
            "error": record.get("error"),
        })

    return queue


def save_queue(
    state: StateManager,
    all_document_ids: list[str],
    output_path: str | Path,
) -> list[dict[str, Any]]:
    """Build and save the queue to queue.json."""
    queue = build_queue(state, all_document_ids)
    write_json_file(output_path, {
        "generated_at": state._now(),
        "total": len(queue),
        "entries": queue,
    })
    return queue


def display_queue(queue: list[dict[str, Any]]) -> str:
    """Return a human-readable summary of the queue."""
    lines: list[str] = []
    lines.append(f"Review Queue ({len(queue)} entries)")
    lines.append("=" * 80)

    # Group by owner
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for entry in queue:
        owner = entry.get("owner") or "(unprocessed)"
        by_owner.setdefault(owner, []).append(entry)

    for owner in sorted(by_owner.keys()):
        entries = by_owner[owner]
        lines.append(f"\n{owner} ({len(entries)} entries):")
        for e in entries:
            doc = e["document_id"]
            status = e.get("status", "?")
            action = e.get("next_action", "?")
            req_id = e.get("request_id") or "?"
            kind = e.get("kind") or "?"
            system = e.get("system") or "?"
            env = e.get("environment") or "?"
            perm = e.get("permission") or "?"
            lines.append(
                f"  {doc:16s}  {status:20s}  {action:25s}  "
                f"{req_id:10s}  {kind:10s}  {system:10s}  {env:10s}  {perm:5s}"
            )
            if e.get("error"):
                lines.append(f"    ERROR: {e['error']}")

    return "\n".join(lines)
