"""OpenAI Responses integration; tests inject HTTP and a synthetic environment."""
import json
import math
import os
import socket
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import extraction
from .providers import ProviderConfig, ProviderError, ProviderResponse


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(request, *, timeout):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


class OpenAIProvider:
    def __init__(self, *, transport=None, environ=None):
        self.transport = transport or _transport
        self.environ = environ

    def request_evidence(self, message: str, config: ProviderConfig):
        endpoint = urlsplit(config.endpoint)
        if endpoint.scheme != "https" or not endpoint.netloc or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ProviderError("Provider endpoint must be HTTPS without credentials or query values.", "provider_auth")
        messages = extraction.build_messages(message)
        if config.system_prompt is not None:
            messages[0]["content"] = config.system_prompt
        schema = config.extraction_schema if config.extraction_schema is not None else json.loads(extraction.SCHEMA_PATH.read_text())
        return {
            "url": config.endpoint.rstrip("/") + "/responses", "method": "POST",
            "headers": {"Authorization": "Bearer [REDACTED]", "Content-Type": "application/json"},
            "timeout_seconds": config.timeout_seconds,
            "body": {
                "model": config.model, "input": messages, "temperature": config.temperature,
                "max_output_tokens": config.max_output_tokens, "store": config.store,
                "text": {"format": {"type": "json_schema", "name": "access_request", "strict": True, "schema": schema}},
            },
        }

    def extract(self, *, document_id, source_sha256, message, config):
        evidence = self.request_evidence(message, config)
        environment = self.environ if self.environ is not None else os.environ
        key = environment.get("API_KEY")
        if not isinstance(key, str) or not key.strip():
            raise ProviderError("Set API_KEY in the environment for live extraction.", "provider_auth")
        request = Request(evidence["url"], data=json.dumps(evidence["body"]).encode("utf-8"), method="POST",
                          headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with self.transport(request, timeout=config.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as error:
            category = "provider_auth" if error.code in (401,403) else "rate_limit" if error.code == 429 else "timeout" if error.code in (408,504) else "provider_unavailable"
            retry_after = None
            try:
                value = float(error.headers.get("Retry-After", ""))
                if math.isfinite(value) and value >= 0: retry_after = value
            except (ValueError, TypeError, AttributeError):
                pass
            # Never echo URL, reason, headers or error body, which can contain a key.
            raise ProviderError(f"Provider HTTP {error.code}; check service access or retry later.", category, retry_after) from None
        except (TimeoutError, socket.timeout):
            raise ProviderError("Provider request timed out; retry extraction.", "timeout") from None
        except (IncompleteRead, ConnectionResetError):
            raise ProviderError("Provider connection ended before the response completed.", "interrupted_response") from None
        except URLError as error:
            category = "timeout" if isinstance(error.reason, TimeoutError) else "provider_unavailable"
            raise ProviderError("Provider connection failed; check service availability.", category) from None
        except OSError:
            raise ProviderError("Provider transport failed; check service availability.", "provider_unavailable") from None
        try:
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=extraction._unique_object, parse_constant=extraction._invalid_constant)
            if not isinstance(payload, dict): raise ValueError("object required")
        except (ValueError, UnicodeError):
            raise ProviderError("Provider returned malformed JSON.", "malformed_output") from None
        returned_model = payload.get("model")
        model = returned_model if isinstance(returned_model, str) and returned_model else config.model
        model = model.replace(key, "[REDACTED]")
        return ProviderResponse(payload=payload, response_source="openai:" + model)
