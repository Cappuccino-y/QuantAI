"""models — 结构化数据模型（dataclass）。

字段来源核对记录:
- Position: 真源 current_position 全局 dict（L147–154），pkl 格式保持兼容
- ConditionalOrder: AI 路径（L2245–2313: trigger_price/stop_loss/take_profit/action/volume
  + created_date L2305）∪ 午盘路径（L4238–4250: trigger_type/limit_price/source/
  kospi_amp/kospi_delta/force_close_time）；pkl 中以 plain dict 存储故提供 to_dict/from_dict
- AIDecision: 真源对 decision 的全部键访问（grep 核实）: action/adjust_existing/
  adjust_stop_loss/adjust_take_profit/conditional_entry/confidence/limit_price/
  next_interval_sec/reason/stop_loss/take_profit/volume
- TradeEvent: trade_log.csv 列结构（TradeLogger._init_csv L171–174）
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


# ---------- 持仓（pkl 兼容） ----------

@dataclass
class Position:
    """镜像真源 current_position dict（L147–154），字段名逐项一致。"""
    direction: Optional[str] = None   # "LONG" or "SHORT"
    volume: int = 0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    last_ai_decision: str = ""

    def is_empty(self) -> bool:
        return self.direction is None or self.volume == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "direction": self.direction,
            "volume": self.volume,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "last_ai_decision": self.last_ai_decision,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Position":
        return cls(
            direction=d.get("direction"),
            volume=int(d.get("volume") or 0),
            entry_price=float(d.get("entry_price") or 0.0),
            stop_loss=float(d.get("stop_loss") or 0.0),
            take_profit=float(d.get("take_profit") or 0.0),
            last_ai_decision=d.get("last_ai_decision") or "",
        )


# ---------- 条件单（pkl 兼容） ----------

@dataclass
class ConditionalOrder:
    """AI 条件单（L2245–2313）与午盘顺势单（L4238–4250）的统一模型。

    pkl 中以 plain dict 存储，from_dict 对缺失键容忍（旧 pkl 无 created_date 等字段）。
    """
    action: str = ""                  # "BUY" / "SELL"
    trigger_type: str = ""            # 触发方式（午盘路径写入；AI 路径由 conditional_entry 携带）
    trigger_price: float = 0.0        # 已转期货价（conv）
    limit_price: float = 0.0          # 0 = 用对手价成交
    stop_loss: float = 0.0
    take_profit: float = 0.0
    volume: int = 0
    source: str = ""                  # 来源标记，如 "12:50_lunch_breakout"
    created_date: str = ""            # 8/27 修复: 防条件单跨日/跨周末残留
    kospi_amp: Optional[float] = None   # 午盘路径专属
    kospi_delta: Optional[float] = None  # 午盘路径专属
    force_close_time: Optional[str] = None  # 午盘路径专属，如 "14:00"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "trigger_type": self.trigger_type,
            "trigger_price": self.trigger_price,
            "limit_price": self.limit_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "volume": self.volume,
            "source": self.source,
            "created_date": self.created_date,
            "kospi_amp": self.kospi_amp,
            "kospi_delta": self.kospi_delta,
            "force_close_time": self.force_close_time,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ConditionalOrder":
        return cls(
            action=d.get("action") or "",
            trigger_type=d.get("trigger_type") or "",
            trigger_price=float(d.get("trigger_price") or 0.0),
            limit_price=float(d.get("limit_price") or 0.0),
            stop_loss=float(d.get("stop_loss") or 0.0),
            take_profit=float(d.get("take_profit") or 0.0),
            volume=int(d.get("volume") or 0),
            source=d.get("source") or "",
            created_date=d.get("created_date") or "",
            kospi_amp=d.get("kospi_amp"),
            kospi_delta=d.get("kospi_delta"),
            force_close_time=d.get("force_close_time"),
        )


# ---------- AI 决策 ----------

@dataclass
class AIDecision:
    """LLM 返回的决策 JSON（键集合按真源 grep 核实，见模块 docstring）。"""
    action: str = "WAIT"              # BUY / SELL / WAIT（真源 L2246 默认 WAIT）
    confidence: float = 0.0
    reason: str = ""
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    volume: Optional[int] = None
    limit_price: Optional[float] = None
    conditional_entry: Optional[Dict[str, Any]] = None
    adjust_existing: Optional[Dict[str, Any]] = None
    adjust_stop_loss: Optional[float] = None
    adjust_take_profit: Optional[float] = None
    next_interval_sec: Optional[int] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AIDecision":
        return cls(
            action=d.get("action", "WAIT"),
            confidence=float(d.get("confidence", 0) or 0),
            reason=d.get("reason") or "",
            stop_loss=d.get("stop_loss"),
            take_profit=d.get("take_profit"),
            volume=d.get("volume"),
            limit_price=d.get("limit_price"),
            conditional_entry=d.get("conditional_entry"),
            adjust_existing=d.get("adjust_existing"),
            adjust_stop_loss=d.get("adjust_stop_loss"),
            adjust_take_profit=d.get("adjust_take_profit"),
            next_interval_sec=d.get("next_interval_sec"),
        )


# ---------- 交易事件（trade_log.csv 行） ----------

@dataclass
class TradeEvent:
    """对应 TradeLogger CSV 列结构（真源 L171–174）。"""
    event_type: str
    symbol: str
    direction: Optional[str]
    volume: int
    price: float
    pnl: float = 0.0
    balance_after: float = 0.0
    ai_reason: str = ""
    timestamp: Optional[datetime] = None


# ---------- 策略层结构化信号（strategies 子包输出，渲染/告警由上层负责） ----------

class SignalRegime(str, Enum):
    """大盘定调 regime（MA60/200）。"""
    BULL = "BULL"
    BEAR = "BEAR"
    NEUTRAL = "NEUTRAL"


@dataclass
class LeftSideSignal:
    """左侧信号结构化输出（真源 _compute_left_side_signals L1608–2050 的计算段）。

    设计要点 1（计算与渲染分离）: 策略只产出本结构；prompt 文本由 ai_decision.PromptBuilder
    渲染，钉钉告警由 notifier 发送。字段在 phase 3 迁移时按信号实际载荷细化。
    """
    name: str                          # L12a / L3 / D17 / D0 / GAP_FILL / REGIME
    direction: str                     # "LONG" / "SHORT" / "NEUTRAL"
    triggered: bool = False
    strength: float = 0.0
    detail: str = ""
    sl_suggestion: Optional[float] = None
    tp_suggestion: Optional[float] = None
    created_at: Optional[datetime] = None


@dataclass
class FilterResult:
    """过滤器/豁免链统一输出。allowed=False 时 reason 必填（用于日志与告警）。"""
    allowed: bool
    reason: str = ""
    filter_name: str = ""


# ---------- 日韩联动 ----------

@dataclass
class JPIndexSnapshot:
    """日经/KOSPI 单次快照（fetch_jp_indices 输出，字段在 phase 2 迁移时核对）。"""
    name: str
    price: float = 0.0
    pct_change: float = 0.0
    fetched_at: Optional[datetime] = None


@dataclass
class LunchContext:
    """午盘联动上下文（真源 self.lunch_context dict + _refresh_lunch_context L3803–3808）。

    真源是通用 KV（任意 key + update_time），本模型提供类型化访问 + 动态键兜底；
    已知键按 3800–4422 区间写入点归纳，phase 3 迁移时逐点核对。
    """
    values: Dict[str, Any] = field(default_factory=dict)
    update_time: str = ""

    def set(self, key: str, value: Any) -> None:
        """对齐 _refresh_lunch_context: 写入 key + 刷新 update_time。"""
        self.values[key] = value
        self.update_time = datetime.now().strftime("%H:%M:%S")

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)
