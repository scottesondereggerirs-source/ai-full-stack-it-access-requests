"""Step 1 / T-01: offline provider contract, written before its implementation.

Expected facts were transcribed from the committed plan and source messages.
Saved provider envelopes are stimuli; tests/expected/facts.json is the oracle.
This step does not assert that a live model extracts these facts correctly.

The implementation contract introduced here is deliberately small:
  it_access.providers.ProviderConfig() -- frozen configuration, mock by default
  it_access.providers.MockProvider(fixture_directory)
  provider.extract(document_id=..., source_sha256=..., message=..., config=...)
  -> frozen ProviderResponse(payload=<raw envelope>, response_source="mock")
Missing/mismatched saved responses raise ProviderError rather than fabricating
a successful extraction. Source and message hashes select the correct fixture.
"""

from contextlib import ExitStack
import dataclasses
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

MOCKS = ROOT / "tests" / "mock_responses"
EXPECTED = ROOT / "tests" / "expected" / "facts.json"


class CredentialsForbidden(dict):
    """An injected environment; never inspect the real API_KEY in these tests."""

    def __getitem__(self, key):
        if key == "API_KEY":
            raise AssertionError("mock mode attempted to read API_KEY")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key == "API_KEY":
            raise AssertionError("mock mode attempted to read API_KEY")
        return super().get(key, default)

    def __contains__(self, key):
        if key == "API_KEY":
            raise AssertionError("mock mode attempted to look up API_KEY")
        return super().__contains__(key)


