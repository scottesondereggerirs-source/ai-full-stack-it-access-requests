"""SQLite current records, immutable source revisions and retained attempts.

No provider call happens in a transaction. The CLI's process-level admission
lock is a separate concern; these primitives guard transitions and source identity
even when an obsolete result is accidentally submitted to the store.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3
from typing import Any
from urllib.parse import urlsplit


class StateError(RuntimeError):
    """An invalid state transition, configuration or database operation."""


@dataclass(frozen=True)
class ScheduledAttempt:
    document_id: str
    revision_id: int
    attempt_id: int
    needs_processing: bool


_CACHE_FIELDS = (
    "provider", "endpoint", "model", "prompt_sha256", "schema_sha256",
    "generation", "policy_sha256", "routing_version", "mock_fixture_hashes",
)
_SECRET_KEYS = {
    "api_key", "apikey", "authorization", "access_token", "api_token",
    "password", "secret", "credentials", "headers", "token",
}
_ROUTES = {
    ("review", "IT Service Desk", "inspect-message"),
    ("review", "IT Service Desk", "confirm-withdrawal"),
    ("needs-information", "IT Service Desk", "clarify"),
    ("review", "Security Review", "review-risk-then-owner"),
    ("review", "System Owner", "review-request"),
}
_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    current_revision_id INTEGER REFERENCES revisions(revision_id),
    current_attempt_id INTEGER REFERENCES attempts(attempt_id)
);
CREATE TABLE IF NOT EXISTS revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    source_bytes BLOB NOT NULL,
    source_sha256 TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    settings_fingerprint TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
    settings_json TEXT NOT NULL,
    settings_fingerprint TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','running','succeeded','failed','interrupted')),
    status TEXT NOT NULL,
    owner TEXT NOT NULL,
    next_action TEXT NOT NULL,
    response_source TEXT,
    extraction_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS revision_documents ON revisions(document_id, revision_id);
CREATE INDEX IF NOT EXISTS attempt_revisions ON attempts(revision_id, attempt_id);
COMMIT;
"""
_VIEW = """
SELECT a.attempt_id, a.revision_id, r.document_id, r.source_sha256,
       a.settings_json, a.settings_fingerprint, a.policy_version, a.state,
       a.status, a.owner, a.next_action, a.response_source, a.extraction_json,
       a.error_json, a.created_at, a.started_at, a.finished_at
FROM attempts a JOIN revisions r ON r.revision_id = a.revision_id
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        encoded.encode("utf-8")
        return encoded
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise StateError("State values must be finite, UTF-8 JSON data.") from error


def _reject_credentials(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise StateError("Settings keys must be strings.")
            if key.lower().replace("-", "_") in _SECRET_KEYS:
                raise StateError("Credentials must remain in the environment, outside persisted settings.")
            _reject_credentials(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_credentials(child)


def _prepare_settings(settings: dict[str, Any]) -> tuple[str, str, str]:
    if not isinstance(settings, dict):
        raise StateError("Processing settings must be an object.")
    _reject_credentials(settings)
    required = set(_CACHE_FIELDS) - {"mock_fixture_hashes"}
    if not required.issubset(settings):
        raise StateError("Processing settings lack required extraction or policy provenance.")
    if not isinstance(settings["generation"], dict) or not isinstance(settings.get("mock_fixture_hashes", {}), dict):
        raise StateError("Generation settings and mock fixture hashes must be objects.")
    for key in required - {"generation"}:
        if not isinstance(settings[key], str) or not settings[key]:
            raise StateError("Processing provenance identifiers must be nonempty strings.")
    try:
        endpoint = urlsplit(settings["endpoint"])
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise StateError("Endpoint credentials and query/fragment values cannot be persisted.")
    except ValueError as error:
        raise StateError("Provider endpoint is malformed.") from error
    encoded = _json(settings)
    cache = {key: settings.get(key, {}) for key in _CACHE_FIELDS}
    fingerprint = hashlib.sha256(_json(cache).encode("utf-8")).hexdigest()
    return encoded, fingerprint, settings["policy_sha256"]


def settings_fingerprint(settings: dict[str, Any]) -> str:
    """Cache extraction inputs, excluding timeout, worker count and pacing."""
    return _prepare_settings(settings)[1]


def _record(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["settings"] = json.loads(result.pop("settings_json"))
    for stored, public in (("extraction_json", "extraction"), ("error_json", "error")):
        value = result.pop(stored)
        result[public] = json.loads(value) if value is not None else None
    return result


class StateStore:
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = Path(path).resolve()
        self.read_only = read_only
        self._connection: sqlite3.Connection | None = None
        try:
            if read_only:
                if not self.path.is_file():
                    raise StateError("Intake database does not exist; run intake first.")
                connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5.0, isolation_level=None)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
            self._connection = connection
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            if read_only:
                connection.execute("PRAGMA query_only = ON")
            else:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(_SCHEMA)
        except (OSError, sqlite3.Error) as error:
            self.close()
            raise StateError("Cannot initialize the intake database; check its path, permissions and availability.") from error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StateError("The intake database is closed.")
        return self._connection

    @contextmanager
    def _write(self):
        if self.read_only:
            raise StateError("This database connection is read-only.")
        connection = self._db
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield
            connection.commit()
        except BaseException as error:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(error, sqlite3.Error):
                raise StateError("Database transaction failed; check the database's availability and integrity.") from error
            raise

    @contextmanager
    def snapshot(self):
        connection = self._db
        connection.execute("BEGIN")
        try:
            yield self
        finally:
            connection.rollback()

    def _current_optional(self, document_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            _VIEW + " JOIN documents d ON d.current_attempt_id = a.attempt_id WHERE d.document_id = ?",
            (document_id,),
        ).fetchone()
        return _record(row) if row is not None else None

    def current(self, document_id: str) -> dict[str, Any]:
        row = self._current_optional(document_id)
        if row is None:
            raise StateError("No current record exists for this document.")
        return row

    def queue(self) -> list[dict[str, Any]]:
        # One SELECT gives extraction, route, attempt and source one read snapshot.
        rows = self._db.execute(
            _VIEW + " JOIN documents d ON d.current_attempt_id = a.attempt_id ORDER BY r.document_id"
        ).fetchall()
        return [_record(row) for row in rows]

    def history(self, document_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            _VIEW + " JOIN documents d ON d.document_id = r.document_id "
            "WHERE r.document_id = ? AND a.attempt_id != d.current_attempt_id ORDER BY a.attempt_id",
            (document_id,),
        ).fetchall()
        return [_record(row) for row in rows]

    def attempt(self, attempt_id: int) -> dict[str, Any]:
        row = self._db.execute(_VIEW + " WHERE a.attempt_id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise StateError("No recorded attempt exists for this identifier.")
        return _record(row)

    def source_bytes(self, attempt_id: int) -> bytes:
        row = self._db.execute(
            "SELECT r.source_bytes FROM revisions r JOIN attempts a ON a.revision_id = r.revision_id "
            "WHERE a.attempt_id = ?", (attempt_id,),
        ).fetchone()
        if row is None:
            raise StateError("No recorded source exists for this attempt.")
        return bytes(row[0])

    def _supersede(self, current: dict[str, Any] | None) -> None:
        if current is not None and current["state"] in ("pending", "running"):
            error = {
                "category": "interrupted_response",
                "message": "Attempt superseded by a newer source or processing configuration.",
                "retry_command": "it-access retry " + shlex.quote(current["document_id"]),
            }
            self._db.execute(
                "UPDATE attempts SET state = 'interrupted', status = 'error', owner = 'IT Service Desk', "
                "next_action = 'retry-extraction', error_json = ?, finished_at = ? WHERE attempt_id = ?",
                (_json(error), _now(), current["attempt_id"]),
            )

    def _new_attempt(self, document_id: str, revision_id: int, prepared: tuple[str, str, str]) -> ScheduledAttempt:
        encoded, fingerprint, policy_version = prepared
        settings = json.loads(encoded)
        response_source = "mock" if settings["provider"] == "mock" else settings["provider"] + ":" + settings["model"]
        cursor = self._db.execute(
            "INSERT INTO attempts (revision_id, settings_json, settings_fingerprint, policy_version, state, "
            "status, owner, next_action, response_source, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', 'pending', 'IT Service Desk', 'wait-for-extraction', ?, ?)",
            (revision_id, encoded, fingerprint, policy_version, response_source, _now()),
        )
        attempt_id = cursor.lastrowid
        self._db.execute(
            "UPDATE documents SET current_revision_id = ?, current_attempt_id = ? WHERE document_id = ?",
            (revision_id, attempt_id, document_id),
        )
        return ScheduledAttempt(document_id, revision_id, attempt_id, True)

    def _enqueue(self, document_id: str, raw: bytes, prepared: tuple[str, str, str]) -> ScheduledAttempt:
        source_hash = hashlib.sha256(raw).hexdigest()
        current = self._current_optional(document_id)
        if current is not None and current["source_sha256"] == source_hash and current["settings_fingerprint"] == prepared[1]:
            return ScheduledAttempt(document_id, current["revision_id"], current["attempt_id"], current["state"] == "pending")
        self._supersede(current)
        self._db.execute("INSERT INTO documents (document_id) VALUES (?) ON CONFLICT(document_id) DO NOTHING", (document_id,))
        cursor = self._db.execute(
            "INSERT INTO revisions (document_id, source_bytes, source_sha256, settings_json, settings_fingerprint, "
            "policy_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (document_id, raw, source_hash, *prepared, _now()),
        )
        return self._new_attempt(document_id, cursor.lastrowid, prepared)

    def enqueue(self, document_id: str, raw: bytes, settings: dict[str, Any]) -> ScheduledAttempt:
        if not isinstance(document_id, str) or not document_id or document_id in (".", "..") or "/" in document_id or "\x00" in document_id:
            raise StateError("Document identity must be a nonempty input filename.")
        if not isinstance(raw, bytes):
            raise StateError("Source content must be original file bytes.")
        prepared = _prepare_settings(settings)
        with self._write():
            return self._enqueue(document_id, raw, prepared)

    def _require_current(self, attempt_id: int, state: str) -> dict[str, Any]:
        attempt = self.attempt(attempt_id)
        current = self.current(attempt["document_id"])
        if current["attempt_id"] != attempt_id or attempt["state"] != state:
            raise StateError("Attempt is no longer current or is not in the required processing state.")
        return attempt

    def mark_running(self, attempt_id: int) -> None:
        with self._write():
            self._require_current(attempt_id, "pending")
            self._db.execute(
                "UPDATE attempts SET state = 'running', status = 'processing', started_at = ? WHERE attempt_id = ?",
                (_now(), attempt_id),
            )

    def finish(self, attempt_id: int, record: dict[str, Any]) -> None:
        with self._write():
            attempt = self._require_current(attempt_id, "running")
            if (
                not isinstance(record, dict)
                or record.get("document_id") != attempt["document_id"]
                or record.get("source_sha256") != attempt["source_sha256"]
            ):
                raise StateError("Completion does not match the attempt's document and original source hash.")
            route = (record.get("status"), record.get("owner"), record.get("next_action"))
            failure = record.get("status") == "error"
            if failure:
                error = record.get("error")
                if (
                    route != ("error", "IT Service Desk", "retry-extraction")
                    or record.get("extraction") is not None
                    or not isinstance(error, dict)
                    or any(not isinstance(error.get(key), str) or not error[key].strip() for key in ("category", "message", "retry_command"))
                ):
                    raise StateError("Failure completion requires a null extraction and an actionable error route.")
            elif route not in _ROUTES or not isinstance(record.get("extraction"), dict) or record.get("error") is not None:
                raise StateError("Successful completion requires a validated extraction and a human review route.")
            source = record.get("response_source")
            if not isinstance(source, str) or not source:
                raise StateError("Completion must identify its response source.")
            extraction_json = _json(record["extraction"]) if record.get("extraction") is not None else None
            error_json = _json(record["error"]) if record.get("error") is not None else None
            self._db.execute(
                "UPDATE attempts SET state = ?, status = ?, owner = ?, next_action = ?, response_source = ?, "
                "extraction_json = ?, error_json = ?, finished_at = ? WHERE attempt_id = ?",
                ("failed" if failure else "succeeded", *route, source, extraction_json, error_json, _now(), attempt_id),
            )

    def retry(self, document_id: str) -> ScheduledAttempt:
        with self._write():
            current = self.current(document_id)
            if current["state"] in ("pending", "running"):
                raise StateError("Document already has unfinished work; resume it instead of retrying.")
            prepared = _prepare_settings(current["settings"])
            return self._new_attempt(document_id, current["revision_id"], prepared)

    def refresh(self, settings: dict[str, Any], document_id: str | None = None) -> list[ScheduledAttempt]:
        prepared = _prepare_settings(settings)
        with self._write():
            query = (
                "SELECT d.document_id, d.current_attempt_id, r.source_bytes FROM documents d "
                "JOIN revisions r ON r.revision_id = d.current_revision_id"
            )
            if document_id is not None:
                rows = self._db.execute(query + " WHERE d.document_id = ?", (document_id,)).fetchall()
                if not rows:
                    raise StateError("No stored document exists to refresh.")
            else:
                rows = self._db.execute(query + " ORDER BY d.document_id").fetchall()
            jobs = []
            for row in rows:
                job = self._enqueue(row["document_id"], bytes(row["source_bytes"]), prepared)
                if job.attempt_id != row["current_attempt_id"]:
                    jobs.append(job)
            return jobs

    def configure_attempt(self, attempt_id: int, settings: dict[str, Any]) -> None:
        """Record effective mock fixtures/operational settings before dispatch."""
        prepared = _prepare_settings(settings)
        with self._write():
            attempt = self._require_current(attempt_id, "pending")
            for field in _CACHE_FIELDS:
                if field != "mock_fixture_hashes" and settings.get(field) != attempt["settings"].get(field):
                    raise StateError("Generation or policy changes require refresh, not a retry configuration update.")
            self._db.execute(
                "UPDATE attempts SET settings_json = ?, settings_fingerprint = ?, policy_version = ? WHERE attempt_id = ?",
                (*prepared, attempt_id),
            )

    def recover_running(self) -> None:
        """Called only after acquiring the process lock: previous writers are gone."""
        with self._write():
            for current in self.queue():
                if current["state"] != "running":
                    continue
                error = {
                    "category": "interrupted_response",
                    "message": "Previous processing stopped before this attempt was committed; resume unfinished work.",
                    "retry_command": shlex.join(["it-access", "--db", str(self.path), "retry", current["document_id"]]),
                }
                self._db.execute(
                    "UPDATE attempts SET state = 'interrupted', status = 'error', owner = 'IT Service Desk', "
                    "next_action = 'retry-extraction', error_json = ?, finished_at = ? WHERE attempt_id = ?",
                    (_json(error), _now(), current["attempt_id"]),
                )

    def resume_jobs(self) -> list[ScheduledAttempt]:
        with self._write():
            jobs = []
            for current in self.queue():
                if current["state"] == "pending":
                    jobs.append(ScheduledAttempt(current["document_id"], current["revision_id"], current["attempt_id"], True))
                elif current["state"] == "interrupted":
                    jobs.append(self._new_attempt(current["document_id"], current["revision_id"], _prepare_settings(current["settings"])))
            return jobs
