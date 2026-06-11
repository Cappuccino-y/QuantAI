"""核心数据模型 (frozen dataclasses + 状态对象)."""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal, Optional

DirectionType = Optional[Literal["LONG", "SHORT"]]
ActionType = Literal["BUY", "SELL", "WAIT"]
TriggerType = Literal["PRICE_ABOVE", "PRICE_BELOW"]
TradeMode = Literal["SWING", "SCALPING", "IDLE"]


@dataclass
class Position:
    """实时持仓状态."""

    direction: DirectionType = None
    volume: int = 0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    last_ai_decision: str = ""
    entry_time: Optional[datetime] = None

    @property
    def is_empty(self) -> bool:
        return self.direction is None or self.volume == 0

    def copy(self) -> "Position":
        return copy.deepcopy(self)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        et = data.get("entry_time")
        if isinstance(et, str):
            try:
                et = datetime.strptime(et, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                et = None
        return cls(
            direction=data.get("direction"),
            volume=data.get("volume", 0),
            entry_price=data.get("entry_price", 0.0),
            stop_loss=data.get("stop_loss", 0.0),
            take_profit=data.get("take_profit", 0.0),
            last_ai_decision=data.get("last_ai_decision", ""),
            entry_time=et,
        )


@dataclass
class ConditionalOrder:
    """AI 给出的条件单."""

    trigger_type: TriggerType
    trigger_price: float
    stop_loss: float
    take_profit: float
    action: ActionType
    volume: int = 1
    limit_price: float = 0.0
    reason: str = ""
    source: str = "ai"
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ConditionalOrder":
        return cls(
            trigger_type=data.get("trigger_type", "PRICE_ABOVE"),
            trigger_price=float(data.get("trigger_price", 0)),
            stop_loss=float(data.get("stop_loss", 0)),
            take_profit=float(data.get("take_profit", 0)),
            action=data.get("action", "BUY"),
            volume=int(data.get("volume", 1)),
            limit_price=float(data.get("limit_price", 0)),
            reason=data.get("reason", ""),
            source=data.get("source", "ai"),
            extra={k: v for k, v in data.items()
                   if k not in {"trigger_type", "trigger_price", "stop_loss",
                                "take_profit", "action", "volume", "limit_price",
                                "reason", "source"}},
        )


@dataclass
class TradeEvent:
    """交易日志条目."""

    event_type: str
    symbol: str
    direction: str
    volume: int
    price: float
    pnl: float = 0.0
    balance_after: float = 0.0
    ai_reason: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def to_row(self) -> list[str]:
        return [
            self.timestamp,
            self.event_type,
            self.symbol,
            self.direction or "",
            str(self.volume),
            f"{self.price:.2f}",
            f"{self.pnl:.2f}",
            f"{self.balance_after:.2f}",
            self.ai_reason,
        ]


@dataclass
class AIData:
    """ATR 与应激指标."""

    atr_5: float = 0.0
    atr_15: float = 0.0
    atr_60: float = 0.0
    stress_level: float = 1.0

    @property
    def is_high_volatility(self) -> bool:
        return self.stress_level >= 2.0

    @property
    def is_extreme_volatility(self) -> bool:
        return self.stress_level >= 3.0


@dataclass
class BasisInfo:
    """基差与合约状态."""

    index_price: float
    im_price: float
    basis: float
    basis_pct: float
    days_to_expiry: int
    symbol: str


@dataclass
class AIDecision:
    """LLM 返回的决策（解析后规整化）."""

    action: ActionType
    volume: int
    stop_loss: float
    take_profit: float
    confidence: float
    reason: str = ""
    conditional_entry: Optional[ConditionalOrder] = None
    adjust_existing: Optional[dict] = None
    next_interval_sec: int = 900
    mode: TradeMode = "SWING"
    raw: dict = field(default_factory=dict)


__all__ = [
    "DirectionType",
    "ActionType",
    "TriggerType",
    "TradeMode",
    "Position",
    "ConditionalOrder",
    "TradeEvent",
    "AIData",
    "BasisInfo",
    "AIDecision",
]
