"""Configuration loading from config.json with environment variable overrides."""

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = "config.json"


def load_config(config_path: str | None = None) -> dict[str, Any]:
    """Load configuration from config.json, with environment variable overrides.

    Env vars (highest priority):
      API_KEY         — LLM provider API key
      MODEL_ID        — override model_id
      BASE_URL        — override LLM base URL
      RESPONSE_SOURCE — override response_source ("mock" or "provider")
    """
    path = Path(config_path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, encoding="utf-8") as f:
        config = json.load(f)

    model_id = os.environ.get("MODEL_ID")
    if model_id:
        config.setdefault("model", {})["model_id"] = model_id

    base_url = os.environ.get("BASE_URL")
    if base_url:
        config.setdefault("model", {})["base_url"] = base_url

    response_source = os.environ.get("RESPONSE_SOURCE")
    if response_source:
        config["response_source"] = response_source

    # Expand ${VAR} references (e.g. ${DATABRICKS_HOST}) in the base URL
    model_cfg = config.get("model", {})
    if model_cfg.get("base_url"):
        model_cfg["base_url"] = os.path.expandvars(model_cfg["base_url"]).rstrip("/")

    return config


def get_api_key(config: dict[str, Any]) -> str | None:
    """Return the API key from config or environment variable."""
    return os.environ.get("API_KEY") or config.get("model", {}).get("api_key")
