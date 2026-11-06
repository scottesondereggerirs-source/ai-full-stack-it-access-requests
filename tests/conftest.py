"""Pytest fixtures for the IT Access Requests test suite."""

import json
import shutil
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def schema():
    from src.validator import load_schema
    return load_schema(PROJECT_ROOT / "snapshot.schema.json")


@pytest.fixture
def requests_dir() -> Path:
    return PROJECT_ROOT / "materials" / "requests"


@pytest.fixture
def mock_provider():
    from src.mock_provider import MockProvider
    return MockProvider()


@pytest.fixture
def extractor(schema):
    from src.extractor import Extractor
    config = {
        "model": {
            "provider": "databricks",
            "model_id": "test-model",
            "base_url": "http://localhost",
            "temperature": 0.0,
            "max_tokens": 1024,
        },
        "response_source": "mock",
    }
    return Extractor(config, schema, use_mock=True)


@pytest.fixture
def tmp_deliverables(tmp_path) -> Path:
    d = tmp_path / "deliverables"
    d.mkdir()
    return d


@pytest.fixture
def state_manager(tmp_deliverables):
    from src.state import StateManager
    return StateManager(
        deliverables_dir=str(tmp_deliverables),
        processing_settings={"model_id": "test-model", "response_source": "mock"},
    )


@pytest.fixture
def load_request_file(requests_dir):
    def _load(doc_id: str) -> dict:
        path = requests_dir / doc_id
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return _load


@pytest.fixture
def mock_extraction(mock_provider):
    def _get(doc_id: str) -> dict:
        return mock_provider.extract(doc_id, "", "")
    return _get
