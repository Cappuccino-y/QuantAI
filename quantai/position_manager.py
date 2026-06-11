"""持仓状态管理：pickle 持久化 + 云端一致性校验.

封装全局 ``Position`` 与 ``ConditionalOrder``；
启动时与天勤 API 真实持仓双向校对，避免因手动平仓/上次崩溃导致状态漂移。
"""
from __future__ import annotations

import logging
import pickle
import threading
from pathlib import Path
from typing import Any, Optional

from .config import paths
from .models import ConditionalOrder, Position

logger = logging.getLogger(__name__)


class PositionManager:
    """线程安全的持仓状态容器 + 持久化."""

    def __init__(self, state_file: Optional[Path] = None) -> None:
        self.state_file: Path = Path(state_file) if state_file else paths["position_file"]
        self._lock = threading.RLock()
        self._position: Position = Position()
        self._conditional: Optional[ConditionalOrder] = None
        self._last_pnl: float = 0.0

    @property
    def position(self) -> Position:
        with self._lock:
            return self._position.copy()

    @property
    def conditional_order(self) -> Optional[ConditionalOrder]:
        with self._lock:
            return self._conditional

    @property
    def last_pnl(self) -> float:
        return self._last_pnl

    @last_pnl.setter
    def last_pnl(self, value: float) -> None:
        self._last_pnl = value

    def update_position(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self._position, k):
                    setattr(self._position, k, v)
            self.save()

    def replace_position(self, position: Position) -> None:
        with self._lock:
            self._position = position
            self.save()

    def clear_position(self) -> None:
        with self._lock:
            self._position = Position()
            self.save()

    def set_conditional(self, cond: Optional[ConditionalOrder]) -> None:
        with self._lock:
            self._conditional = cond
            self.save()

    def load(self) -> None:
        if not self.state_file.exists():
            logger.info("No prior position state file at %s", self.state_file)
            return
        try:
            with self.state_file.open("rb") as f:
                state = pickle.load(f)
            if isinstance(state, dict) and "position" in state:
                pos_data = state["position"]
                cond_data = state.get("conditional_order")
            else:
                pos_data = state
                cond_data = None
            if isinstance(pos_data, dict):
                self._position = Position.from_dict(pos_data)
            elif isinstance(pos_data, Position):
                self._position = pos_data
            if cond_data is not None:
                if isinstance(cond_data, ConditionalOrder):
                    self._conditional = cond_data
                elif isinstance(cond_data, dict):
                    self._conditional = ConditionalOrder.from_dict(cond_data)
            logger.info("Loaded position state: %s, conditional=%s",
                        self._position, self._conditional)
        except Exception as exc:
            logger.error("Load position state failed: %s", exc)

    def save(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "position": self._position.to_dict(),
                "conditional_order": self._conditional.to_dict() if self._conditional else None,
            }
            with self.state_file.open("wb") as f:
                pickle.dump(payload, f)
        except Exception as exc:
            logger.error("Save position state failed: %s", exc)

    def reconcile_with_broker(
        self,
        broker_direction: Optional[str],
        broker_volume: int,
        broker_entry: float,
    ) -> bool:
        """与券商持仓比对；不一致则用券商真值覆盖本地并返回 True."""
        with self._lock:
            local_dir = self._position.direction
            local_vol = self._position.volume

            inconsistent = (broker_direction != local_dir) or (broker_volume != local_vol)
            if not inconsistent and broker_direction is not None:
                if abs(broker_entry - self._position.entry_price) > 0.01:
                    self._position.entry_price = broker_entry
                    self.save()
                    logger.info("Sync entry price to broker: %.2f", broker_entry)
                return False

            if inconsistent:
                logger.warning(
                    "Position drift detected: local=%s %s lots vs broker=%s %s lots",
                    local_dir, local_vol, broker_direction, broker_volume,
                )
                self._position.direction = broker_direction
                self._position.volume = broker_volume
                self._position.entry_price = broker_entry
                if broker_direction is None:
                    self._position.stop_loss = 0.0
                    self._position.take_profit = 0.0
                self.save()
                return True
            return False


__all__ = ["PositionManager"]
