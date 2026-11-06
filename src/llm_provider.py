"""Live LLM provider — OpenAI-compatible chat completions endpoint."""

import json
import logging
from typing import Any

import requests

from .utils import redact_credentials

logger = logging.getLogger("it_access_requests")


class LLMProvider:
    """Calls an OpenAI-compatible chat completions endpoint.

    Works with Databricks AI Gateway, OpenAI, or any compatible API.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_id: str,
        temperature: float | None = None,
        max_tokens: int = 4096,
        timeout: int = 120,
        max_tokens_param: str = "max_completion_tokens",
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model_id = model_id
        self.temperature = temperature  # None = provider default (required by reasoning models)
        self.max_tokens = max_tokens
        self.max_tokens_param = max_tokens_param
        self.timeout = timeout

    @property
    def response_source(self) -> str:
        return f"provider:{self.model_id}"

    def extract(
        self,
        system_prompt: str,
        user_prompt: str,
        document_id: str,
        message: str,
        schema_json: str,
    ) -> dict[str, Any]:
        """Send extraction request and return parsed JSON response.

        Raises:
            RuntimeError: on HTTP error, refusal, or interrupted response.
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        payload = self._build_payload(system_prompt, user_prompt)

        logger.debug("Sending request to %s for %s", self.base_url, document_id)

        try:
            response = requests.post(
                self.base_url,
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(f"Request timed out after {self.timeout}s") from exc
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError(f"Connection error: {exc}") from exc

        if response.status_code != 200:
            body = redact_credentials(response.text)
            raise RuntimeError(
                f"HTTP {response.status_code} from provider: {body}"
            )

        data = response.json()

        choice = (data.get("choices") or [{}])[0]
        finish_reason = choice.get("finish_reason")
        content = (choice.get("message") or {}).get("content") or ""

        # Interrupted / truncated response
        if finish_reason == "length":
            raise RuntimeError(
                f"Interrupted response: output truncated at token limit ({self.max_tokens_param}={self.max_tokens})"
            )
        if finish_reason == "content_filter":
            raise RuntimeError("Provider refused to extract: response blocked by content filter")
        if not content:
            raise RuntimeError("Empty response from provider (possible interrupted response)")

        content = content.strip()

        # Strip markdown code fences if present
        if content.startswith("```"):
            lines = content.split("\n")
            # Remove first line (```json or ```) and last line (```)
            lines = [l for l in lines[1:] if not l.strip().startswith("```")]
            content = "\n".join(lines).strip()

        # Check for refusal indicators
        refusal_markers = ["I cannot", "I can't", "I'm unable to", "As an AI"]
        if any(content.startswith(marker) for marker in refusal_markers):
            raise RuntimeError(f"Provider refused to extract: {content[:200]}")

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Malformed JSON from provider: {exc}") from exc

    def _build_payload(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Build the Chat Completions request body."""
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            self.max_tokens_param: self.max_tokens,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        return payload

    def build_request_evidence(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        """Build a redacted request evidence record for deliverables."""
        return {
            "url": self.base_url,
            "body": self._build_payload(system_prompt, user_prompt),
            "headers": {
                "Content-Type": "application/json",
                "Authorization": "Bearer ***REDACTED***",
            },
        }
