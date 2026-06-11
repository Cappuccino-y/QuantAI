"""Pytest 配置：把 .env.example 注入测试环境，避免 import-time 凭证错误."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _set_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TQ_ACCOUNT", "test_account")
    monkeypatch.setenv("TQ_PASSWORD", "test_password")
    monkeypatch.setenv("TQ_USE_SIM", "True")
    monkeypatch.setenv("LLM_API_KEY", "test_llm_key")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("LLM_MODEL_ID", "gpt-4o-mini")
    monkeypatch.setenv("DINGTALK_WEBHOOK", "")
    monkeypatch.setenv("ENABLE_NOTIFY", "False")
    monkeypatch.setenv("QUANTAI_ENV", "test")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")


@pytest.fixture
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("QUANTAI_DATA_DIR", str(data_dir))
    return data_dir
