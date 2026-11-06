"""Shared utilities: hashing, file I/O, credential redaction, logging."""

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any


def compute_sha256(file_path: str | Path) -> str:
    """Compute SHA-256 hash of a file's raw bytes."""
    with open(file_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def read_json_file(file_path: str | Path) -> dict[str, Any]:
    """Read and parse a JSON file."""
    with open(file_path, encoding="utf-8") as f:
        return json.load(f)


def write_json_file(file_path: str | Path, data: Any) -> None:
    """Write data as JSON to a file (indented, trailing newline)."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def write_jsonl_file(file_path: str | Path, records: list[dict[str, Any]]) -> None:
    """Write records as JSON Lines (one JSON object per line)."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl_file(file_path: str | Path) -> list[dict[str, Any]]:
    """Read a JSON Lines file into a list of dicts."""
    records: list[dict[str, Any]] = []
    path = Path(file_path)
    if not path.exists():
        return records
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def redact_credentials(text: str) -> str:
    """Redact API keys and bearer tokens from text for safe logging."""
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", r"\1***REDACTED***", text)
    text = re.sub(r'("api_key"\s*:\s*")[^"]*(")', r'\1***REDACTED***\2', text)
    text = re.sub(r"(api_key=)[A-Za-z0-9\-._~+/]+", r"\1***REDACTED***", text)
    return text


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure and return a module logger."""
    logger = logging.getLogger("it_access_requests")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger
