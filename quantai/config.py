"""配置中心.

所有凭证（账户/密码/Webhook/API Key）从 .env 读取；
业务参数集中管理，避免散落在源码中。

加载顺序：
1. 项目根目录 .env（``QuantAI/.env``）
2. 当前工作目录 .env
3. 包内 .env（仅开发兜底）

启动检查：在 :func:`ensure_credentials` 中显式断言关键凭证存在，
缺失则拒绝启动，避免在交易途中静默失败。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent


def _load_env() -> Path | None:
    candidates = [
        PROJECT_ROOT / ".env",
        Path.cwd() / ".env",
        PACKAGE_ROOT / ".env",
    ]
    for path in candidates:
        if path.exists():
            load_dotenv(path, override=False)
            return path
    return None


LOADED_ENV_PATH = _load_env()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class AccountConfig:
    """天勤账户与运行模式."""

    account: str = field(default_factory=lambda: os.getenv("TQ_ACCOUNT", ""))
    password: str = field(default_factory=lambda: os.getenv("TQ_PASSWORD", ""))
    use_sim: bool = field(default_factory=lambda: _env_bool("TQ_USE_SIM", True))

    def assert_ready(self) -> None:
        if not self.account or not self.password:
            raise RuntimeError(
                "缺少天勤凭证：请在项目根目录 .env 中配置 TQ_ACCOUNT 与 TQ_PASSWORD。"
                "示例参见 .env.example。"
            )


@dataclass(frozen=True)
class LLMConfig:
    """LLM 客户端配置（OpenAI Compatible）."""

    api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", ""))
    model_id: str = field(default_factory=lambda: os.getenv("LLM_MODEL_ID", ""))
    bot_finance_model_id: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL_BOT_FINANCE_ID", "")
    )
    bot_finance_base_url: str = field(
        default_factory=lambda: os.getenv("LLM_BASE_BOT_FINANCE_URL", "")
    )

    def assert_ready(self) -> None:
        missing = [n for n, v in (("LLM_API_KEY", self.api_key), ("LLM_MODEL_ID", self.model_id)) if not v]
        if missing:
            raise RuntimeError(f"缺少 LLM 配置项: {missing}. 请检查 .env。")


@dataclass(frozen=True)
class DingTalkConfig:
    """钉钉机器人 Webhook 配置."""

    webhook: str = field(default_factory=lambda: os.getenv("DINGTALK_WEBHOOK", ""))
    secret: str = field(default_factory=lambda: os.getenv("DINGTALK_SECRET", ""))
    enabled: bool = field(default_factory=lambda: _env_bool("ENABLE_NOTIFY", True))


@dataclass(frozen=True)
class RuntimeConfig:
    """运行环境配置."""

    env: str = field(default_factory=lambda: os.getenv("QUANTAI_ENV", "paper"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class TradingConfig:
    """业务策略参数（可硬编码，可调参；非凭证类）."""

    symbol_prefix: str = "CFFEX.IM"
    index_name: str = "中证1000"
    contract_multiplier: int = 200
    min_price_tick: float = 0.2

    margin_rate: float = 0.15
    max_capital_usage: float = 0.60
    max_risk_per_trade_pct: float = 0.02
    min_confidence: float = 0.55

    base_decision_interval: int = 900
    short_term_interval: int = 300
    min_decision_interval: int = 300
    max_decision_interval: int = 1200

    scalping_atr_ratio: float = 1.3
    breakout_threshold: float = 0.3
    stop_adjust_cooldown: int = 300

    stop_relax_required_confidence: float = 0.75
    min_stop_distance_atr_mult: float = 0.8
    min_stop_distance_atr_mult_cond: float = 0.6
    add_required_confidence: float = 0.85
    add_min_price_gap_atr: float = 1.0
    add_max_drawdown_pct: float = 1.5
    max_position_lots: int = 3
    stopout_cooldown_sec: int = 900
    emergency_auto_reset_sec: int = 1800

    stress_threshold_pause: float = 2.0
    stress_threshold_extreme: float = 3.0
    rollover_days_threshold: int = 2
    near_close_minutes: int = 5

    atr_period: int = 14
    kline_data_length: int = 200


def get_paths() -> dict[str, Path]:
    """运行时产物路径（持仓/日志/决策日志）."""
    data = RuntimeConfig().data_dir
    return {
        "position_file": data / "position_state.pkl",
        "trade_log": data / "trade_log.csv",
        "ai_decisions": data / "ai_decisions.jsonl",
        "performance_metrics": data / "performance_metrics.csv",
        "trading_log": data / "trading.log",
    }


account = AccountConfig()
llm = LLMConfig()
dingtalk = DingTalkConfig()
runtime = RuntimeConfig()
trading = TradingConfig()
paths = get_paths()


def ensure_credentials(*, require_llm: bool = True) -> None:
    """启动前显式校验凭证；任一缺失立即抛错."""
    account.assert_ready()
    if require_llm:
        llm.assert_ready()
    if dingtalk.enabled and not dingtalk.webhook:
        raise RuntimeError(
            "ENABLE_NOTIFY=True 但缺少 DINGTALK_WEBHOOK。"
            "请配置 webhook 或设置 ENABLE_NOTIFY=False 关闭通知。"
        )


__all__ = [
    "PACKAGE_ROOT",
    "PROJECT_ROOT",
    "LOADED_ENV_PATH",
    "AccountConfig",
    "LLMConfig",
    "DingTalkConfig",
    "RuntimeConfig",
    "TradingConfig",
    "account",
    "llm",
    "dingtalk",
    "runtime",
    "trading",
    "paths",
    "ensure_credentials",
]
