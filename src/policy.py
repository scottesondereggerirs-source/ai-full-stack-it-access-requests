"""Office policy routing logic — applies review rules in order."""

from typing import Any

from .validator import (
    has_conflicting_details,
    has_identifier_mismatch,
    has_instruction_override,
    is_complete,
    is_start_after_end,
)


def apply_policy(extraction: dict[str, Any]) -> dict[str, str]:
    """Apply office-policy routing rules in order.

    Returns a dict with keys: status, owner, next_action.

    Rules (from materials/office-policy.md, applied in order):
      1. Instruction-override attempt → IT Service Desk / review / inspect-message
      2. Withdrawal with request_id + request_date → IT Service Desk / review / confirm-withdrawal
      3. kind other/null, conflicting details, identifier mismatch, missing required facts,
         start_date after end_date → IT Service Desk / needs-information / clarify
      4. Complete new/change with production OR write OR admin → Security Review / review / review-risk-then-owner
      5. Complete staging/read → System Owner / review / review-request
    """
    kind = extraction.get("kind")
    request_id = extraction.get("request_id")
    request_date = extraction.get("request_date")
    environment = extraction.get("environment")
    permission = extraction.get("permission")

    # Rule 1: Instruction-override attempt
    if has_instruction_override(extraction):
        return {
            "status": "review",
            "owner": "IT Service Desk",
            "next_action": "inspect-message",
        }

    # Rule 2: Withdrawal with request_id and request_date
    if kind == "withdrawal" and request_id and request_date:
        return {
            "status": "review",
            "owner": "IT Service Desk",
            "next_action": "confirm-withdrawal",
        }

    # Rule 3: Needs-information conditions
    needs_info = False
    if kind in ("other", None):
        needs_info = True
    if has_conflicting_details(extraction):
        needs_info = True
    if has_identifier_mismatch(extraction):
        needs_info = True
    if is_start_after_end(extraction):
        needs_info = True
    # Check missing required facts for new/change requests
    if kind in ("new", "change") and not is_complete(extraction):
        needs_info = True

    if needs_info:
        return {
            "status": "needs-information",
            "owner": "IT Service Desk",
            "next_action": "clarify",
        }

    # Rule 4: Complete new/change involving production, write, or admin
    if kind in ("new", "change") and is_complete(extraction):
        if environment == "production" or permission in ("write", "admin"):
            return {
                "status": "review",
                "owner": "Security Review",
                "next_action": "review-risk-then-owner",
            }

    # Rule 5: Complete staging/read
    if kind in ("new", "change") and is_complete(extraction):
        if environment == "staging" and permission == "read":
            return {
                "status": "review",
                "owner": "System Owner",
                "next_action": "review-request",
            }

    # Fallback: should not reach here if extraction is valid and complete
    return {
        "status": "needs-information",
        "owner": "IT Service Desk",
        "next_action": "clarify",
    }
