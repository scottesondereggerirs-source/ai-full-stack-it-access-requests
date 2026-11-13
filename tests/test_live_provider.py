"""Step 5: mocked HTTP only; no actual environment key or model access."""
from contextlib import ExitStack
import io
import importlib
import json
import socket
import unittest
from urllib.error import HTTPError, URLError
from unittest import mock

from test_mock_provider import ROOT, MOCKS


class LiveProviderContract(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for owner, name in ((socket.socket, "connect"), (socket, "create_connection")):
            guard = self.stack.enter_context(mock.patch.object(owner, name, side_effect=AssertionError("network forbidden")))
            self.addCleanup(guard.assert_not_called)
        self.module = importlib.import_module("it_access.live_provider")
        from it_access.providers import ProviderConfig
        self.config = ProviderConfig(provider="openai")
        self.message = json.loads((ROOT / "materials/requests/IT-001.json").read_text())["message"]
        self.good = json.loads((MOCKS / "openai-completed.json").read_text())

    def test_T_34_provider_request(self):
        transport = mock.Mock(return_value=io.BytesIO(json.dumps(self.good).encode()))
        provider = self.module.OpenAIProvider(transport=transport, environ={"API_KEY": "SYNTHETIC_KEY"})
        response = provider.extract(document_id="IT-001.json", source_sha256="0"*64, message=self.message, config=self.config)
        request = transport.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer SYNTHETIC_KEY")
        self.assertEqual(transport.call_args.kwargs["timeout"], 60)
        self.assertEqual(body["input"][1], {"role":"user", "content":self.message})
        self.assertEqual(body["model"], self.config.model)
        self.assertFalse(body["store"])
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["text"]["format"]["schema"], json.loads((ROOT/"snapshot.schema.json").read_text()))
        self.assertEqual(response.response_source, "openai:gpt-4o-2024-08-06")
        self.assertEqual(transport.call_count, 1)
        from dataclasses import replace
        changed = replace(self.config, endpoint="https://provider.example.test/v1", model="configured-model", timeout_seconds=7)
        transport.return_value = io.BytesIO(json.dumps(self.good).encode())
        provider.extract(document_id="IT-001.json", source_sha256="0"*64, message=self.message, config=changed)
        self.assertEqual(transport.call_args.args[0].full_url,"https://provider.example.test/v1/responses")
        self.assertEqual(json.loads(transport.call_args.args[0].data)["model"],"configured-model")
        self.assertEqual(transport.call_args.kwargs["timeout"],7)

    def test_T_35_provider_response_errors(self):
        from it_access.providers import ProviderError
        from it_access.extraction import process_response
        for fixture, category in (("refusal.json","refusal"),("incomplete.json","interrupted_response")):
            with self.subTest(fixture=fixture):
                transport=mock.Mock(return_value=io.BytesIO((MOCKS/fixture).read_bytes()))
                provider=self.module.OpenAIProvider(transport=transport,environ={"API_KEY":"FAKE"})
                response=provider.extract(document_id="IT-001.json",source_sha256="0"*64,message=self.message,config=self.config)
                record=process_response(document_id="IT-001.json",source_sha256="0"*64,message=self.message,response=response)
                self.assertEqual(record["error"]["category"],category)
                self.assertIsNone(record["extraction"])
                self.assertEqual(transport.call_count,1)
        for error, category in (
            (HTTPError("https://example.test",401,"unauthorized",{},None),"provider_auth"),
            (HTTPError("https://example.test",429,"limited",{"Retry-After":"2"},None),"rate_limit"),
            (HTTPError("https://example.test",500,"unavailable",{},None),"provider_unavailable"),
            (TimeoutError("timeout"),"timeout"),
            (URLError("connection unavailable"),"provider_unavailable"),
            (ConnectionResetError("cut off"),"interrupted_response"),
        ):
            with self.subTest(category=category):
                transport=mock.Mock(side_effect=error)
                with self.assertRaises(ProviderError) as caught:
                    self.module.OpenAIProvider(transport=transport,environ={"API_KEY":"FAKE"}).extract(document_id="IT-001.json",source_sha256="0"*64,message=self.message,config=self.config)
                self.assertEqual(caught.exception.category,category)
                if category=="rate_limit":self.assertEqual(caught.exception.retry_after,2)
                self.assertEqual(transport.call_count,1)
        transport=mock.Mock(return_value=io.BytesIO(b'not valid JSON'))
        with self.assertRaises(ProviderError) as caught:
            self.module.OpenAIProvider(transport=transport,environ={"API_KEY":"FAKE"}).extract(document_id="IT-001.json",source_sha256="0"*64,message=self.message,config=self.config)
        self.assertEqual(caught.exception.category,"malformed_output")

    def test_T_36_credential_redaction(self):
        from it_access.providers import ProviderError
        secret="TEST_SECRET_SENTINEL"
        transport=mock.Mock(side_effect=HTTPError("https://example.test",401,"Bearer "+secret,{},io.BytesIO(secret.encode())))
        provider=self.module.OpenAIProvider(transport=transport,environ={"API_KEY":secret})
        with self.assertRaises(ProviderError) as caught:
            provider.extract(document_id="IT-001.json",source_sha256="0"*64,message=self.message,config=self.config)
        self.assertNotIn(secret,str(caught.exception))
        evidence=provider.request_evidence(self.message,self.config)
        self.assertNotIn(secret,json.dumps(evidence))
        self.assertEqual(evidence["headers"]["Authorization"],"Bearer [REDACTED]")
        missing=mock.Mock()
        with self.assertRaises(ProviderError):
            self.module.OpenAIProvider(transport=missing,environ={}).extract(document_id="IT-001.json",source_sha256="0"*64,message=self.message,config=self.config)
        missing.assert_not_called()
