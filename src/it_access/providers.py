"""Provider boundary and source-bound, offline saved responses.

This adapter loads test stimuli only. It does not extract facts or consult the
independent expected records. Response validation and routing are separate steps.
"""

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any


class ProviderError(RuntimeError):
    """An actionable failure obtaining a response, before extraction validation."""

    def __init__(self, message: str, category: str = "provider_unavailable", retry_after: float | None = None):
        super().__init__(message)
        self.category = category
        self.retry_after = retry_after


@dataclass(frozen=True)
class ProviderConfig:
    provider: str = "mock"
    model: str = "gpt-4o-2024-08-06"
    endpoint: str = "https://api.openai.com/v1"
    timeout_seconds: float = 60.0
    temperature: float = 0.0
    max_output_tokens: int = 3000
    store: bool = False
    system_prompt: str | None = None
    extraction_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProviderResponse:
    # The metadata cannot be reassigned; each call owns its mutable raw payload.
    payload: dict[str, Any]
    response_source: str


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ProviderError(
            f"Cannot load mock fixture {path.name}; supply a readable UTF-8 JSON file."
        ) from error


class MockProvider:
    """Resolve a saved response by filename, raw source hash and message hash.

    A missing match fails explicitly instead of returning another document's
    extraction. Neither credentials nor network libraries are needed here.
    """

    def __init__(self, fixture_directory: Path):
        self.fixture_directory = Path(fixture_directory).resolve()
        try:
            self._manifest_bytes = (self.fixture_directory / "manifest.json").read_bytes()
            manifest = json.loads(self._manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ProviderError("Cannot load the mock manifest; supply a readable UTF-8 JSON manifest.") from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("format_version") != 1
            or manifest.get("response_source") != "mock"
            or not isinstance(manifest.get("fixtures"), list)
        ):
            raise ProviderError("Invalid mock manifest; supply a version 1 mock fixture manifest.")

        self._fixtures: dict[tuple[str, str], tuple[str, Path]] = {}
        for entry in manifest["fixtures"]:
            required = ("document_id", "source_sha256", "message_sha256", "response_file")
            if not isinstance(entry, dict) or any(
                not isinstance(entry.get(field), str) or not entry[field]
                for field in required
            ):
                raise ProviderError("Invalid mock manifest entry; supply identity, hashes and response_file.")
            key = (entry["document_id"], entry["source_sha256"])
            if key in self._fixtures:
                raise ProviderError("Duplicate document/source in mock manifest; keep one matching reply.")
            path = (self.fixture_directory / entry["response_file"]).resolve()
            if path.parent != self.fixture_directory:
                raise ProviderError("Mock response_file must identify a file inside the fixture directory.")
            self._fixtures[key] = (entry["message_sha256"], path)
        # Capture one immutable fixture snapshot for this processing command.
        # Hash and decode these same bytes so provenance cannot describe a
        # different file revision from the response actually returned.
        self._responses: dict[Path, bytes | None] = {}
        for _, path in self._fixtures.values():
            if path not in self._responses:
                try:
                    self._responses[path] = path.read_bytes()
                except OSError:
                    self._responses[path] = None

    def fixture_hashes(self) -> dict[str, str]:
        result = {"manifest.json": hashlib.sha256(self._manifest_bytes).hexdigest()}
        for path, raw in self._responses.items():
            result[path.name] = hashlib.sha256(raw).hexdigest() if raw is not None else "unavailable"
        return result

    def fixture_fingerprint(self) -> dict[str, str]:
        """Constant-sized per-record provenance for the captured fixture bundle."""
        if not hasattr(self, "_bundle_digest"):
            encoded = json.dumps(self.fixture_hashes(), sort_keys=True, separators=(",", ":")).encode()
            self._bundle_digest = hashlib.sha256(encoded).hexdigest()
        return {"bundle_sha256": self._bundle_digest}

    def extract(
        self,
        *,
        document_id: str,
        source_sha256: str,
        message: str,
        config: ProviderConfig,
    ) -> ProviderResponse:
        if config.provider != "mock":
            raise ProviderError("MockProvider requires provider=mock; select the matching provider adapter.")
        match = self._fixtures.get((document_id, source_sha256))
        if match is None:
            raise ProviderError(
                f"No saved mock response for {document_id} at source {source_sha256}; "
                "add a source-specific reply and manifest entry, or select the live provider."
            )
        message_hash, path = match
        if hashlib.sha256(message.encode("utf-8")).hexdigest() != message_hash:
            raise ProviderError(
                f"Message does not match the saved source for {document_id}; "
                "use the original decoded message or provide a new source-specific fixture."
            )
        raw = self._responses[path]
        if raw is None:
            raise ProviderError(f"Cannot read mock response {path.name}; restore its saved reply before retrying.")
        try:
            # Decode anew so callers cannot mutate another call's response.
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise ProviderError(f"Mock response {path.name} must be valid UTF-8 JSON.", "malformed_output") from error
        if not isinstance(payload, dict):
            raise ProviderError(f"Mock response {path.name} must contain a provider response object.")
        if "mock_error" in payload:
            failure = payload["mock_error"]
            categories = {"timeout", "rate_limit", "provider_auth", "provider_unavailable", "malformed_output"}
            if (
                not isinstance(failure, dict)
                or not isinstance(failure.get("category"), str)
                or failure["category"] not in categories
                or not isinstance(failure.get("message"), str)
                or not failure["message"].strip()
            ):
                raise ProviderError("Saved mock error lacks a recognized category and message.", "malformed_output")
            retry_after = failure.get("retry_after")
            if not isinstance(retry_after, (int, float)) or not math.isfinite(retry_after) or retry_after < 0:
                retry_after = None
            raise ProviderError(failure["message"], failure["category"], retry_after)
        return ProviderResponse(payload=payload, response_source="mock")
