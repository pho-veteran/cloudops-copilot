"""Deployment-level wiring checks for the `live_kubernetes` source mode."""

from pathlib import Path


def test_compose_passes_kube_read_config():
    compose = Path("compose.yaml").read_text(encoding="utf-8")
    assert "KUBE_READ_CONFIG" in compose
    assert "COLLECTION_SOURCE_MODE" in compose


def test_env_example_documents_live_mode():
    env = Path(".env.example").read_text(encoding="utf-8")
    assert "COLLECTION_SOURCE_MODE=development_fixture" in env  # default unchanged
    assert "live_kubernetes" in env
    assert "KUBE_READ_CONFIG" in env


def test_foundation_docs_declare_live_adapter():
    docs = Path("docs/guides/foundation.md").read_text(encoding="utf-8")
    assert "live_kubernetes" in docs
    assert "Only the fixture adapter is installed" not in docs
