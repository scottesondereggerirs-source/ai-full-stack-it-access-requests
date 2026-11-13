"""One admitted writer, bounded provider calls, and durable per-message results."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import copy
import fcntl
import hashlib
import json
import math
from pathlib import Path
import shlex
import threading
import time
from typing import Any

from . import extraction
from .providers import MockProvider, ProviderConfig, ProviderError
from .state import ScheduledAttempt, StateError, StateStore


class BusyError(StateError):
    pass


class WriterLock:
    def __init__(self, database: Path):
        self.path = Path(str(Path(database).resolve()) + ".lock")
        self._stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+b")
        try:
            fcntl.flock(self._stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._stream.close()
            self._stream = None
            raise BusyError("Intake is busy with another operator; retry the command after that process finishes.") from error
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._stream is not None:
            fcntl.flock(self._stream, fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None
        # Keep the inode: removing a shared lock file permits competing locks.


class StartGate:
    def __init__(self, interval: float, *, clock=time.monotonic, sleep=time.sleep):
        if not math.isfinite(interval) or interval < 0:
            raise StateError("Start interval must be a finite nonnegative number.")
        self.interval = interval
        self.clock = clock
        self.sleep = sleep
        self._next = None
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = self.clock()
            if self._next is not None and now < self._next:
                self.sleep(self._next - now)
                now = self.clock()
            self._next = now + self.interval


def provider_for(settings: dict[str, Any]):
    if settings["provider"] == "mock":
        return MockProvider(Path(settings["mock_dir"]))
    if settings["provider"] == "openai":
        from .live_provider import OpenAIProvider
        return OpenAIProvider()
    raise StateError("The live provider is not available in this build; use mock mode.")


def capture_settings(config: ProviderConfig, mock_dir: Path, workers: int, interval: float):
    settings = {
        "provider": config.provider,
        "endpoint": config.endpoint,
        "model": config.model,
        "generation": {"temperature": config.temperature, "max_output_tokens": config.max_output_tokens, "store": config.store},
        "system_prompt": extraction.build_messages("")[0]["content"],
        "schema_sha256": hashlib.sha256(extraction.SCHEMA_PATH.read_bytes()).hexdigest(),
        "extraction_schema": json.loads(extraction.SCHEMA_PATH.read_text(encoding="utf-8")),
        "policy_sha256": hashlib.sha256(extraction.POLICY_PATH.read_bytes()).hexdigest(),
        "routing_version": extraction.ROUTING_VERSION,
        "mock_dir": str(mock_dir.resolve()),
        "mock_fixture_hashes": {},
        "timeout_seconds": config.timeout_seconds,
        "workers": workers,
        "min_interval": interval,
    }
    settings["prompt_sha256"] = hashlib.sha256(settings["system_prompt"].encode("utf-8")).hexdigest()
    provider = provider_for(settings)
    if isinstance(provider, MockProvider):
        settings["mock_fixture_hashes"] = provider.fixture_hashes()
    return settings, provider


def _parse_export(raw: bytes) -> str:
    try:
        export = json.loads(
            raw.decode("utf-8"), object_pairs_hook=extraction._unique_object,
            parse_constant=extraction._invalid_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ProviderError("Input export must be unambiguous UTF-8 JSON with a message string.", "input") from error
    if not isinstance(export, dict) or not isinstance(export.get("message"), str):
        raise ProviderError("Input export is missing a message string; correct the export and retry.", "input")
    return export["message"]


def _failure(document_id, source_hash, response_source, database, category, message, retry_after=None):
    error = {
        "category": category,
        "message": message,
        "retry_command": shlex.join(["it-access", "--db", str(database), "retry", document_id]),
    }
    if retry_after is not None:
        error["retry_after"] = retry_after
    return {
        "document_id": document_id, "source_sha256": source_hash,
        "response_source": response_source, "extraction": None,
        "status": "error", "owner": "IT Service Desk",
        "next_action": "retry-extraction", "error": error,
    }


def _process_one(document_id, raw, settings, provider, gate, database):
    source_hash = hashlib.sha256(raw).hexdigest()
    response_source = "mock" if settings["provider"] == "mock" else settings["provider"] + ":" + settings["model"]
    try:
        message = _parse_export(raw)
        # An old generation prompt can be retained, but a different validation
        # schema or routing build must be explicitly refreshed, not mislabeled.
        if (
            settings["schema_sha256"] != hashlib.sha256(extraction.SCHEMA_PATH.read_bytes()).hexdigest()
            or settings["policy_sha256"] != hashlib.sha256(extraction.POLICY_PATH.read_bytes()).hexdigest()
            or settings["routing_version"] != extraction.ROUTING_VERSION
        ):
            raise ProviderError("Recorded policy/schema differs from this build; refresh the document before retrying.", "validation")
        config = ProviderConfig(
            provider=settings["provider"], model=settings["model"], endpoint=settings["endpoint"],
            timeout_seconds=settings["timeout_seconds"], **settings["generation"],
            system_prompt=settings.get("system_prompt"), extraction_schema=settings.get("extraction_schema"),
        )
        gate.wait()
        response = provider.extract(document_id=document_id, source_sha256=source_hash, message=message, config=config)
        record = extraction.process_response(document_id=document_id, source_sha256=source_hash, message=message, response=response)
        if record["error"] is not None:
            record["error"]["retry_command"] = shlex.join(["it-access", "--db", str(database), "retry", document_id])
        return record
    except ProviderError as error:
        return _failure(document_id, source_hash, response_source, database, error.category, str(error), error.retry_after)
    except Exception:
        # Never place arbitrary transport/model exceptions into operational logs.
        return _failure(document_id, source_hash, response_source, database, "provider_unavailable", "Extraction could not complete; inspect configuration and retry this message.")


def run_jobs(store: StateStore, jobs: list[ScheduledAttempt], *, providers=None):
    selected = [job for job in jobs if job.needs_processing]
    if not selected:
        return
    recorded = {job.attempt_id: store.attempt(job.attempt_id)["settings"] for job in selected}
    # Conservative limits when resume combines jobs from different old batches.
    workers = min(int(settings.get("workers", 2)) for settings in recorded.values())
    if workers not in (1, 2):
        raise StateError("Recorded worker limit must be 1 or 2.")
    interval = max(float(settings.get("min_interval", 1.0)) for settings in recorded.values())
    gate = StartGate(interval)
    providers = dict(providers or {})
    remaining = iter(selected)
    active = {}

    def submit(pool, job):
        settings = copy.deepcopy(recorded[job.attempt_id])
        key = (settings["provider"], settings.get("mock_dir"), settings["endpoint"])
        if key not in providers:
            providers[key] = provider_for(settings)
        provider = providers[key]
        if isinstance(provider, MockProvider):
            settings["mock_fixture_hashes"] = provider.fixture_hashes()
        settings.update(workers=workers, min_interval=interval)
        store.configure_attempt(job.attempt_id, settings)
        raw = store.source_bytes(job.attempt_id)
        store.mark_running(job.attempt_id)
        future = pool.submit(_process_one, job.document_id, raw, settings, provider, gate, store.path)
        active[future] = job

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in range(workers):
            job = next(remaining, None)
            if job is not None:
                submit(pool, job)
        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                job = active.pop(future)
                result = future.result()
                store.finish(job.attempt_id, result)
                print(f"{job.document_id}\t{result['status']}\t{result['owner']}\t{result['next_action']}", flush=True)
                if result["error"] is not None:
                    print(result["error"]["message"] + "\n" + result["error"]["retry_command"], flush=True)
            for _ in range(workers - len(active)):
                job = next(remaining, None)
                if job is not None:
                    submit(pool, job)
