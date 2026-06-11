"""配置层加载校验单测."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _reload_config(monkeypatch: pytest.MonkeyPatch):
    """每个测试重载 config 以反映新的环境变量."""
    import quantai.config as cfg
    importlib.reload(cfg)
    yield
    importlib.reload(cfg)


class TestEnvLoading:
    def test_account_loaded(self) -> None:
        from quantai.config import account
        assert account.account == "test_account"
        assert account.password == "test_password"
        assert account.use_sim is True

    def test_llm_loaded(self) -> None:
        from quantai.config import llm
        assert llm.api_key == "test_llm_key"
        assert llm.model_id == "gpt-4o-mini"

    def test_trading_defaults(self) -> None:
        from quantai.config import trading
        assert trading.contract_multiplier == 200
        assert trading.margin_rate == 0.15
        assert 0 < trading.min_confidence < 1
        assert trading.stopout_cooldown_sec > 0


class TestCredentialsValidation:
    def test_missing_password_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TQ_PASSWORD", "")
        import quantai.config as cfg
        importlib.reload(cfg)
        with pytest.raises(RuntimeError, match="缺少天勤凭证"):
            cfg.ensure_credentials(require_llm=False)

    def test_missing_llm_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_API_KEY", "")
        import quantai.config as cfg
        importlib.reload(cfg)
        with pytest.raises(RuntimeError, match="缺少 LLM 配置项"):
            cfg.ensure_credentials(require_llm=True)

    def test_notify_enabled_but_webhook_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_NOTIFY", "True")
        monkeypatch.setenv("DINGTALK_WEBHOOK", "")
        import quantai.config as cfg
        importlib.reload(cfg)
        with pytest.raises(RuntimeError, match="DINGTALK_WEBHOOK"):
            cfg.ensure_credentials(require_llm=False)

    def test_pass_when_all_set(self) -> None:
        import quantai.config as cfg
        cfg.ensure_credentials(require_llm=True)


class TestPaths:
    def test_paths_pointing_under_data_dir(self) -> None:
        from quantai.config import paths, runtime
        for key, p in paths.items():
            assert str(p).startswith(str(runtime.data_dir)), (
                f"path {key}={p} not under data_dir={runtime.data_dir}"
            )
