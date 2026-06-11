"""日志与交易事件落地 (CSV + RotatingFile)."""
from __future__ import annotations

import csv
import logging
import threading
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

from .config import paths, runtime
from .models import TradeEvent


def setup_logging(log_file: Optional[Path] = None, level: Optional[str] = None) -> logging.Logger:
    """配置根 logger：控制台 + 按天轮转的文件 handler."""
    log_file = log_file or paths["trading_log"]
    level = level or runtime.log_level
    log_file.parent.mkdir(parents=True, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        str(log_file), when="midnight", backupCount=7, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [file_handler, console_handler]
    return root


class TradeLogger:
    """线程安全的 CSV append-only 交易事件日志."""

    HEADER = [
        "timestamp", "event_type", "symbol", "direction", "volume",
        "price", "pnl", "balance_after", "ai_reason",
    ]

    def __init__(self, log_file: Optional[Path] = None) -> None:
        self.log_file: Path = Path(log_file) if log_file else paths["trade_log"]
        self._lock = threading.Lock()
        self._init_csv()

    def _init_csv(self) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_file.exists():
            with self.log_file.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.HEADER)

    def log_event(self, event: TradeEvent) -> None:
        with self._lock:
            with self.log_file.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(event.to_row())

    def log(
        self,
        event_type: str,
        symbol: str,
        direction: Optional[str],
        volume: int,
        price: float,
        pnl: float = 0.0,
        balance_after: float = 0.0,
        ai_reason: str = "",
    ) -> None:
        self.log_event(
            TradeEvent(
                event_type=event_type,
                symbol=symbol,
                direction=direction or "",
                volume=volume,
                price=price,
                pnl=pnl,
                balance_after=balance_after,
                ai_reason=ai_reason,
            )
        )


__all__ = ["setup_logging", "TradeLogger"]
