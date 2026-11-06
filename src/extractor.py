"""Extraction orchestration: build prompt, call provider, parse, validate."""

import json
import logging
from typing import Any

from .config import get_api_key
from .mock_provider import MockProvider
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .validator import load_schema, validate_extraction
from .utils import redact_credentials

logger = logging.getLogger("it_access_requests")


class ExtractionError(Exception):
    """Raised when extraction fails (provider error, malformed output, refusal)."""


class Extractor:
    """Orchestrates LLM extraction: prompt → provider → parse → validate.

    Supports mock provider (for reproducible dev) and live provider.
    """

    def __init__(
        self,
        config: dict[str, Any],
        schema: dict[str, Any],
        use_mock: bool = True,
    ) -> None:
        self.config = config
        self.schema = schema
        self.use_mock = use_mock
        self._mock = MockProvider()
        self._live = None

    def _get_live_provider(self):
        """Lazily initialise the live LLM provider."""
        if self._live is None:
            from .llm_provider import LLMProvider

            api_key = get_api_key(self.config)
            if not api_key:
                raise ExtractionError("No API key found. Set the API_KEY environment variable.")
            self._live = LLMProvider(
                base_url=self.config["model"]["base_url"],
                api_key=api_key,
                model_id=self.config["model"]["model_id"],
                temperature=self.config["model"].get("temperature"),
                max_tokens=self.config["model"].get("max_tokens", 4096),
                timeout=self.config["model"].get("timeout_seconds", 120),
                max_tokens_param=self.config["model"].get("max_tokens_param", "max_completion_tokens"),
            )
        return self._live

    @property
    def response_source(self) -> str:
        """Return the response source identifier."""
        if self.use_mock:
            return self._mock.response_source
        provider = self._get_live_provider()
        return provider.response_source

    def extract(self, document_id: str, message: str) -> dict[str, Any]:
        """Extract a structured record from the message.

        Returns the extraction dict (validated against schema).
        Raises ExtractionError on failure.
        """
        schema_json = json.dumps(self.schema, indent=2)
        user_prompt = build_user_prompt(document_id, message, schema_json)

        logger.info("Extracting %s (source=%s)", document_id, "mock" if self.use_mock else "provider")

        try:
            if self.use_mock:
                raw_response = self._mock.extract(document_id, message, schema_json)
            else:
                provider = self._get_live_provider()
                raw_response = provider.extract(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    document_id=document_id,
                    message=message,
                    schema_json=schema_json,
                )
        except Exception as exc:
            logger.error("Provider error for %s: %s", document_id, redact_credentials(str(exc)))
            raise ExtractionError(f"Provider error: {exc}") from exc

        # Parse response (mock returns dict, live returns parsed JSON or raises)
        if isinstance(raw_response, str):
            try:
                extraction = json.loads(raw_response)
            except json.JSONDecodeError as exc:
                raise ExtractionError(f"Malformed JSON output: {exc}") from exc
        elif isinstance(raw_response, dict):
            extraction = raw_response
        else:
            raise ExtractionError(f"Unexpected response type: {type(raw_response)}")

        # Validate against schema
        validation = validate_extraction(extraction, self.schema)
        if not validation["valid"]:
            errors = "; ".join(validation["schema_errors"])
            logger.error("Schema validation failed for %s: %s", document_id, errors)
            raise ExtractionError(f"Schema validation failed: {errors}")

        logger.info("Extraction successful for %s", document_id)
        return extraction

    def extract_with_metadata(self, document_id: str, message: str, source_sha256: str) -> dict[str, Any]:
        """Extract and wrap in a full record with metadata.

        Returns a dict with: document_id, source_sha256, extraction, status,
        owner, next_action, error, response_source.
        """
        from .policy import apply_policy

        try:
            extraction = self.extract(document_id, message)
            routing = apply_policy(extraction)
            return {
                "document_id": document_id,
                "source_sha256": source_sha256,
                "extraction": extraction,
                "status": routing["status"],
                "owner": routing["owner"],
                "next_action": routing["next_action"],
                "error": None,
                "response_source": self.response_source,
            }
        except ExtractionError as exc:
            return {
                "document_id": document_id,
                "source_sha256": source_sha256,
                "extraction": None,
                "status": "error",
                "owner": "IT Service Desk",
                "next_action": "retry-extraction",
                "error": str(exc),
                "response_source": self.response_source,
            }
