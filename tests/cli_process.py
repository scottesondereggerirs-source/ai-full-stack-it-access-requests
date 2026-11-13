"""Offline subprocess harness for step 4 tests; never a product entry point.

Runs the actual python -m it_access command with connection/key-access guards.
Optional filesystem gates pause existing provider/store boundaries. There are
no test-only switches in the product CLI or environment configuration.
"""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import runpy
import socket
import sys
import threading
import time
from unittest import mock

from test_mock_provider import CredentialsForbidden, ROOT


class Controls:
    def __init__(self, directory, phase, document_id):
        self.directory = directory
        self.phase = phase
        self.document_id = document_id
        self.used = False
        self.lock = threading.Lock()
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)

    def event(self, kind, document_id, **extra):
        if self.directory is None:
            return
        event = {"kind": kind, "document_id": document_id, "time": time.monotonic(), **extra}
        with self.lock:
            with (self.directory / "provider-events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event) + "\n")
                stream.flush()

    def pause(self, phase, document_id):
        if self.directory is None or phase != self.phase or document_id != self.document_id:
            return
        with self.lock:
            if self.used:
                return
            self.used = True
            marker = self.directory / "reached.json"
            marker.write_text(json.dumps({"phase": phase, "document_id": document_id}), encoding="utf-8")
        deadline = time.monotonic() + 25
        # Poll only a local test event. Pacing tests use an injected fake clock.
        while not (self.directory / "release").exists():
            if time.monotonic() >= deadline:
                raise AssertionError("test gate was not released before its deadline")
            threading.Event().wait(0.01)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-dir", type=Path)
    parser.add_argument("--hold-phase", choices=("before_dispatch", "provider", "after_response", "after_commit"))
    parser.add_argument("--hold-document", default="IT-001.json")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    controls = Controls(args.control_dir, args.hold_phase, args.hold_document)

    with ExitStack() as patches:
        patches.enter_context(mock.patch.object(os, "environ", CredentialsForbidden({
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONUNBUFFERED": "1",
        })))
        guards = [
            patches.enter_context(mock.patch.object(owner, name, side_effect=AssertionError("network forbidden in CLI test")))
            for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection"))
        ]
        from it_access.providers import MockProvider
        from it_access.state import StateStore

        real_extract = MockProvider.extract
        real_running = StateStore.mark_running
        real_finish = StateStore.finish

        def extract(provider, *, document_id, source_sha256, message, config):
            controls.event("provider_call", document_id, source_sha256=source_sha256)
            controls.pause("provider", document_id)
            try:
                response = real_extract(
                    provider, document_id=document_id, source_sha256=source_sha256,
                    message=message, config=config,
                )
            finally:
                controls.event("provider_returned", document_id)
            controls.pause("after_response", document_id)
            return response

        def mark_running(store, attempt_id):
            document_id = store.attempt(attempt_id)["document_id"]
            controls.pause("before_dispatch", document_id)
            return real_running(store, attempt_id)

        def finish(store, attempt_id, record):
            result = real_finish(store, attempt_id, record)
            controls.pause("after_commit", record["document_id"])
            return result

        patches.enter_context(mock.patch.object(MockProvider, "extract", extract))
        patches.enter_context(mock.patch.object(StateStore, "mark_running", mark_running))
        patches.enter_context(mock.patch.object(StateStore, "finish", finish))
        patches.enter_context(mock.patch.object(sys, "argv", ["it-access", *command]))
        try:
            runpy.run_module("it_access", run_name="__main__")
        except SystemExit as result:
            code = result.code or 0
        else:
            code = 0
        finally:
            for guard in guards:
                guard.assert_not_called()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
