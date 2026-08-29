"""performance — 交易绩效记录器（真源 L188–384 逐行迁移）。

在交易过程中持续追踪胜率、盈亏比、最大回撤、单笔风险占比等关键指标。
每次 CLOSE 触发时调用 record_trade()，每个 tick 可调用 update_equity() 更新权益曲线。
"""
import csv
import json
import logging
import os
from datetime import datetime

from . import config


class PerformanceMetrics:
    def __init__(self):
        self.trades = []               # [(pnl, entry_time, exit_time, direction, volume, entry_price, exit_price), ...]
        self.equity_curve = []         # [(timestamp, balance), ...]
        self.peak_balance = 0.0
        self.max_drawdown = 0.0
        self.peak_balance_time = None
        # 修复 M5: 启动时恢复历史交易与绩效状态，避免重启清零
        self._load_trades()
        self._load_perf_state()
        self._init_file()

    # ========== 修复 M5: 交易/绩效持久化 ==========
    def _load_trades(self):
        """从 JSONL 恢复历史交易记录（重启后胜率/盈亏比/连击不丢失）"""
        if not os.path.exists(config.TRADES_HISTORY_FILE):
            return
        try:
            with open(config.TRADES_HISTORY_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    trade = json.loads(line)
                    # 时间字段恢复为 datetime（兼容保存前的字符串化）
                    for k in ('entry_time', 'exit_time'):
                        v = trade.get(k)
                        if v:
                            try:
                                trade[k] = datetime.fromisoformat(v)
                            except Exception:
                                pass
                    self.trades.append(trade)
            logging.info(f"加载历史交易记录: {len(self.trades)} 笔")
        except Exception as e:
            logging.warning(f"加载历史交易失败: {e}")

    def _append_trade_file(self, trade: dict):
        """单条交易追加到 JSONL（O(1)）"""
        try:
            with open(config.TRADES_HISTORY_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps(trade, ensure_ascii=False) + '\n')
        except Exception as e:
            logging.warning(f"追加交易记录失败: {e}")

    def _save_perf_state(self):
        """保存 peak_balance/max_drawdown 以便跨重启恢复"""
        try:
            with open(config.PERF_STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'peak_balance': self.peak_balance,
                    'max_drawdown': self.max_drawdown,
                    'peak_balance_time': self.peak_balance_time.isoformat() if self.peak_balance_time else None,
                }, f, ensure_ascii=False)
        except Exception as e:
            logging.warning(f"保存绩效状态失败: {e}")

    def _load_perf_state(self):
        try:
            if os.path.exists(config.PERF_STATE_FILE):
                with open(config.PERF_STATE_FILE, 'r', encoding='utf-8') as f:
                    st = json.load(f)
                self.peak_balance = st.get('peak_balance', 0.0)
                self.max_drawdown = st.get('max_drawdown', 0.0)
                t = st.get('peak_balance_time')
                if t:
                    try:
                        self.peak_balance_time = datetime.fromisoformat(t)
                    except Exception:
                        pass
        except Exception as e:
            logging.warning(f"加载绩效状态失败: {e}")
    # ================================================

    def _init_file(self):
        self.metrics_file = config.METRICS_FILE
        if not os.path.exists(self.metrics_file):
            with open(self.metrics_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "balance", "peak_balance", "drawdown",
                    "trade_count", "win_count", "loss_count", "win_rate",
                    "avg_pnl", "avg_win", "avg_loss", "profit_factor",
                    "max_drawdown", "current_streak"
                ])

    def record_trade(self, pnl: float, direction: str, volume: int,
                     entry_price: float, exit_price: float,
                     entry_time: datetime, exit_time: datetime):
        """记录一次完整交易（开仓→平仓）"""
        # 修复 M5: 交易持久化到 JSONL，重启后仍可统计胜率/盈亏比
        trade = {
            'pnl': pnl, 'direction': direction, 'volume': volume,
            'entry_price': entry_price, 'exit_price': exit_price,
            'entry_time': entry_time.isoformat() if hasattr(entry_time, 'isoformat') else str(entry_time),
            'exit_time': exit_time.isoformat() if hasattr(exit_time, 'isoformat') else str(exit_time),
        }
        self.trades.append(trade)
        self._append_trade_file(trade)
        self._flush()

    # 修复 M5: 权益曲线保留最近 N 个点，防止无界增长
    EQUITY_CURVE_MAX = 1000  # 30s 一个点 ≈ 8.3 小时

    def update_equity(self, balance: float, when: datetime = None):
        """每个 tick 更新权益曲线，计算回撤"""
        if when is None:
            when = datetime.now()
        self.equity_curve.append((when, balance))
        if len(self.equity_curve) > self.EQUITY_CURVE_MAX:
            self.equity_curve = self.equity_curve[-self.EQUITY_CURVE_MAX:]
        if balance > self.peak_balance:
            self.peak_balance = balance
            self.peak_balance_time = when
        if self.peak_balance > 0:
            dd = (self.peak_balance - balance) / self.peak_balance * 100
            if dd > self.max_drawdown:
                self.max_drawdown = dd
        # 修复 M5: 周期持久化 peak/drawdown（调用点已限频 30s）
        self._save_perf_state()

    def summary(self) -> dict:
        """计算并返回当前统计指标"""
        if not self.trades:
            return {
                'trade_count': 0, 'win_count': 0, 'loss_count': 0,
                'win_rate': 0, 'avg_pnl': 0, 'avg_win': 0, 'avg_loss': 0,
                'profit_factor': 0, 'max_drawdown': self.max_drawdown,
                'current_streak': 0
            }
        wins = [t for t in self.trades if t['pnl'] > 0]
        losses = [t for t in self.trades if t['pnl'] <= 0]
        win_sum = sum(t['pnl'] for t in wins)
        loss_sum = abs(sum(t['pnl'] for t in losses))
        pf = win_sum / loss_sum if loss_sum > 0 else float('inf')
        # 连胜/连败
        streak = 0
        for t in reversed(self.trades):
            if (t['pnl'] > 0 and streak >= 0) or (t['pnl'] <= 0 and streak <= 0):
                streak += 1 if t['pnl'] > 0 else -1
            else:
                break
        return {
            'trade_count': len(self.trades),
            'win_count': len(wins),
            'loss_count': len(losses),
            'win_rate': len(wins) / len(self.trades) * 100,
            'avg_pnl': sum(t['pnl'] for t in self.trades) / len(self.trades),
            'avg_win': win_sum / len(wins) if wins else 0,
            'avg_loss': loss_sum / len(losses) if losses else 0,
            'profit_factor': pf,
            'max_drawdown': self.max_drawdown,
            'current_streak': streak
        }

    def _flush(self):
        """把当前统计指标写入文件（追加）"""
        s = self.summary()
        last_balance = self.equity_curve[-1][1] if self.equity_curve else 0
        try:
            with open(self.metrics_file, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    f"{last_balance:.2f}", f"{self.peak_balance:.2f}",
                    f"{self.max_drawdown:.2f}",
                    s['trade_count'], s['win_count'], s['loss_count'],
                    f"{s['win_rate']:.2f}",
                    f"{s['avg_pnl']:.2f}", f"{s['avg_win']:.2f}", f"{s['avg_loss']:.2f}",
                    f"{s['profit_factor']:.2f}" if s['profit_factor'] != float('inf') else "inf",
                    f"{s['max_drawdown']:.2f}", s['current_streak']
                ])
        except Exception as e:
            logging.error(f"写入绩效文件失败: {e}")

    def print_daily_report(self):
        """收盘后打印日报"""
        s = self.summary()
        report = f"""
===== 交易日报 =====
交易笔数: {s['trade_count']} (胜 {s['win_count']} / 负 {s['loss_count']})
胜率: {s['win_rate']:.2f}%
平均盈亏: {s['avg_pnl']:.2f} 元
平均盈利: {s['avg_win']:.2f} 元
平均亏损: {s['avg_loss']:.2f} 元
盈亏比: {(s['avg_win']/s['avg_loss']) if s['avg_loss'] > 0 else 0:.2f}
Profit Factor: {s['profit_factor']:.2f}
最大回撤: {s['max_drawdown']:.2f}%
当前连击: {s['current_streak']:+d}
"""
        logging.info(report)
        return report
