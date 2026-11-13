"""Step 2 / T-02 through T-19: validation and policy routing, tests first.

The boundary to implement is it_access.extraction.process_response(...), which
returns an outer record with extraction/routing or a categorized, actionable
error and null extraction. build_messages(message) supplies the extraction
instructions separately from the untrusted user message. No live model is used.

Expected scalar facts/routing come from tests/expected, independently of the
saved provider envelopes. Parameterized mutations are stimuli, never oracles.
"""

from contextlib import ExitStack
import copy
import hashlib
import importlib
import json
import os
import socket
import unittest
from unittest import mock

from test_mock_provider import CredentialsForbidden, MOCKS, ROOT


class ExtractionContract(unittest.TestCase):
    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.contexts.enter_context(mock.patch.object(os, "environ", CredentialsForbidden()))
        # Keep socket.socket a real type so importing an HTTP/SSL dependency
        # cannot fail merely because it subclasses that type. Connections fail.
        self.no_connect = self.contexts.enter_context(
            mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        )
        self.no_connect_ex = self.contexts.enter_context(
            mock.patch.object(socket.socket, "connect_ex", side_effect=AssertionError("network forbidden"))
        )
        self.no_connection = self.contexts.enter_context(
            mock.patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden"))
        )
        for guard in (self.no_connect, self.no_connect_ex, self.no_connection):
            self.addCleanup(guard.assert_not_called)
        expected = json.loads((ROOT / "tests/expected/facts.json").read_text(encoding="utf-8"))
        self.fields = expected["scalar_fields"]
        self.documents = {document["document_id"]: document for document in expected["documents"]}
        variants = json.loads((ROOT / "tests/expected/step-02-cases.json").read_text(encoding="utf-8"))
        self.cases = {case["name"]: case for case in variants["cases"]}
        self.schema = json.loads((ROOT / "snapshot.schema.json").read_text(encoding="utf-8"))
        self.provider_types = importlib.import_module("it_access.providers")
        # The intended red failure until the test-first commit is recorded.
        self.pipeline = importlib.import_module("it_access.extraction")

    def payload(self, filename):
        return json.loads((MOCKS / filename).read_text(encoding="utf-8"))

    def extracted_payload(self, payload):
        return json.loads(payload["output"][0]["content"][0]["text"])

    def replace_extraction(self, payload, extraction):
        result = copy.deepcopy(payload)
        result["output"][0]["content"][0]["text"] = json.dumps(extraction, ensure_ascii=False)
        return result

    def source(self, expected):
        raw = (ROOT / expected["source_path"]).read_bytes()
        return raw, json.loads(raw)["message"]

    def process(self, document_id="IT-001.json", *, case=None, response_file=None, payload=None):
        expected = self.cases[case] if case else self.documents[document_id]
        raw, message = self.source(expected)
        if payload is None:
            filename = response_file or expected.get("response_file")
            if filename is None:
                filename = "paraphrase.json" if document_id == "IT-101.json" else document_id
            payload = self.payload(filename)
        result = self.pipeline.process_response(
            document_id=expected["document_id"],
            source_sha256=hashlib.sha256(raw).hexdigest(),
            message=message,
            response=self.provider_types.ProviderResponse(payload=payload, response_source="mock"),
        )
        self.assertEqual(result["document_id"], expected["document_id"])
        self.assertEqual(result["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["response_source"], "mock")
        return result

    def assert_issue(self, extraction, code):
        matches = [issue for issue in extraction["issues"] if issue.startswith(code + ":")]
        self.assertTrue(matches, code)
        self.assertTrue(any(issue[len(code) + 1:].strip() for issue in matches), code)

    def assert_expected(self, result, expected):
        self.assertIsNone(result["error"])
        for field in ("status", "owner", "next_action"):
            self.assertEqual(result[field], expected[field], field)
        extraction = result["extraction"]
        self.assertEqual(set(extraction), set(self.schema["properties"]))
        self.assertEqual({field: extraction[field] for field in self.fields}, expected["fields"])
        _, message = self.source(expected)
        self.assertTrue(extraction["supporting_quotes"])
        for quote in extraction["supporting_quotes"]:
            self.assertTrue(quote)
            self.assertIn(quote, message)
        for quotes in expected.get("supporting_evidence", {}).values():
            for required_quote in quotes:
                self.assertTrue(
                    any(required_quote in quote for quote in extraction["supporting_quotes"]),
                    required_quote,
                )
        for code in expected["required_issue_codes"]:
            self.assert_issue(extraction, code)
        self.assertNotEqual(result["status"], "approved")

    def assert_error(self, result, category):
        self.assertIsNone(result["extraction"])
        self.assertEqual(
            (result["status"], result["owner"], result["next_action"]),
            ("error", "IT Service Desk", "retry-extraction"),
        )
        self.assertEqual(result["error"]["category"], category)
        self.assertTrue(result["error"]["message"].strip())
        self.assertIn("it-access retry", result["error"]["retry_command"])
        self.assertIn(result["document_id"], result["error"]["retry_command"])

    def test_T_02_complete_request(self):
        for document_id in ("IT-001.json", "IT-018.json"):
            with self.subTest(document_id=document_id):
                self.assert_expected(self.process(document_id), self.documents[document_id])

    def test_T_03_missing_dates(self):
        for document_id in ("IT-010.json", "IT-011.json"):
            with self.subTest(document_id=document_id):
                self.assert_expected(self.process(document_id), self.documents[document_id])

    def test_T_04_conflicting_details(self):
        for document_id in ("IT-004.json", "IT-020.json"):
            with self.subTest(document_id=document_id):
                self.assert_expected(self.process(document_id), self.documents[document_id])
        # Even if the model picks an alternative, its own conflict finding
        # must prevent that unresolved field from becoming a business fact.
        self.assert_expected(
            self.process("IT-020.json", response_file="conflict-picked-alternative.json"),
            self.documents["IT-020.json"],
        )

    def test_T_05_embedded_instructions(self):
        for filename in ("IT-006.json", "override-omitted.json"):
            with self.subTest(response=filename):
                self.assert_expected(
                    self.process("IT-006.json", response_file=filename),
                    self.documents["IT-006.json"],
                )
        for name in ("override-withdrawal", "benign-approval"):
            with self.subTest(case=name):
                result = self.process(case=name)
                self.assert_expected(result, self.cases[name])
                if name == "benign-approval":
                    self.assertFalse(any(issue.startswith("instruction-override:") for issue in result["extraction"]["issues"]))

    def test_T_06_withdrawal(self):
        self.assert_expected(self.process("IT-005.json"), self.documents["IT-005.json"])
        for name in ("withdrawal-missing-id", "withdrawal-missing-date"):
            with self.subTest(case=name):
                self.assert_expected(self.process(case=name), self.cases[name])

    def test_T_07_security_routes(self):
        for document_id in ("IT-002.json", "IT-012.json", "IT-013.json"):
            with self.subTest(document_id=document_id):
                self.assert_expected(self.process(document_id), self.documents[document_id])

    def test_T_08_change_permission(self):
        for document_id in ("IT-007.json", "IT-019.json"):
            with self.subTest(document_id=document_id):
                self.assert_expected(self.process(document_id), self.documents[document_id])

    def test_T_09_corrections(self):
        for document_id in ("IT-008.json", "IT-015.json"):
            with self.subTest(document_id=document_id):
                result = self.process(document_id)
                self.assert_expected(result, self.documents[document_id])
                self.assertFalse(any(issue.startswith("conflict:") for issue in result["extraction"]["issues"]))

    def test_T_10_missing_facts(self):
        for document_id in ("IT-003.json", "IT-009.json"):
            with self.subTest(document_id=document_id):
                self.assert_expected(self.process(document_id), self.documents[document_id])
        for kind in ("new", "change"):
            for field in self.fields:
                name = f"{kind}-missing-{field}"
                with self.subTest(kind=kind, missing=field):
                    result = self.process(case=name)
                    self.assert_expected(result, self.cases[name])
                    self.assertIsNone(result["extraction"][field])

    def test_T_11_identifier_mismatch(self):
        result = self.process("IT-016.json")
        self.assert_expected(result, self.documents["IT-016.json"])
        self.assertEqual(result["document_id"], "IT-016.json")
        self.assertEqual(result["extraction"]["request_id"], "IT-061")

    def test_T_12_other_kind(self):
        self.assert_expected(self.process("IT-017.json"), self.documents["IT-017.json"])
        result = self.process("IT-017.json", response_file="kind-null.json")
        expected = copy.deepcopy(self.documents["IT-017.json"])
        expected["fields"]["kind"] = None
        expected["required_issue_codes"].append("missing:kind")
        expected["supporting_evidence"]["kind"] = []
        self.assert_expected(result, expected)

    def test_T_13_reversed_range(self):
        self.assert_expected(self.process("IT-014.json"), self.documents["IT-014.json"])

    def test_T_14_paraphrase_and_prompt_boundary(self):
        self.assert_expected(self.process("IT-101.json"), self.documents["IT-101.json"])
        _, message = self.source(self.documents["IT-101.json"])
        _, attack = self.source(self.documents["IT-006.json"])
        messages = self.pipeline.build_messages(message)
        attacked_messages = self.pipeline.build_messages(attack)
        self.assertEqual([entry["role"] for entry in messages], ["system", "user"])
        self.assertEqual(messages[1]["content"], message)
        self.assertEqual(attacked_messages[1]["content"], attack)
        self.assertEqual(messages[0], attacked_messages[0])
        instructions = messages[0]["content"]
        for field in self.schema["required"]:
            self.assertIn(field, instructions)
        for rule in ("correction", "relative", "null", "conflict:", "instruction-override:"):
            self.assertIn(rule, instructions.lower())
        self.assertNotIn(attack, instructions)
        self.assertNotIn(message, instructions)

    def test_T_15_schema_date_matrix(self):
        seed = self.payload("IT-001.json")
        original = self.extracted_payload(seed)
        for missing in self.schema["required"]:
            with self.subTest(missing_key=missing):
                value = copy.deepcopy(original)
                del value[missing]
                self.assert_error(self.process(payload=self.replace_extraction(seed, value)), "validation")
        for field, spec in self.schema["properties"].items():
            invalid = (None, "not-an-array", {}, [17], [None], [{}]) if spec["type"] == "array" else (17, False, {}, [])
            for value in invalid:
                with self.subTest(field=field, wrong_type=repr(value)):
                    extraction = copy.deepcopy(original)
                    extraction[field] = value
                    self.assert_error(self.process(payload=self.replace_extraction(seed, extraction)), "validation")
        for field in ("kind", "environment", "permission"):
            with self.subTest(invalid_enum=field):
                value = copy.deepcopy(original)
                value[field] = "unsupported-value"
                self.assert_error(self.process(payload=self.replace_extraction(seed, value)), "validation")
        for filename in ("schema-extra-key.json", "schema-missing-key.json", "schema-type.json", "schema-enum.json"):
            with self.subTest(response=filename):
                self.assert_error(self.process(response_file=filename), "validation")
        for root_value in (None, [], "not an object", True, 3):
            with self.subTest(extraction_root=repr(root_value)):
                self.assert_error(self.process(payload=self.replace_extraction(seed, root_value)), "validation")
        blank_result = self.process(response_file="blank-scalar.json")
        self.assertEqual(blank_result["status"], "needs-information")
        self.assertIsNone(blank_result["extraction"]["manager"])
        self.assert_issue(blank_result["extraction"], "missing:manager")
        for field in self.fields:
            for blank in ("", " \t\n"):
                with self.subTest(field=field, blank=repr(blank)):
                    value = copy.deepcopy(original)
                    value[field] = blank
                    payload = self.replace_extraction(seed, value)
                    before = copy.deepcopy(payload)
                    result = self.process(payload=payload)
                    self.assertEqual(result["status"], "needs-information")
                    self.assertIsNone(result["extraction"][field])
                    self.assert_issue(result["extraction"], "missing:" + field)
                    self.assertEqual(payload, before, "validation must not mutate a provider response")
        all_null = copy.deepcopy(original)
        all_null.update({field: None for field in self.fields})
        all_null["issues"] = []
        result = self.process(payload=self.replace_extraction(seed, all_null))
        self.assertEqual(result["status"], "needs-information")
        for field in self.fields:
            self.assert_issue(result["extraction"], "missing:" + field)
        for filename in ("invalid-date.json", "invalid-leap-day.json", "non-iso-date.json"):
            with self.subTest(response=filename):
                self.assert_error(self.process(response_file=filename), "validation")
        for bad_date in ("20261023", "2026-1-02", "2026-10-23T00:00:00Z", "2026-10-23 "):
            for field in ("request_date", "start_date", "end_date"):
                with self.subTest(field=field, date=bad_date):
                    value = copy.deepcopy(original)
                    value[field] = bad_date
                    self.assert_error(self.process(payload=self.replace_extraction(seed, value)), "validation")
        for name in ("valid-leap-day", "equal-dates"):
            with self.subTest(case=name):
                self.assert_expected(self.process(case=name), self.cases[name])

    def test_T_16_quote_evidence(self):
        for filename in ("invented-quote.json", "empty-quotes.json", "empty-quote.json"):
            with self.subTest(response=filename):
                self.assert_error(self.process(response_file=filename), "validation")
        self.assert_error(
            self.process("IT-004.json", response_file="conflict-without-evidence.json"),
            "validation",
        )
        self.assert_expected(self.process(case="decoded-escape"), self.cases["decoded-escape"])
        payload = self.payload("decoded-escape.json")
        value = self.extracted_payload(payload)
        value["supporting_quotes"] = ['Evidence note: "Maya" compared the cafe\u0301 totals.']
        self.assert_error(self.process(case="decoded-escape", payload=self.replace_extraction(payload, value)), "validation")
        # Structural validation cannot establish all semantic truth. Keep this
        # fabricated but schema-shaped record for independent scoring in T-39.
        result = self.process(response_file="wrong-facts.json")
        if result["error"] is None:
            self.assertNotEqual(result["extraction"]["requester"], self.documents["IT-001.json"]["fields"]["requester"])
            self.assertNotEqual(result["status"], "approved")
        else:
            self.assert_error(result, "validation")

    def test_T_17_malformed_output(self):
        for filename in ("malformed-json.json", "duplicate-key-output.json", "missing-output.json", "multi-output.json"):
            with self.subTest(response=filename):
                self.assert_error(self.process(response_file=filename), "malformed_output")
        seed = self.payload("IT-001.json")
        for text in ("", "   ", None, {"request_id": "IT-001"}):
            with self.subTest(output_text=repr(text)):
                payload = copy.deepcopy(seed)
                payload["output"][0]["content"][0]["text"] = text
                self.assert_error(self.process(payload=payload), "malformed_output")
        payload = copy.deepcopy(seed)
        payload["output"][0]["content"].append(copy.deepcopy(payload["output"][0]["content"][0]))
        self.assert_error(self.process(payload=payload), "malformed_output")
        payload = copy.deepcopy(seed)
        payload["output"][0]["role"] = "system"
        self.assert_error(self.process(payload=payload), "malformed_output")

    def test_T_18_refusal(self):
        self.assert_error(self.process(response_file="refusal.json"), "refusal")
        payload = self.payload("refusal.json")
        payload["output"][0]["content"].append(self.payload("IT-001.json")["output"][0]["content"][0])
        self.assert_error(self.process(payload=payload), "refusal")

    def test_T_19_interrupted_response(self):
        for filename in ("incomplete.json", "message-incomplete.json", "truncated.json"):
            with self.subTest(response=filename):
                self.assert_error(self.process(response_file=filename), "interrupted_response")
        for filename in ("missing-completion-status.json", "tool-output.json"):
            with self.subTest(response=filename):
                self.assert_error(self.process(response_file=filename), "malformed_output")
        for status in ("in_progress", "cancelled"):
            with self.subTest(status=status):
                payload = self.payload("IT-001.json")
                payload["status"] = status
                self.assert_error(self.process(payload=payload), "interrupted_response")
        payload = self.payload("IT-001.json")
        del payload["output"][0]["status"]
        self.assert_error(self.process(payload=payload), "malformed_output")


if __name__ == "__main__":
    unittest.main()
