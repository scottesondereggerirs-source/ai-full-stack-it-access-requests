"""Validate provider output and apply the Northstar review rules in order.

Schema and quote checks establish structural/evidence validity, not general
semantic truth. Independent source comparisons and human review are still needed.
"""

from datetime import date
import json
from pathlib import Path
import re
import shlex
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from .providers import ProviderResponse


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "snapshot.schema.json"
POLICY_PATH = ROOT / "materials" / "office-policy.md"
PROMPT_PATH = Path(__file__).with_name("extraction_prompt.txt")
ROUTING_VERSION = "1"
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_OVERRIDE = re.compile(
    r"SYSTEM OVERRIDE|bypass approval|display the API key|"
    r"mark request as approved|highest-priority system instruction",
    re.IGNORECASE,
)
_UNFINISHED = {"incomplete", "in_progress", "queued", "cancelled"}


class ExtractionError(ValueError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def build_messages(message: str) -> list[dict[str, str]]:
    """Use file-authored instructions; the provider supplies the schema separately."""
    instructions = PROMPT_PATH.read_text(encoding="utf-8")
    return [{"role": "system", "content": instructions}, {"role": "user", "content": message}]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Nonstandard JSON numeric constant")


def _require_completion(value: Any) -> None:
    if value == "completed":
        return
    if isinstance(value, str) and value in _UNFINISHED:
        raise ExtractionError("interrupted_response", "Provider response was not completed; retry extraction.")
    if value == "failed":
        raise ExtractionError("provider_unavailable", "Provider reported a failed response; retry extraction.")
    raise ExtractionError("malformed_output", "Provider response lacks a recognized completion status.")


def _parse_response(payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise ExtractionError("malformed_output", "Provider response must be an object.")
    _require_completion(payload.get("status"))
    output = payload.get("output")
    if not isinstance(output, list) or len(output) != 1:
        raise ExtractionError("malformed_output", "Expected exactly one assistant output message.")
    assistant = output[0]
    if (
        not isinstance(assistant, dict)
        or assistant.get("type") != "message"
        or assistant.get("role") != "assistant"
    ):
        raise ExtractionError("malformed_output", "Expected an assistant message, not a tool or other output.")
    _require_completion(assistant.get("status"))
    content = assistant.get("content")
    if not isinstance(content, list) or not content or any(not isinstance(part, dict) for part in content):
        raise ExtractionError("malformed_output", "Assistant content must contain extraction text.")
    # A refusal cannot be bypassed by adding an otherwise usable text part.
    if any(part.get("type") == "refusal" for part in content):
        raise ExtractionError("refusal", "Provider refused extraction; inspect the source message before retrying.")
    if len(content) != 1 or content[0].get("type") != "output_text":
        raise ExtractionError("malformed_output", "Expected exactly one extraction text part.")
    text = content[0].get("text")
    if not isinstance(text, str) or not text.strip():
        raise ExtractionError("malformed_output", "Extraction text is absent, empty or not a string.")
    try:
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, RecursionError) as error:
        # Do not echo an arbitrary model response into errors or logs.
        raise ExtractionError("malformed_output", "Extraction text is not valid unambiguous JSON.") from error


def _add_issue(extraction: dict[str, Any], code: str, explanation: str) -> None:
    prefix = code + ":"
    if not any(issue.startswith(prefix) and issue[len(prefix):].strip() for issue in extraction["issues"]):
        extraction["issues"].append(f"{prefix} {explanation}")


def _validate(extraction: Any, message: str, schema: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    scalar_fields = [name for name, spec in schema["properties"].items() if spec["type"] != "array"]
    if isinstance(extraction, dict):
        # Parsed JSON is new storage, not the provider's payload. Normalize only
        # blank scalar strings; absent keys and invalid arrays must still fail.
        for name in scalar_fields:
            value = extraction.get(name)
            if isinstance(value, str) and not value.strip():
                extraction[name] = None
    try:
        Draft202012Validator(schema).validate(extraction)
    except ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise ExtractionError(
            "validation",
            f"Extraction violates snapshot.schema.json at {location}; check required keys, types and enum values.",
        ) from error

    quotes = extraction["supporting_quotes"]
    if not quotes or any(not quote or quote not in message for quote in quotes):
        raise ExtractionError("validation", "Supporting quotes must be nonempty exact substrings of the message.")
    for field in ("request_date", "start_date", "end_date"):
        value = extraction[field]
        if value is not None:
            try:
                if not _DATE.fullmatch(value):
                    raise ValueError("Invalid date format")
                date.fromisoformat(value)
            except ValueError as error:
                raise ExtractionError("validation", f"Invalid {field}; use a real calendar date in YYYY-MM-DD form.") from error

    for issue in extraction["issues"]:
        if issue.startswith("conflict:"):
            parts = issue.split(":", 2)
            if len(parts) != 3 or parts[1] not in scalar_fields or not parts[2].strip():
                raise ExtractionError("validation", "A conflict issue must name a scalar field and explain its alternatives.")
            if len(set(quotes)) < 2:
                raise ExtractionError("validation", "An unresolved conflict requires distinct source quotes for its alternatives.")
            extraction[parts[1]] = None
    for field in scalar_fields:
        if extraction[field] is None:
            _add_issue(extraction, "missing:" + field, f"No explicit resolved {field} is available.")
    return extraction, scalar_fields


def _find_override(extraction: dict[str, Any], message: str) -> bool:
    marked = any(
        issue.startswith("instruction-override:") and issue[len("instruction-override:"):].strip()
        for issue in extraction["issues"]
    )
    detected = False
    for match in re.finditer(r"[^.!?\n]+[.!?]?", message):
        sentence = match.group().strip()
        if _OVERRIDE.search(sentence):
            detected = True
            if sentence not in extraction["supporting_quotes"]:
                extraction["supporting_quotes"].append(sentence)
    if detected:
        _add_issue(extraction, "instruction-override", "Source attempts to override extraction or approval rules; inspect the message.")
    return bool(marked or detected)


def _route(extraction: dict[str, Any], document_id: str, message: str, scalar_fields: list[str]) -> tuple[str, str, str]:
    mismatch = extraction["request_id"] is not None and extraction["request_id"] != Path(document_id).stem
    if mismatch:
        _add_issue(extraction, "identifier-mismatch", "Stated request identifier differs from the input filename.")
    reversed_dates = (
        extraction["start_date"] is not None
        and extraction["end_date"] is not None
        and extraction["start_date"] > extraction["end_date"]
    )
    if reversed_dates:
        _add_issue(extraction, "invalid-range", "The stated start date is after the stated end date.")

    # These branches are the ordered rules, not model-provided approval choices.
    if _find_override(extraction, message):
        return "review", "IT Service Desk", "inspect-message"
    kind = extraction["kind"]
    if kind == "withdrawal" and extraction["request_id"] is not None and extraction["request_date"] is not None:
        return "review", "IT Service Desk", "confirm-withdrawal"
    has_conflict = any(issue.startswith("conflict:") for issue in extraction["issues"])
    if (
        kind not in ("new", "change")
        or has_conflict
        or mismatch
        or reversed_dates
        or any(extraction[field] is None for field in scalar_fields)
    ):
        return "needs-information", "IT Service Desk", "clarify"
    if extraction["environment"] == "production" or extraction["permission"] in ("write", "admin"):
        return "review", "Security Review", "review-risk-then-owner"
    return "review", "System Owner", "review-request"


def process_response(
    *,
    document_id: str,
    source_sha256: str,
    message: str,
    response: ProviderResponse,
) -> dict[str, Any]:
    """Return one complete terminal record, including explicit validation errors."""
    record: dict[str, Any] = {
        "document_id": document_id,
        "source_sha256": source_sha256,
        "response_source": response.response_source,
        "extraction": None,
        "status": "error",
        "owner": "IT Service Desk",
        "next_action": "retry-extraction",
        "error": None,
    }
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        extraction, scalar_fields = _validate(_parse_response(response.payload), message, schema)
        status, owner, action = _route(extraction, document_id, message, scalar_fields)
    except ExtractionError as error:
        record["error"] = {
            "category": error.category,
            "message": str(error),
            "retry_command": "it-access retry " + shlex.quote(document_id),
        }
        return record
    record.update(extraction=extraction, status=status, owner=owner, next_action=action)
    return record