class MockProviderContract(unittest.TestCase):
    def setUp(self):
        self.expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.no_socket = self.contexts.enter_context(
            mock.patch.object(
                socket, "socket", side_effect=AssertionError("network is forbidden")
            )
        )
        self.no_connection = self.contexts.enter_context(
            mock.patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("network is forbidden"),
            )
        )
        self.contexts.enter_context(mock.patch.object(os, "environ", CredentialsForbidden()))

    def tearDown(self):
        self.no_socket.assert_not_called()
        self.no_connection.assert_not_called()

    def provider_types(self):
        # Import inside the test so the red run reports the missing application
        # module at the relevant test, rather than failing unittest discovery.
        return importlib.import_module("it_access.providers")

    def first_source(self):
        document = self.expected["documents"][0]
        raw = (ROOT / document["source_path"]).read_bytes()
        return document["document_id"], hashlib.sha256(raw).hexdigest(), json.loads(raw)["message"]

    def test_T_01_expected_facts_are_source_grounded(self):
        """Check the independent oracle against original text, not mock replies."""
        documents = self.expected["documents"]
        fields = self.expected["scalar_fields"]
        self.assertEqual(len(documents), 21)
        self.assertEqual(len({d["document_id"] for d in documents}), 21)
        self.assertEqual(len(fields), 12)
        self.assertEqual(len(fields) * len(documents), 252)
        self.assertEqual(
            {d["document_id"] for d in documents},
            {f"IT-{number:03}.json" for number in range(1, 21)} | {"IT-101.json"},
        )
        for document in documents:
            with self.subTest(document_id=document["document_id"]):
                raw = (ROOT / document["source_path"]).read_bytes()
                message = json.loads(raw)["message"]
                self.assertEqual(document["source_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(set(document["fields"]), set(fields))
                for field, value in document["fields"].items():
                    evidence = document["supporting_evidence"][field]
                    if value is not None:
                        self.assertTrue(evidence, field)
                        if field != "kind":
                            self.assertIn(value, message, field)
                    else:
                        self.assertIn("missing:" + field, document["required_issue_codes"])
                    for quote in evidence:
                        self.assertTrue(quote)
                        self.assertIn(quote, message, field)

    def test_T_01_saved_fixture_manifest_is_complete(self):
        manifest = json.loads((MOCKS / "manifest.json").read_text(encoding="utf-8"))
        entries = manifest["fixtures"]
        self.assertEqual(manifest["response_source"], "mock")
        self.assertEqual(len(entries), 21)
        by_id = {entry["document_id"]: entry for entry in entries}
        self.assertEqual(len(by_id), 21)
        for document in self.expected["documents"]:
            with self.subTest(document_id=document["document_id"]):
                entry = by_id[document["document_id"]]
                self.assertEqual(entry["source_sha256"], document["source_sha256"])
                raw = (ROOT / document["source_path"]).read_bytes()
                message = json.loads(raw)["message"]
                self.assertEqual(
                    entry["message_sha256"], hashlib.sha256(message.encode("utf-8")).hexdigest()
                )
                fixture = MOCKS / entry["response_file"]
                self.assertEqual(fixture.parent.resolve(), MOCKS.resolve())
                self.assertTrue(fixture.is_file())

    def test_T_01_mock_provider_offline(self):
        types = self.provider_types()
        provider = types.MockProvider(MOCKS)
        config = types.ProviderConfig()
        self.assertEqual(config.provider, "mock")
        for document in self.expected["documents"]:
            with self.subTest(document_id=document["document_id"]):
                raw = (ROOT / document["source_path"]).read_bytes()
                message = json.loads(raw)["message"]
                response = provider.extract(
                    document_id=document["document_id"],
                    source_sha256=hashlib.sha256(raw).hexdigest(),
                    message=message,
                    config=config,
                )
                self.assertEqual(response.response_source, "mock")
                self.assertEqual(response.payload["status"], "completed")
                output = response.payload["output"]
                self.assertEqual(len(output), 1)
                self.assertEqual(output[0]["status"], "completed")
                content = output[0]["content"]
                self.assertEqual(len(content), 1)
                self.assertEqual(content[0]["type"], "output_text")
                extraction = json.loads(content[0]["text"])
                self.assertEqual(
                    {field: extraction[field] for field in self.expected["scalar_fields"]},
                    document["fields"],
                )
                self.assertTrue(extraction["supporting_quotes"])
                for quote in extraction["supporting_quotes"]:
                    self.assertTrue(quote)
                    self.assertIn(quote, message)
                for code in document["required_issue_codes"]:
                    self.assertTrue(any(issue.startswith(code + ":") for issue in extraction["issues"]), code)

    def test_T_01_unknown_document_has_no_generic_success_fallback(self):
        types = self.provider_types()
        provider = types.MockProvider(MOCKS)
        message = "An unfamiliar request without a saved mock reply."
        with self.assertRaises(types.ProviderError):
            provider.extract(
                document_id="IT-999.json",
                source_sha256=hashlib.sha256(message.encode()).hexdigest(),
                message=message,
                config=types.ProviderConfig(),
            )

    def test_T_01_changed_source_does_not_reuse_the_old_fixture(self):
        types = self.provider_types()
        document_id, _, message = self.first_source()
        changed = message.replace("new read access", "new write access")
        with self.assertRaises(types.ProviderError):
            types.MockProvider(MOCKS).extract(
                document_id=document_id,
                source_sha256=hashlib.sha256(changed.encode()).hexdigest(),
                message=changed,
                config=types.ProviderConfig(),
            )

    def test_T_01_inconsistent_message_is_not_hidden_by_a_matching_source_hash(self):
        types = self.provider_types()
        document_id, source_hash, message = self.first_source()
        with self.assertRaises(types.ProviderError):
            types.MockProvider(MOCKS).extract(
                document_id=document_id,
                source_sha256=source_hash,
                message=message + " Correction: permission is write.",
                config=types.ProviderConfig(),
            )

    def test_T_01_response_payloads_are_isolated_between_calls(self):
        types = self.provider_types()
        document_id, source_hash, message = self.first_source()
        provider = types.MockProvider(MOCKS)
        arguments = dict(
            document_id=document_id,
            source_sha256=source_hash,
            message=message,
            config=types.ProviderConfig(),
        )
        first = provider.extract(**arguments)
        original = first.payload["output"][0]["content"][0]["text"]
        first.payload["output"][0]["content"][0]["text"] = "tampered"
        second = provider.extract(**arguments)
        self.assertEqual(second.payload["output"][0]["content"][0]["text"], original)

    def test_T_01_configuration_and_response_metadata_are_frozen(self):
        types = self.provider_types()
        config = types.ProviderConfig()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.provider = "openai"
        document_id, source_hash, message = self.first_source()
        response = types.MockProvider(MOCKS).extract(
            document_id=document_id,
            source_sha256=source_hash,
            message=message,
            config=config,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            response.response_source = "openai:invented"


if __name__ == "__main__":
    unittest.main()
