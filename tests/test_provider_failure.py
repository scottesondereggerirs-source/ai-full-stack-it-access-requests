"""Tests for provider failure, refusal, and interrupted response handling."""

import json
import pytest
from unittest.mock import patch, MagicMock
from src.extractor import Extractor, ExtractionError
from src.llm_provider import LLMProvider


class TestProviderError:
    def test_http_error_raises_extraction_error(self, schema):
        config = {
            "model": {
                "provider": "databricks",
                "model_id": "test-model",
                "base_url": "http://localhost",
                "temperature": 0.0,
                "max_tokens": 1024,
            },
            "response_source": "provider",
        }
        extractor = Extractor(config, schema, use_mock=False)

        # Mock the live provider to raise
        with patch.object(extractor, "_get_live_provider") as mock_get:
            mock_provider = MagicMock()
            mock_provider.extract.side_effect = RuntimeError("HTTP 500: Internal Server Error")
            mock_provider.response_source = "provider:test-model"
            mock_get.return_value = mock_provider

            with pytest.raises(ExtractionError, match="Provider error"):
                extractor.extract("IT-001.json", "test message")


class TestRefusalHandling:
    def test_refusal_response_raises(self):
        provider = LLMProvider(
            base_url="http://localhost",
            api_key="test-key",
            model_id="test-model",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {"content": "I cannot help with this request."}
            }]
        }

        with patch("src.llm_provider.requests.post", return_value=mock_response):
            with pytest.raises(RuntimeError, match="refused to extract"):
                provider.extract("system", "user", "IT-001.json", "msg", "{}")


class TestInterruptedResponse:
    def test_empty_response_raises(self):
        provider = LLMProvider(
            base_url="http://localhost",
            api_key="test-key",
            model_id="test-model",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {"content": ""}
            }]
        }

        with patch("src.llm_provider.requests.post", return_value=mock_response):
            with pytest.raises(RuntimeError, match="Empty response"):
                provider.extract("system", "user", "IT-001.json", "msg", "{}")


class TestMalformedOutput:
    def test_malformed_json_raises(self):
        provider = LLMProvider(
            base_url="http://localhost",
            api_key="test-key",
            model_id="test-model",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {"content": "This is not JSON at all"}
            }]
        }

        with patch("src.llm_provider.requests.post", return_value=mock_response):
            with pytest.raises(RuntimeError, match="Malformed JSON"):
                provider.extract("system", "user", "IT-001.json", "msg", "{}")


class TestTimeoutHandling:
    def test_timeout_raises_runtime_error(self):
        import requests as req
        provider = LLMProvider(
            base_url="http://localhost",
            api_key="test-key",
            model_id="test-model",
            timeout=1,
        )

        with patch("src.llm_provider.requests.post", side_effect=req.exceptions.Timeout("timed out")):
            with pytest.raises(RuntimeError, match="timed out"):
                provider.extract("system", "user", "IT-001.json", "msg", "{}")


class TestFailedRetryAfterSuccess:
    def test_retry_failure_shows_error_in_current(self, extractor, state_manager):
        """A failed retry after a prior success should show error in current queue."""
        # First successful extraction
        record = extractor.extract_with_metadata("IT-001.json", "msg", "hash1")
        state_manager.save_record(record)
        assert state_manager.get_current_records()["IT-001.json"]["status"] == "review"

        # Simulate a failed retry by saving an error record
        error_record = {
            "document_id": "IT-001.json",
            "source_sha256": "hash1",
            "extraction": None,
            "status": "error",
            "owner": "IT Service Desk",
            "next_action": "retry-extraction",
            "error": "Provider timeout on retry",
            "response_source": "mock",
        }
        state_manager.save_record(error_record)

        current = state_manager.get_current_records()["IT-001.json"]
        assert current["status"] == "error"
        assert current["error"] == "Provider timeout on retry"

        # Prior success should be in history
        history = state_manager.get_history("IT-001.json")
        assert len(history) == 1
        assert history[0]["status"] == "review"
