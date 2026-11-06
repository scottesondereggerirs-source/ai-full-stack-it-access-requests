"""Schema validation and completion checks for extracted records."""

import json
from pathlib import Path
from typing import Any

import jsonschema


REQUIRED_FACTS = [
    "request_id",
    "request_date",
    "requester",
    "team",
    "system",
    "environment",
    "permission",
    "business_purpose",
    "start_date",
    "end_date",
    "manager",
]


def load_schema(schema_path: str | Path) -> dict[str, Any]:
    """Load the JSON schema from disk."""
    with open(schema_path, encoding="utf-8") as f:
        return json.load(f)


def validate_schema(extraction: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Validate extraction against the JSON schema.

    Returns a list of error messages (empty if valid).
    """
    errors: list[str] = []
    validator = jsonschema.Draft7Validator(schema)
    for error in validator.iter_errors(extraction):
        errors.append(f"Schema validation error at {'/'.join(str(p) for p in error.absolute_path)}: {error.message}")
    return errors


def get_missing_facts(extraction: dict[str, Any]) -> list[str]:
    """Return list of required facts that are null or missing (for new/change requests)."""
    missing: list[str] = []
    for fact in REQUIRED_FACTS:
        val = extraction.get(fact)
        if val is None or val == "":
            missing.append(fact)
    return missing


def has_instruction_override(extraction: dict[str, Any]) -> bool:
    """Check if the extraction flags an instruction-override attempt."""
    issues_text = " ".join(extraction.get("issues", []))
    return "override" in issues_text.lower() or "instruction-override" in issues_text.lower()


def has_identifier_mismatch(extraction: dict[str, Any]) -> bool:
    """Check if the extraction flags an identifier mismatch."""
    issues_text = " ".join(extraction.get("issues", []))
    return "mismatch" in issues_text.lower()


def has_conflicting_details(extraction: dict[str, Any]) -> bool:
    """Check if the extraction flags conflicting details."""
    issues_text = " ".join(extraction.get("issues", []))
    return "conflict" in issues_text.lower()


def is_start_after_end(extraction: dict[str, Any]) -> bool:
    """Check if start_date is after end_date (both must be non-null strings)."""
    start = extraction.get("start_date")
    end = extraction.get("end_date")
    if start and end:
        return start > end
    return False


def is_complete(extraction: dict[str, Any]) -> bool:
    """Check if all required facts are present and non-null for new/change requests."""
    kind = extraction.get("kind")
    if kind not in ("new", "change"):
        return False
    return len(get_missing_facts(extraction)) == 0


def validate_extraction(extraction: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Full validation: schema + completion checks.

    Returns a dict with:
      - valid: bool
      - schema_errors: list[str]
      - missing_facts: list[str]
      - has_conflict: bool
      - has_mismatch: bool
      - has_override: bool
      - start_after_end: bool
      - is_complete: bool
    """
    schema_errors = validate_schema(extraction, schema)
    missing = get_missing_facts(extraction)
    override = has_instruction_override(extraction)
    mismatch = has_identifier_mismatch(extraction)
    conflict = has_conflicting_details(extraction)
    after_end = is_start_after_end(extraction)
    complete = is_complete(extraction) if not schema_errors else False

    return {
        "valid": len(schema_errors) == 0,
        "schema_errors": schema_errors,
        "missing_facts": missing,
        "has_conflict": conflict,
        "has_mismatch": mismatch,
        "has_override": override,
        "start_after_end": after_end,
        "is_complete": complete,
    }
