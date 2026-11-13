"""Step 3 / T-20 through T-26: durable state and history, written first.

StateStore(path) is a context manager with close(), enqueue(document_id, raw,
settings), retry(document_id), refresh(settings, document_id=None),
mark_running(attempt_id), finish(attempt_id, terminal_record), current(id),
queue(), history(id), attempt(attempt_id), and source_bytes(attempt_id).

enqueue/retry return a job with document_id, revision_id, attempt_id and
needs_processing attributes. refresh returns only newly scheduled jobs. Views
include revision_id, attempt_id, state, settings, settings_fingerprint,
policy_version (the policy content hash), timestamps and the outer record fields.
History excludes the current attempt. StateError rejects invalid transitions or
cross-document/source completion without modifying the current view.

settings_fingerprint(settings) canonically hashes provider, endpoint, model,
prompt_sha256, schema_sha256, generation, policy_sha256, routing_version and
mock_fixture_hashes. Timeout, worker count and pacing are operational only.
Credential-bearing settings are rejected before persistence. No real API_KEY,
network, operator CLI or live provider is used here.

The small test harness injects saved provider responses/results at the store
boundary. Actual scheduling, locks and provider-failure orchestration are step 4.
"""

from contextlib import ExitStack, closing
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import shlex
import socket
import sqlite3
import tempfile
import unittest
from unittest import mock

from test_mock_provider import CredentialsForbidden, MOCKS, ROOT


