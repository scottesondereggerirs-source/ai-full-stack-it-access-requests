"""Step 4 / T-27 through T-33: real CLI processes, recovery and pacing.

The CLI exposes main(argv) and python -m it_access. JSON queue output is a list
of current records; show --json is {"current": record}, adding "history" only
with --history. Processing commands use the plan's options and exit codes.

Tests run actual commands through cli_process.py, which blocks network/key
access and gates existing boundaries without adding product test switches.
StartGate(interval, clock=monotonic, sleep=sleep).wait() serializes start pacing;
it is the only newly specified runner helper, tested with a fake clock.
"""

from collections import Counter
from contextlib import ExitStack
import hashlib
import importlib
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from test_mock_provider import CredentialsForbidden, MOCKS, ROOT


DEFAULT_DB = object()


class OperatorCliContract(unittest.TestCase):
    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.contexts.enter_context(mock.patch.object(os, "environ", CredentialsForbidden()))
        for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection")):
            guard = self.contexts.enter_context(mock.patch.object(owner, name, side_effect=AssertionError("network forbidden")))
            self.addCleanup(guard.assert_not_called)
        self.directory = Path(self.contexts.enter_context(tempfile.TemporaryDirectory()))
        self.database = self.directory / "intake.sqlite3"
        self.inputs = self.directory / "inputs"
        self.fixtures = self.directory / "fixtures"
        self.trace = self.directory / "controls"
        self.children = []
        self.addCleanup(self.close_children)
        facts = json.loads((ROOT / "tests/expected/facts.json").read_text(encoding="utf-8"))
        self.fields = facts["scalar_fields"]
        self.expected = {row["document_id"]: row for row in facts["documents"]}
        self.inputs.mkdir()
        self.fixtures.mkdir()
        # Only small saved JSON fixtures are copied; no real provider or archive.
        for path in MOCKS.glob("*.json"):
            shutil.copyfile(path, self.fixtures / path.name)
        self.manifest = json.loads((self.fixtures / "manifest.json").read_text(encoding="utf-8"))
        # The expected red failure until the step-4 test-first commit is recorded.
        self.cli = importlib.import_module("it_access.cli")

    def close_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def add_inputs(self, documents, directory=None):
        target = directory or self.inputs
        target.mkdir(parents=True, exist_ok=True)
        for document_id in documents:
            shutil.copyfile(ROOT / self.expected[document_id]["source_path"], target / document_id)
        return target

    def map_reply(self, document_id, filename, raw=None):
        entries = self.manifest["fixtures"]
        entry = next((item for item in entries if item["document_id"] == document_id), None)
        if entry is None:
            self.assertIsNotNone(raw)
            entry = {"document_id": document_id}
            entries.append(entry)
        if raw is not None:
            entry.update(
                source_sha256=hashlib.sha256(raw).hexdigest(),
                message_sha256=hashlib.sha256(json.loads(raw)["message"].encode("utf-8")).hexdigest(),
            )
        entry["response_file"] = filename
        (self.fixtures / "manifest.json").write_text(json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8")

    def processing_options(self, workers=2, model=None):
        options = ["--provider", "mock", "--mock-dir", str(self.fixtures), "--workers", str(workers), "--min-interval", "0"]
        if model is not None:
            options.extend(["--model", model])
        return options

    def command(self, arguments, *, db=DEFAULT_DB, trace=None, phase=None, document="IT-001.json"):
        command = [sys.executable, str(ROOT / "tests/cli_process.py"), "--control-dir", str(trace or self.trace)]
        if phase is not None:
            command.extend(["--hold-phase", phase, "--hold-document", document])
        command.append("--")
        if db is not None:
            command.extend(["--db", str(self.database if db is DEFAULT_DB else db)])
        command.extend(str(argument) for argument in arguments)
        return command

    @staticmethod
    def child_environment():
        # Construct a clean mapping rather than copying the actual environment.
        return {
            "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(ROOT / "tests"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def run_cli(self, *arguments, db=DEFAULT_DB, trace=None, expected=0):
        result = subprocess.run(
            self.command(arguments, db=db, trace=trace), cwd=ROOT,
            env=self.child_environment(), capture_output=True, text=True, timeout=12,
        )
        self.assertEqual(result.returncode, expected, result.stdout + "\n" + result.stderr)
        return result

    def start_cli(self, *arguments, db=DEFAULT_DB, trace=None, phase="provider", document="IT-001.json"):
        child = subprocess.Popen(
            self.command(arguments, db=db, trace=trace, phase=phase, document=document),
            cwd=ROOT, env=self.child_environment(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        self.children.append(child)
        return child

    def wait_ready(self, child, trace=None):
        marker = (trace or self.trace) / "reached.json"
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if marker.exists():
                try:
                    return json.loads(marker.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            if child.poll() is not None:
                stdout, stderr = child.communicate()
                self.fail("CLI exited before its checkpoint:\n" + stdout + "\n" + stderr)
            time.sleep(0.01)
        self.fail("CLI did not reach the deterministic test checkpoint")

    def release(self, child, trace=None, expected=0):
        (trace or self.trace).mkdir(parents=True, exist_ok=True)
        ((trace or self.trace) / "release").touch()
        stdout, stderr = child.communicate(timeout=12)
        self.assertEqual(child.returncode, expected, stdout + "\n" + stderr)

    def queue(self, *, db=DEFAULT_DB, trace=None):
        result = self.run_cli("queue", "--json", db=db, trace=trace)
        rows = json.loads(result.stdout)
        self.assertIsInstance(rows, list)
        return {row["document_id"]: row for row in rows}

    def wait_queue(self, child, predicate, *, db=DEFAULT_DB, trace=None):
        deadline = time.monotonic() + 12
        last = None
        while time.monotonic() < deadline:
            last = self.queue(db=db, trace=trace)
            if predicate(last):
                return last
            if child.poll() is not None:
                stdout, stderr = child.communicate()
                self.fail("CLI exited before expected partial progress:\n" + stdout + "\n" + stderr)
            time.sleep(0.01)
        self.fail("Expected partial progress was not visible: " + repr(last))

    def events(self, trace=None):
        path = (trace or self.trace) / "provider-events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def calls(self, trace=None):
        return Counter(event["document_id"] for event in self.events(trace) if event["kind"] == "provider_call")

    def assert_row(self, row, document_id):
        expected = self.expected[document_id]
        self.assertEqual(row["document_id"], document_id)
        self.assertEqual(row["source_sha256"], expected["source_sha256"])
        self.assertEqual({field: row["extraction"][field] for field in self.fields}, expected["fields"])
        self.assertIsNone(row["error"])
        self.assertEqual(row["state"], "succeeded")
        for field in ("status", "owner", "next_action"):
            self.assertEqual(row[field], expected[field])
        self.assertEqual(row["response_source"], "mock")
        self.assertTrue(row["settings_fingerprint"])
        self.assertEqual(row["policy_version"], hashlib.sha256((ROOT / "materials/office-policy.md").read_bytes()).hexdigest())

    def test_T_27_cli_intake_queue(self):
        self.assertTrue(callable(self.cli.main))
        self.add_inputs(("IT-001.json", "IT-002.json"))
        self.run_cli("intake", self.inputs, *self.processing_options())
        rows = self.queue()
        self.assertEqual(set(rows), {"IT-001.json", "IT-002.json"})
        for document_id, row in rows.items():
            self.assert_row(row, document_id)
        text = self.run_cli("queue").stdout
        for value in ("IT-001.json", "IT-002.json", "System Owner", "Security Review", "review-request", "review-risk-then-owner"):
            self.assertIn(value, text)
        shown = json.loads(self.run_cli("show", "IT-002.json", "--history", "--json").stdout)
        self.assertEqual(shown["current"], rows["IT-002.json"])
        self.assertEqual(shown["history"], [])
        self.assertEqual(len(json.loads(self.run_cli("queue", "--status", "review", "--json").stdout)), 2)
        self.run_cli("show", "does-not-exist.json", expected=2)
        self.run_cli("intake", self.inputs, *self.processing_options())
        repeated = self.queue()
        self.assertEqual({key: row["attempt_id"] for key, row in repeated.items()}, {key: row["attempt_id"] for key, row in rows.items()})
        self.assertEqual(self.calls(), {"IT-001.json": 1, "IT-002.json": 1})

    def test_T_28_partial_progress(self):
        self.add_inputs(("IT-001.json", "IT-002.json", "IT-012.json"))
        self.map_reply("IT-001.json", "delayed-IT-001.json")
        child = self.start_cli("intake", self.inputs, *self.processing_options())
        self.wait_ready(child)
        rows = self.wait_queue(child, lambda rows: rows.get("IT-012.json", {}).get("state") == "succeeded")
        self.assertEqual(rows["IT-001.json"]["state"], "running")
        self.assertIsNone(rows["IT-001.json"]["extraction"])
        self.assert_row(rows["IT-002.json"], "IT-002.json")
        self.assert_row(rows["IT-012.json"], "IT-012.json")
        self.assertIsNone(child.poll())
        self.release(child)
        for document_id, row in self.queue().items():
            self.assert_row(row, document_id)

    def test_T_29_overlapping_operators(self):
        self.add_inputs(("IT-001.json", "IT-002.json"))
        child = self.start_cli("intake", self.inputs, *self.processing_options())
        self.wait_ready(child)
        before = self.queue()
        separate = self.add_inputs(("IT-006.json",), self.directory / "later-inputs")
        alias = self.directory / "database-alias.sqlite3"
        alias.symlink_to(self.database)
        for arguments, database in (
            (("intake", separate, *self.processing_options()), DEFAULT_DB),
            (("intake", separate, *self.processing_options()), alias),
            (("resume",), DEFAULT_DB),
            (("retry", "IT-001.json"), DEFAULT_DB),
            (("refresh", *self.processing_options()), DEFAULT_DB),
        ):
            with self.subTest(command=arguments[0], alias=database == alias):
                busy = self.run_cli(*arguments, db=database, expected=3)
                self.assertIn("busy", (busy.stdout + busy.stderr).lower())
        current = self.queue()
        self.assertEqual(set(current), set(before))
        self.assertEqual(current["IT-001.json"]["attempt_id"], before["IT-001.json"]["attempt_id"])
        lock_path = Path(str(self.database.resolve()) + ".lock")
        self.assertTrue(lock_path.exists())
        self.release(child)
        self.assertTrue(lock_path.exists(), "release the OS lock without deleting its shared inode")
        self.run_cli("intake", separate, *self.processing_options())
        rows = self.queue()
        self.assertEqual(set(rows), {"IT-001.json", "IT-002.json", "IT-006.json"})
        self.assert_row(rows["IT-006.json"], "IT-006.json")

    def test_T_30_restart_resume(self):
        self.add_inputs(("IT-001.json", "IT-002.json", "IT-012.json"))
        raw = (self.inputs / "IT-001.json").read_bytes()
        # A terminal failure precedes the checkpoint and must not be retried by resume.
        self.map_reply("IT-000.json", "provider-unavailable.json", raw=raw)
        for phase in ("before_dispatch", "provider", "after_response", "after_commit"):
            with self.subTest(checkpoint=phase):
                folder = self.directory / phase
                inputs = folder / "inputs"
                self.add_inputs(("IT-001.json", "IT-002.json", "IT-012.json"), inputs)
                (inputs / "IT-000.json").write_bytes(raw)
                database = folder / "state.sqlite3"
                trace = folder / "controls"
                child = self.start_cli(
                    "intake", inputs, *self.processing_options(workers=1, model="recorded-model"),
                    db=database, trace=trace, phase=phase, document="IT-002.json",
                )
                self.wait_ready(child, trace)
                before = self.queue(db=database, trace=trace)
                self.assertEqual(set(before), {"IT-000.json", "IT-001.json", "IT-002.json", "IT-012.json"})
                self.assertEqual(before["IT-000.json"]["state"], "failed")
                self.assert_row(before["IT-001.json"], "IT-001.json")
                self.assertEqual(before["IT-012.json"]["state"], "pending")
                target_state = "pending" if phase == "before_dispatch" else "succeeded" if phase == "after_commit" else "running"
                self.assertEqual(before["IT-002.json"]["state"], target_state)
                child.kill()
                child.communicate(timeout=5)
                # Remove the original input files: resume must use captured bytes.
                for path in inputs.glob("*.json"):
                    path.unlink()
                self.run_cli("resume", db=database, trace=trace)
                after = self.queue(db=database, trace=trace)
                self.assertEqual(after["IT-000.json"]["attempt_id"], before["IT-000.json"]["attempt_id"])
                self.assertEqual(after["IT-000.json"]["state"], "failed")
                self.assertEqual(after["IT-001.json"]["attempt_id"], before["IT-001.json"]["attempt_id"])
                for document_id in ("IT-001.json", "IT-002.json", "IT-012.json"):
                    self.assert_row(after[document_id], document_id)
                    self.assertEqual(after[document_id]["settings"]["model"], "recorded-model")
                shown = json.loads(self.run_cli("show", "IT-002.json", "--history", "--json", db=database, trace=trace).stdout)
                if phase in ("provider", "after_response"):
                    self.assertNotEqual(after["IT-002.json"]["attempt_id"], before["IT-002.json"]["attempt_id"])
                    self.assertEqual(len(shown["history"]), 1)
                    self.assertEqual(shown["history"][0]["state"], "interrupted")
                else:
                    self.assertEqual(after["IT-002.json"]["attempt_id"], before["IT-002.json"]["attempt_id"])
                    self.assertEqual(shown["history"], [])
                counts = self.calls(trace)
                self.assertEqual(counts["IT-000.json"], 1)
                self.assertEqual(counts["IT-001.json"], 1)
                self.assertEqual(counts["IT-012.json"], 1)
                self.assertEqual(counts["IT-002.json"], 2 if phase in ("provider", "after_response") else 1)
                self.run_cli("resume", db=database, trace=trace)
                self.assertEqual(self.calls(trace), counts)

    def test_T_31_failure_isolation_retry(self):
        self.add_inputs(("IT-001.json", "IT-002.json", "IT-012.json", "IT-018.json"))
        for document_id, filename in (("IT-001.json", "timeout.json"), ("IT-002.json", "rate-limit.json"), ("IT-012.json", "provider-unavailable.json")):
            self.map_reply(document_id, filename)
        self.run_cli("intake", self.inputs, *self.processing_options(), expected=1)
        before = self.queue()
        self.assert_row(before["IT-018.json"], "IT-018.json")
        for document_id, category in (("IT-001.json", "timeout"), ("IT-002.json", "rate_limit"), ("IT-012.json", "provider_unavailable")):
            self.assertEqual(before[document_id]["state"], "failed")
            self.assertEqual(before[document_id]["status"], "error")
            self.assertIsNone(before[document_id]["extraction"])
            self.assertEqual(before[document_id]["error"]["category"], category)
        self.assertEqual(before["IT-002.json"]["error"]["retry_after"], 2)
        counts = self.calls()
        self.assertEqual(counts, {document_id: 1 for document_id in before})
        self.run_cli("intake", self.inputs, *self.processing_options(), expected=1)
        self.assertEqual(self.calls(), counts, "an unchanged failed rerun is not an automatic retry")
        self.map_reply("IT-001.json", "IT-001.json")
        command = shlex.split(before["IT-001.json"]["error"]["retry_command"])
        self.assertEqual(command[0], "it-access")
        self.assertIn("--db", command)
        self.assertEqual(Path(command[command.index("--db") + 1]).resolve(), self.database.resolve())
        self.run_cli(*command[1:], db=None)
        after = self.queue()
        self.assert_row(after["IT-001.json"], "IT-001.json")
        self.assertEqual(after["IT-001.json"]["revision_id"], before["IT-001.json"]["revision_id"])
        self.assertNotEqual(after["IT-001.json"]["settings_fingerprint"], before["IT-001.json"]["settings_fingerprint"])
        for document_id in ("IT-002.json", "IT-012.json", "IT-018.json"):
            self.assertEqual(after[document_id]["attempt_id"], before[document_id]["attempt_id"])
        history = json.loads(self.run_cli("show", "IT-001.json", "--history", "--json").stdout)["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["error"]["category"], "timeout")
        self.assertEqual(history[0]["settings_fingerprint"], before["IT-001.json"]["settings_fingerprint"])
        self.assertEqual(self.calls()["IT-001.json"], 2)

    def test_T_32_pacing_limits(self):
        runner = importlib.import_module("it_access.runner")

        class Clock:
            def __init__(self):
                self.time = 100.0
                self.sleeps = []

            def now(self):
                return self.time

            def sleep(self, duration):
                self.sleeps.append(duration)
                self.time += duration

        clock = Clock()
        gate = runner.StartGate(2.0, clock=clock.now, sleep=clock.sleep)
        gate.wait()
        self.assertEqual(clock.time, 100.0)
        gate.wait()
        self.assertEqual(clock.time, 102.0)
        clock.time += 5
        gate.wait()
        self.assertEqual(clock.time, 107.0)
        gate.wait()
        self.assertEqual(clock.time, 109.0)
        self.assertEqual([delay for delay in clock.sleeps if delay], [2.0, 2.0])

        self.add_inputs(("IT-001.json", "IT-002.json", "IT-012.json"))
        for workers in (1, 2):
            with self.subTest(workers=workers):
                database = self.directory / f"workers-{workers}.sqlite3"
                trace = self.directory / f"workers-{workers}"
                child = self.start_cli("intake", self.inputs, *self.processing_options(workers=workers), db=database, trace=trace)
                self.wait_ready(child, trace)
                if workers == 1:
                    rows = self.queue(db=database, trace=trace)
                    self.assertEqual(rows["IT-002.json"]["state"], "pending")
                    self.assertEqual(rows["IT-012.json"]["state"], "pending")
                    self.assertEqual(self.calls(trace), {"IT-001.json": 1})
                else:
                    self.wait_queue(child, lambda rows: rows.get("IT-012.json", {}).get("state") == "succeeded", db=database, trace=trace)
                self.release(child, trace)
                active = maximum = 0
                for event in self.events(trace):
                    active += 1 if event["kind"] == "provider_call" else -1
                    maximum = max(maximum, active)
                    self.assertGreaterEqual(active, 0)
                self.assertEqual(active, 0)
                self.assertEqual(maximum, workers)

    def test_T_33_input_refresh_cli(self):
        missing_database = self.directory / "absent" / "state.sqlite3"
        self.run_cli("queue", "--json", db=missing_database, expected=2)
        self.assertFalse(missing_database.exists())
        self.assertFalse(missing_database.parent.exists())
        self.run_cli("show", "IT-001.json", "--json", db=missing_database, expected=2)
        empty = self.directory / "empty"
        empty.mkdir()
        for path in (empty, self.directory / "no-input-directory"):
            self.run_cli("intake", path, *self.processing_options(), expected=2)
            self.assertFalse(self.database.exists())
        self.add_inputs(("IT-001.json",))
        sentinel = "NEVER_ECHO_TEST_PASSWORD"
        invalid_options = [
            ("--workers", "0"), ("--workers", "3"),
            ("--timeout", "0"), ("--timeout", "-1"), ("--timeout", "nan"), ("--timeout", "inf"),
            ("--min-interval", "-1"), ("--min-interval", "nan"), ("--min-interval", "inf"),
            ("--endpoint", f"https://user:{sentinel}@provider.example.test/v1"),
            ("--endpoint", f"https://provider.example.test/v1?api_key={sentinel}"),
        ]
        for options in invalid_options:
            with self.subTest(invalid=options[0], value=options[1]):
                result = self.run_cli("intake", self.inputs, *self.processing_options(), *options, expected=2)
                self.assertNotIn(sentinel, result.stdout + result.stderr)
                self.assertFalse(self.database.exists())
                self.assertEqual(self.calls(), {})

        bad_sources = {
            "BAD-001.json": b"\xff\xfe",
            "BAD-002.json": b'{"message":',
            "BAD-003.json": b'{"subject":"No message"}',
            "BAD-004.json": b'{"message":17}',
            "BAD-005.json": b'{"message":"first","message":"second"}',
        }
        for filename, raw in bad_sources.items():
            (self.inputs / filename).write_bytes(raw)
        self.run_cli("intake", self.inputs, *self.processing_options(), expected=1)
        rows = self.queue()
        self.assertEqual(set(rows), set(bad_sources) | {"IT-001.json"})
        self.assert_row(rows["IT-001.json"], "IT-001.json")
        for filename, raw in bad_sources.items():
            self.assertEqual(rows[filename]["source_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(rows[filename]["error"]["category"], "input")
            self.assertIsNone(rows[filename]["extraction"])
        self.assertEqual(self.calls(), {"IT-001.json": 1})
        self.run_cli("intake", self.inputs, *self.processing_options(), expected=1)
        repeated = self.queue()
        self.assertEqual({key: row["attempt_id"] for key, row in repeated.items()}, {key: row["attempt_id"] for key, row in rows.items()})
        self.assertEqual(self.calls(), {"IT-001.json": 1})
        for command in (("retry", "missing.json"), ("show", "missing.json"), ("refresh", "--document", "missing.json", *self.processing_options()), ("queue", "--status", "not-a-status")):
            self.run_cli(*command, expected=2)

        # Use a clean separate store to verify refresh of an omitted source.
        refresh_db = self.directory / "refresh.sqlite3"
        refresh_inputs = self.add_inputs(("IT-001.json", "IT-002.json"), self.directory / "refresh-inputs")
        trace = self.directory / "refresh-controls"
        self.run_cli("intake", refresh_inputs, *self.processing_options(), db=refresh_db, trace=trace)
        (refresh_inputs / "IT-002.json").unlink()
        self.run_cli("intake", refresh_inputs, *self.processing_options(), db=refresh_db, trace=trace)
        self.assertEqual(self.calls(trace), {"IT-001.json": 1, "IT-002.json": 1})
        self.run_cli("refresh", *self.processing_options(model="refresh-model"), db=refresh_db, trace=trace)
        refreshed = self.queue(db=refresh_db, trace=trace)
        for document_id, row in refreshed.items():
            self.assert_row(row, document_id)
            self.assertEqual(row["settings"]["model"], "refresh-model")
        self.assertEqual(self.calls(trace), {"IT-001.json": 2, "IT-002.json": 2})
        self.run_cli("refresh", *self.processing_options(workers=1, model="refresh-model"), db=refresh_db, trace=trace)
        self.assertEqual(self.calls(trace), {"IT-001.json": 2, "IT-002.json": 2})


if __name__ == "__main__":
    unittest.main()
