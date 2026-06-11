"""交易绩效指标：胜率、盈亏比、回撤、连胜.

每次平仓后调用 :py:meth:`PerformanceMetrics.record_trade`；
每个 tick 可调用 :py:meth:`update_equity` 更新权益曲线。
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import paths

logger = logging.getLogger(__name__)


@dataclass
class CompletedTrade:
    pnl: float
    direction: str
    volume: int
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime


@dataclass
class MetricsSummary:
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    current_streak: int = 0


class PerformanceMetrics:
    """绩效记录器：持续追踪胜率、盈亏比、最大回撤等指标."""

    HEADER = [
        "timestamp", "balance", "peak_balance", "drawdown",
        "trade_count", "win_count", "loss_count", "win_rate",
        "avg_pnl", "avg_win", "avg_loss", "profit_factor",
        "max_drawdown", "current_streak",
    ]

    def __init__(self, metrics_file: Optional[Path] = None) -> None:
        self.metrics_file: Path = Path(metrics_file) if metrics_file else paths["performance_metrics"]
        self.trades: list[CompletedTrade] = []
        self.equity_curve: list[tuple[datetime, float]] = []
        self.peak_balance: float = 0.0
        self.peak_balance_time: Optional[datetime] = None
        self.max_drawdown: float = 0.0
        self._init_file()

    def _init_file(self) -> None:
        self.metrics_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.metrics_file.exists():
            with self.metrics_file.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.HEADER)

    def record_trade(
        self,
        pnl: float,
        direction: str,
        volume: int,
        entry_price: float,
        exit_price: float,
        entry_time: datetime,
        exit_time: datetime,
    ) -> None:
        self.trades.append(
            CompletedTrade(
                pnl=pnl, direction=direction, volume=volume,
                entry_price=entry_price, exit_price=exit_price,
                entry_time=entry_time, exit_time=exit_time,
            )
        )
        self._flush()

    def update_equity(self, balance: float, when: Optional[datetime] = None) -> None:
        when = when or datetime.now()
        self.equity_curve.append((when, balance))
        if balance > self.peak_balance:
            self.peak_balance = balance
            self.peak_balance_time = when
        if self.peak_balance > 0:
            dd = (self.peak_balance - balance) / self.peak_balance * 100
            if dd > self.max_drawdown:
                self.max_drawdown = dd

    def summary(self) -> MetricsSummary:
        if not self.trades:
            return MetricsSummary(max_drawdown=self.max_drawdown)

        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        win_sum = sum(t.pnl for t in wins)
        loss_sum = abs(sum(t.pnl for t in losses))
        pf = win_sum / loss_sum if loss_sum > 0 else float("inf")

        streak = 0
        for t in reversed(self.trades):
            sign = 1 if t.pnl > 0 else -1
            if streak == 0 or (streak > 0 and sign > 0) or (streak < 0 and sign < 0):
                streak += sign
            else:
                break

        return MetricsSummary(
            trade_count=len(self.trades),
            win_count=len(wins),
            loss_count=len(losses),
            win_rate=len(wins) / len(self.trades) * 100,
            avg_pnl=sum(t.pnl for t in self.trades) / len(self.trades),
            avg_win=win_sum / len(wins) if wins else 0.0,
            avg_loss=loss_sum / len(losses) if losses else 0.0,
            profit_factor=pf,
            max_drawdown=self.max_drawdown,
            current_streak=streak,
        )

    def _flush(self) -> None:
        s = self.summary()
        last_balance = self.equity_curve[-1][1] if self.equity_curve else 0.0
        try:
            with self.metrics_file.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    f"{last_balance:.2f}", f"{self.peak_balance:.2f}",
                    f"{self.max_drawdown:.2f}",
                    s.trade_count, s.win_count, s.loss_count,
                    f"{s.win_rate:.2f}",
                    f"{s.avg_pnl:.2f}", f"{s.avg_win:.2f}", f"{s.avg_loss:.2f}",
                    f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "inf",
                    f"{s.max_drawdown:.2f}", s.current_streak,
                ])
        except Exception as exc:
            logger.error("Write performance metrics file failed: %s", exc)

    def print_daily_report(self) -> str:
        s = self.summary()
        rr = (s.avg_win / s.avg_loss) if s.avg_loss > 0 else 0
        report = (
            "\n===== 交易日报 =====\n"
            f"交易笔数: {s.trade_count} (胜 {s.win_count} / 负 {s.loss_count})\n"
            f"胜率: {s.win_rate:.2f}%\n"
            f"平均盈亏: {s.avg_pnl:.2f} 元\n"
            f"平均盈利: {s.avg_win:.2f} 元\n"
            f"平均亏损: {s.avg_loss:.2f} 元\n"
            f"盈亏比: {rr:.2f}\n"
            f"Profit Factor: {s.profit_factor:.2f}\n"
            f"最大回撤: {s.max_drawdown:.2f}%\n"
            f"当前连击: {s.current_streak:+d}\n"
        )
        logger.info(report)
        return report


__all__ = ["CompletedTrade", "MetricsSummary", "PerformanceMetrics"]