class DurableStateContract(unittest.TestCase):
    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.contexts.enter_context(mock.patch.object(os, "environ", CredentialsForbidden()))
        for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection")):
            guard = self.contexts.enter_context(
                mock.patch.object(owner, name, side_effect=AssertionError("network forbidden"))
            )
            self.addCleanup(guard.assert_not_called)
        self.directory = Path(self.contexts.enter_context(tempfile.TemporaryDirectory()))
        self.database = self.directory / "intake.sqlite3"
        facts = json.loads((ROOT / "tests/expected/facts.json").read_text(encoding="utf-8"))
        self.fields = facts["scalar_fields"]
        self.documents = {item["document_id"]: item for item in facts["documents"]}
        variants = json.loads((ROOT / "tests/expected/step-03-cases.json").read_text(encoding="utf-8"))
        self.cases = {item["name"]: item for item in variants["cases"]}
        self.providers = importlib.import_module("it_access.providers")
        self.extraction = importlib.import_module("it_access.extraction")
        configuration = self.providers.ProviderConfig()
        manifest = json.loads((MOCKS / "manifest.json").read_text(encoding="utf-8"))
        self.settings = {
            "provider": "mock",
            "endpoint": configuration.endpoint,
            "model": configuration.model,
            "prompt_sha256": self.digest(self.extraction.build_messages("")[0]["content"].encode("utf-8")),
            "schema_sha256": self.digest((ROOT / "snapshot.schema.json").read_bytes()),
            "generation": {
                "temperature": configuration.temperature,
                "max_output_tokens": configuration.max_output_tokens,
                "store": configuration.store,
            },
            "policy_sha256": self.digest((ROOT / "materials/office-policy.md").read_bytes()),
            "routing_version": self.extraction.ROUTING_VERSION,
            "mock_fixture_hashes": {
                item["response_file"]: self.digest((MOCKS / item["response_file"]).read_bytes())
                for item in manifest["fixtures"]
            },
            "timeout_seconds": configuration.timeout_seconds,
            "workers": 2,
            "min_interval": 0.0,
        }
        self.responses = mock.Mock(side_effect=self.record_from_saved_response)
        # Expected red failure: no StateStore implementation exists yet.
        self.state = importlib.import_module("it_access.state")

    @staticmethod
    def digest(raw):
        return hashlib.sha256(raw).hexdigest()

    def raw(self, document_id="IT-001.json", case=None):
        expected = self.cases[case] if case else self.documents[document_id]
        return (ROOT / expected["source_path"]).read_bytes()

    def open_store(self, path=None):
        store = self.state.StateStore(path or self.database)
        self.addCleanup(store.close)
        return store

    def record_from_saved_response(self, document_id, raw, response_file):
        payload = json.loads((MOCKS / response_file).read_text(encoding="utf-8"))
        if "mock_error" in payload:
            failure = payload["mock_error"]
            return {
                "document_id": document_id,
                "source_sha256": self.digest(raw),
                "response_source": "mock",
                "extraction": None,
                "status": "error",
                "owner": "IT Service Desk",
                "next_action": "retry-extraction",
                "error": {
                    "category": failure["category"],
                    "message": failure["message"],
                    "retry_command": "it-access retry " + shlex.quote(document_id),
                },
            }
        return self.extraction.process_response(
            document_id=document_id,
            source_sha256=self.digest(raw),
            message=json.loads(raw)["message"],
            response=self.providers.ProviderResponse(payload=payload, response_source="mock"),
        )

    def execute(self, store, job, response_file):
        if job.needs_processing:
            store.mark_running(job.attempt_id)
            record = self.responses(job.document_id, store.source_bytes(job.attempt_id), response_file)
            store.finish(job.attempt_id, record)
        return store.current(job.document_id)

    def complete(self, store, document_id="IT-001.json", *, raw=None, settings=None, response_file=None):
        source = raw if raw is not None else self.raw(document_id)
        selected = settings if settings is not None else self.settings
        job = store.enqueue(document_id, source, selected)
        return self.execute(store, job, response_file or document_id)

    def assert_facts(self, row, expected):
        self.assertEqual(row["state"], "succeeded")
        self.assertIsNone(row["error"])
        self.assertEqual(row["document_id"], expected["document_id"])
        for field in ("status", "owner", "next_action"):
            self.assertEqual(row[field], expected[field], field)
        self.assertEqual({field: row["extraction"][field] for field in self.fields}, expected["fields"])
        self.assertEqual(row["response_source"], "mock")
        for code in expected["required_issue_codes"]:
            self.assertTrue(any(issue.startswith(code + ":") for issue in row["extraction"]["issues"]), code)

    def counts(self, database=None):
        with closing(sqlite3.connect(database or self.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            return tuple(
                connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in ("documents", "revisions", "attempts")
            )

    def test_T_20_identity_hash_history(self):
        raw = self.raw()
        store = self.open_store()
        initial = self.complete(store)
        self.assert_facts(initial, self.documents["IT-001.json"])
        self.assertEqual(initial["source_sha256"], self.digest(raw))
        self.assertEqual(store.source_bytes(initial["attempt_id"]), raw)
        self.assertEqual(store.history("IT-001.json"), [])
        self.assertTrue(initial["created_at"])
        self.assertTrue(initial["started_at"])
        self.assertTrue(initial["finished_at"])

        # JSON formatting is source content for hashing, not a business change.
        reformatted = (json.dumps(json.loads(raw), indent=3, ensure_ascii=False) + "\n").replace("\n", "\r\n").encode("utf-8")
        self.assertNotEqual(reformatted, raw)
        updated = self.complete(store, raw=reformatted)
        self.assert_facts(updated, self.documents["IT-001.json"])
        self.assertEqual(updated["source_sha256"], self.digest(reformatted))
        self.assertNotEqual(updated["revision_id"], initial["revision_id"])
        history = store.history("IT-001.json")
        self.assertEqual([row["attempt_id"] for row in history], [initial["attempt_id"]])
        self.assertEqual(history[0]["source_sha256"], self.digest(raw))
        self.assertEqual(store.source_bytes(initial["attempt_id"]), raw)

        # Same in-message ID never merges independently named documents.
        followup = self.complete(store, "followup.json", raw=raw, response_file="IT-001.json")
        self.assertEqual(followup["extraction"]["request_id"], "IT-001")
        self.assertEqual(followup["status"], "needs-information")
        self.assertEqual((followup["owner"], followup["next_action"]), ("IT Service Desk", "clarify"))
        self.assertEqual(followup["source_sha256"], self.digest(raw))
        self.assertTrue(any(issue.startswith("identifier-mismatch:") for issue in followup["extraction"]["issues"]))
        self.assertEqual([row["document_id"] for row in store.queue()], ["IT-001.json", "followup.json"])
        self.assertNotEqual(followup["revision_id"], updated["revision_id"])
        self.assertEqual(self.counts(), (2, 3, 3))

        # Closing and reopening must preserve records, original bytes and history.
        store.close()
        reopened = self.open_store()
        self.assertEqual(reopened.current("IT-001.json"), updated)
        self.assertEqual(reopened.history("IT-001.json"), history)
        self.assertEqual(reopened.source_bytes(initial["attempt_id"]), raw)
        self.assertEqual(reopened.attempt(initial["attempt_id"]), history[0])

    def test_T_21_unchanged_rerun(self):
        store = self.open_store()
        original = self.complete(store)
        self.assertEqual(self.responses.call_count, 1)
        for _ in range(2):
            current = self.complete(store)
            self.assertEqual(current, original)
        self.assertEqual(self.responses.call_count, 1)
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(store.history("IT-001.json"), [])
        store.close()
        reopened = self.open_store()
        job = reopened.enqueue("IT-001.json", self.raw(), copy.deepcopy(self.settings))
        self.assertFalse(job.needs_processing)
        self.assertEqual(job.attempt_id, original["attempt_id"])
        self.assertEqual(job.revision_id, original["revision_id"])

    def test_T_22_changed_source(self):
        store = self.open_store()
        original = self.complete(store)
        changed_raw = self.raw(case="changed-write")
        job = store.enqueue("IT-001.json", changed_raw, self.settings)
        self.assertTrue(job.needs_processing)
        pending = store.current("IT-001.json")
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(pending["extraction"])
        self.assertEqual(pending["source_sha256"], self.digest(changed_raw))
        self.assertEqual(pending["attempt_id"], job.attempt_id)
        self.assertEqual(store.history("IT-001.json")[0]["attempt_id"], original["attempt_id"])
        changed = self.execute(store, job, "changed-write.json")
        self.assert_facts(changed, self.cases["changed-write"])
        self.assertNotEqual(changed["attempt_id"], original["attempt_id"])
        self.assertEqual(store.history("IT-001.json")[0]["extraction"]["permission"], "read")

        # Old completions cannot promote an earlier source or alter current data.
        old_record = self.record_from_saved_response("IT-001.json", self.raw(), "IT-001.json")
        with self.assertRaises(self.state.StateError):
            store.finish(original["attempt_id"], old_record)
        self.assertEqual(store.current("IT-001.json"), changed)

        # Returning to earlier bytes is a new revision of the current document,
        # not an excuse to promote an old historical attempt.
        reverted = self.complete(store)
        self.assert_facts(reverted, self.documents["IT-001.json"])
        self.assertNotEqual(reverted["revision_id"], original["revision_id"])
        self.assertEqual(self.responses.call_count, 3)
        self.assertEqual(self.counts(), (1, 3, 3))

        # A response that arrives after its running attempt was superseded
        # cannot replace the current source. This exercises the current-pointer
        # guard, rather than only the guard against completing a terminal job.
        with self.state.StateStore(self.directory / "superseded.sqlite3") as isolated:
            earlier = isolated.enqueue("IT-001.json", self.raw(), self.settings)
            isolated.mark_running(earlier.attempt_id)
            later = isolated.enqueue("IT-001.json", changed_raw, self.settings)
            with self.assertRaises(self.state.StateError):
                isolated.finish(earlier.attempt_id, old_record)
            current = isolated.current("IT-001.json")
            self.assertEqual(current["attempt_id"], later.attempt_id)
            self.assertEqual(current["source_sha256"], self.digest(changed_raw))
            self.assertEqual(current["state"], "pending")
            self.assertIsNone(current["extraction"])
            self.assertTrue(any(row["attempt_id"] == earlier.attempt_id for row in isolated.history("IT-001.json")))
            self.assert_facts(self.execute(isolated, later, "changed-write.json"), self.cases["changed-write"])

        # A completion for the wrong document/hash must roll back in full.
        for bad_field, bad_value in (("document_id", "IT-999.json"), ("source_sha256", "0" * 64)):
            with self.subTest(cross_source_field=bad_field):
                path = self.directory / (bad_field + ".sqlite3")
                with self.state.StateStore(path) as isolated:
                    scheduled = isolated.enqueue("IT-001.json", self.raw(), self.settings)
                    isolated.mark_running(scheduled.attempt_id)
                    bad_record = copy.deepcopy(old_record)
                    bad_record[bad_field] = bad_value
                    with self.assertRaises(self.state.StateError):
                        isolated.finish(scheduled.attempt_id, bad_record)
                    row = isolated.current("IT-001.json")
                    self.assertEqual(row["state"], "running")
                    self.assertEqual(row["status"], "processing")
                    self.assertIsNone(row["extraction"])
                    self.assertIsNone(row["finished_at"])
                    isolated.finish(scheduled.attempt_id, old_record)
                    self.assert_facts(isolated.current("IT-001.json"), self.documents["IT-001.json"])

    def test_T_23_incremental_batch(self):
        store = self.open_store()
        first = self.complete(store, "IT-001.json")
        omitted = self.complete(store, "IT-002.json")
        repeated = self.complete(store, "IT-001.json")
        self.assertEqual(repeated, first)
        self.assertEqual(store.current("IT-002.json"), omitted)
        self.assert_facts(omitted, self.documents["IT-002.json"])
        self.assertEqual(len(store.queue()), 2)
        self.assertEqual(store.history("IT-002.json"), [])
        self.assertEqual(self.responses.call_count, 2)
        self.assertEqual(self.counts(), (2, 2, 2))

    def test_T_24_settings_policy_refresh(self):
        fingerprint = self.state.settings_fingerprint
        reversed_keys = dict(reversed(list(self.settings.items())))
        reversed_keys["generation"] = dict(reversed(list(self.settings["generation"].items())))
        self.assertEqual(fingerprint(self.settings), fingerprint(reversed_keys))
        self.assertRegex(fingerprint(self.settings), r"^[0-9a-f]{64}$")
        for key, value in (("workers", 1), ("min_interval", 2.0), ("timeout_seconds", 30.0)):
            changed = copy.deepcopy(self.settings)
            changed[key] = value
            with self.subTest(operational_setting=key):
                self.assertEqual(fingerprint(self.settings), fingerprint(changed))

        variations = {}
        for key, value in (
            ("provider", "openai"),
            ("model", "another-model"),
            ("endpoint", "https://provider.example.test/v1"),
            ("prompt_sha256", "1" * 64),
            ("schema_sha256", "2" * 64),
            ("policy_sha256", "3" * 64),
            ("routing_version", "2"),
        ):
            changed = copy.deepcopy(self.settings)
            changed[key] = value
            variations[key] = changed
        changed = copy.deepcopy(self.settings)
        changed["generation"]["temperature"] = 0.2
        variations["generation"] = changed
        changed = copy.deepcopy(self.settings)
        changed["mock_fixture_hashes"]["IT-001.json"] = "4" * 64
        variations["mock_fixture_hashes"] = changed

        for label, selected in variations.items():
            with self.subTest(relevant_setting=label):
                self.assertNotEqual(fingerprint(self.settings), fingerprint(selected))
                path = self.directory / (label + ".sqlite3")
                with self.state.StateStore(path) as store:
                    originals = {doc: self.complete(store, doc) for doc in ("IT-001.json", "IT-002.json")}
                    # A later intake contains only IT-001. Refresh must still
                    # find both stored sources without any input directory.
                    self.complete(store, "IT-001.json")
                    jobs = store.refresh(selected)
                    self.assertEqual({job.document_id for job in jobs}, set(originals))
                    for job in jobs:
                        self.assertTrue(job.needs_processing)
                        self.assertEqual(store.source_bytes(job.attempt_id), self.raw(job.document_id))
                        refreshed = self.execute(store, job, job.document_id)
                        self.assert_facts(refreshed, self.documents[job.document_id])
                        self.assertEqual(refreshed["settings"], selected)
                        self.assertEqual(refreshed["settings_fingerprint"], fingerprint(selected))
                        self.assertEqual(refreshed["policy_version"], selected["policy_sha256"])
                        old = store.history(job.document_id)
                        self.assertEqual(len(old), 1)
                        self.assertEqual(old[0]["settings"], self.settings)
                        self.assertEqual(old[0]["attempt_id"], originals[job.document_id]["attempt_id"])
                    self.assertEqual(store.refresh(selected), [])
                    operational = copy.deepcopy(selected)
                    operational.update(workers=1, timeout_seconds=15.0, min_interval=0.25)
                    self.assertEqual(store.refresh(operational), [])
                    self.assertEqual(self.counts(path), (2, 4, 4))

        # Settings are captured at enqueue, not retained as mutable caller data.
        store = self.open_store()
        supplied = copy.deepcopy(self.settings)
        scheduled = store.enqueue("IT-001.json", self.raw(), supplied)
        supplied["generation"]["temperature"] = 99
        self.assertEqual(store.attempt(scheduled.attempt_id)["settings"], self.settings)

        # Fake sentinel only; never fetch the real API_KEY. Rejection must happen
        # before credential text can reach the database or its WAL files.
        fake_secret = "STEP3_SYNTHETIC_SECRET_NEVER_PERSIST"
        for key in ("API_KEY", "api_key", "authorization"):
            secret_settings = copy.deepcopy(self.settings)
            secret_settings[key] = fake_secret
            with self.subTest(credential_key=key):
                with self.assertRaises(self.state.StateError) as caught:
                    store.enqueue("secret.json", self.raw(), secret_settings)
                self.assertNotIn(fake_secret, str(caught.exception))
                self.assertEqual([row["document_id"] for row in store.queue()], ["IT-001.json"])
        store.close()
        for path in self.directory.glob("intake.sqlite3*"):
            self.assertNotIn(fake_secret.encode(), path.read_bytes())

    def test_T_25_failed_retry_after_success(self):
        for scenario, failure_file, category in (
            ("same-source", "provider-unavailable.json", "provider_unavailable"),
            ("changed-source", "timeout.json", "timeout"),
            ("changed-settings", "provider-unavailable.json", "provider_unavailable"),
        ):
            with self.subTest(scenario=scenario):
                self.responses.reset_mock()
                with self.state.StateStore(self.directory / (scenario + ".sqlite3")) as store:
                    original = self.complete(store)
                    selected_settings = copy.deepcopy(self.settings)
                    selected_raw = self.raw()
                    successful_reply = "IT-001.json"
                    successful_expected = self.documents["IT-001.json"]
                    if scenario == "same-source":
                        job = store.retry("IT-001.json")
                        self.assertEqual(job.revision_id, original["revision_id"])
                    elif scenario == "changed-source":
                        selected_raw = self.raw(case="changed-write")
                        successful_reply = "changed-write.json"
                        successful_expected = self.cases["changed-write"]
                        job = store.enqueue("IT-001.json", selected_raw, selected_settings)
                        self.assertNotEqual(job.revision_id, original["revision_id"])
                    else:
                        selected_settings.update(model="updated-model", policy_sha256="5" * 64)
                        jobs = store.refresh(selected_settings)
                        self.assertEqual(len(jobs), 1)
                        job = jobs[0]
                    self.assertTrue(job.needs_processing)
                    self.assertNotEqual(job.attempt_id, original["attempt_id"])
                    self.assertIsNone(store.current("IT-001.json")["extraction"])
                    failed = self.execute(store, job, failure_file)
                    self.assertEqual(failed["state"], "failed")
                    self.assertEqual(failed["status"], "error")
                    self.assertEqual(failed["owner"], "IT Service Desk")
                    self.assertEqual(failed["next_action"], "retry-extraction")
                    self.assertIsNone(failed["extraction"])
                    self.assertEqual(failed["error"]["category"], category)
                    self.assertIn("it-access retry", failed["error"]["retry_command"])
                    self.assertEqual(failed["source_sha256"], self.digest(selected_raw))
                    self.assertEqual(failed["settings"], selected_settings)
                    self.assertEqual(failed["policy_version"], selected_settings["policy_sha256"])
                    historical = store.history("IT-001.json")
                    self.assertEqual(len(historical), 1)
                    self.assertEqual(historical[0]["attempt_id"], original["attempt_id"])
                    self.assertEqual(historical[0]["source_sha256"], self.digest(self.raw()))
                    self.assertEqual(historical[0]["settings"], self.settings)
                    self.assertEqual(historical[0]["extraction"]["permission"], "read")
                    # An unchanged ordinary rerun must expose, not hide/retry,
                    # the error even if a successful reply is now available.
                    rerun = self.complete(store, raw=selected_raw, settings=selected_settings, response_file=successful_reply)
                    self.assertEqual(rerun["attempt_id"], failed["attempt_id"])
                    self.assertEqual(rerun["error"], failed["error"])
                    self.assertEqual(self.responses.call_count, 2)
                    retry = store.retry("IT-001.json")
                    self.assertEqual(retry.revision_id, failed["revision_id"])
                    recovered = self.execute(store, retry, successful_reply)
                    self.assert_facts(recovered, successful_expected)
                    self.assertEqual(self.responses.call_count, 3)
                    self.assertEqual(len(store.history("IT-001.json")), 2)
                    self.assertEqual(len(store.queue()), 1)

    def test_T_26_metadata_is_not_facts(self):
        store = self.open_store()
        original = self.complete(store)
        metadata_raw = self.raw(case="metadata-only")
        self.assertEqual(json.loads(metadata_raw)["message"], json.loads(self.raw())["message"])
        metadata = self.complete(store, raw=metadata_raw, response_file="metadata-only.json")
        self.assert_facts(metadata, self.cases["metadata-only"])
        self.assertNotEqual(metadata["source_sha256"], original["source_sha256"])
        self.assertEqual(metadata["source_sha256"], self.digest(metadata_raw))
        self.assertEqual(metadata["extraction"]["request_date"], "2026-10-22")
        self.assertEqual(metadata["extraction"]["environment"], "staging")
        self.assertEqual(metadata["extraction"]["permission"], "read")
        missing_raw = self.raw(case="missing-request-date")
        self.assertIn("2026-10-22", json.loads(missing_raw)["exported_at"])
        self.assertNotIn("2026-10-22", json.loads(missing_raw)["message"])
        missing = self.complete(store, raw=missing_raw, response_file="missing-request-date.json")
        self.assert_facts(missing, self.cases["missing-request-date"])
        self.assertIsNone(missing["extraction"]["request_date"])
        self.assertEqual([row["source_sha256"] for row in store.history("IT-001.json")], [self.digest(self.raw()), self.digest(metadata_raw)])
        self.assertEqual(self.counts(), (1, 3, 3))


if __name__ == "__main__":
    unittest.main()
