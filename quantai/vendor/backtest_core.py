"""
Backtesting Core Module for IM Auto-Trading System
包含: MockTqApi, BacktestEngine, PerformanceAnalyzer
"""

import sys
import re
import json
import time
import logging
import threading
import csv
import os
from datetime import datetime, timedelta, time as dt_time
from typing import Dict, Tuple, Optional, List, Any
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict
import pandas as pd

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ========== MockTqApi ==========
class MockQuote:
    """模拟行情数据"""
    def __init__(self, symbol: str, price: float = 0.0):
        self.symbol = symbol
        self.last_price = price
        self.ask_price1 = price + 0.2 if price > 0 else 0
        self.bid_price1 = price - 0.2 if price > 0 else 0
        self.ask_volume1 = 10
        self.bid_volume1 = 10
        self.open_interest = 100000
        self.volume = 0
        self.highest = price * 1.01 if price > 0 else 0
        self.lowest = price * 0.99 if price > 0 else 0
        self.open = price
        self.close = price


class MockOrder:
    """模拟订单"""
    def __init__(self, order_id: str, symbol: str, direction: str, offset: str, 
                 volume: int, limit_price: float, status: str = "ALIVE"):
        self.order_id = order_id
        self.symbol = symbol
        self.direction = direction
        self.offset = offset
        self.volume_left = volume
        self.volume_orig = volume
        self.limit_price = limit_price
        self.status = status  # ALIVE, FINISHED, REJECTED, CANCELLED
        self.last_msg = ""
        self.trade_price = 0.0
        self.is_error = False


class MockPosition:
    """模拟持仓"""
    def __init__(self):
        self.volume_long = 0
        self.volume_short = 0
        self.open_price_long = 0.0
        self.open_price_short = 0.0
        self.position_long_his = 0
        self.position_short_his = 0
        self.position_long_gl = 0.0
        self.position_short_gl = 0.0


class MockAccount:
    """模拟账户"""
    def __init__(self, balance: float = 1000000.0):
        self.balance = balance
        self.position_profit = 0.0
        self.static_balance = balance


class MockTqApi:
    """
    模拟天勤API，用于回测
    必须与真实TqApi接口兼容
    """
    def __init__(self, auth=None, url=None):
        self._quotes: Dict[str, MockQuote] = {}
        self._orders: List[MockOrder] = []
        self._position: Dict[str, MockPosition] = defaultdict(MockPosition)
        self._account = MockAccount()
        self._symbol = "CFFEX.IM2506"  # 默认合约
        self._order_id_counter = 1000
        self._current_time = datetime.now()
        self._closed = False
        
    def get_quote(self, symbol: str) -> MockQuote:
        if symbol not in self._quotes:
            self._quotes[symbol] = MockQuote(symbol, 2500.0)
        return self._quotes[symbol]
    
    def get_position(self, symbol: str) -> MockPosition:
        if symbol not in self._position:
            self._position[symbol] = MockPosition()
        return self._position[symbol]
    
    def get_account(self) -> MockAccount:
        return self._account
    
    def insert_order(self, symbol: str, direction: str, offset: str, 
                    volume: int, limit_price: float) -> MockOrder:
        order_id = f"ORD_{self._order_id_counter}"
        self._order_id_counter += 1
        order = MockOrder(order_id, symbol, direction, offset, volume, limit_price)
        self._orders.append(order)
        return order
    
    def cancel_order(self, order: MockOrder):
        if order.status == "ALIVE":
            order.status = "CANCELLED"
    
    def wait_update(self, deadline=None, timeout=None):
        """回测中为no-op"""
        pass
    
    def get_contract_info(self, symbol: str) -> Dict:
        return {'expire_datetime': datetime(2025, 6, 15).timestamp()}
    
    def close(self):
        self._closed = True
    
    # ========== 回测专用方法 ==========
    def set_price(self, symbol: str, price: float):
        """设置模拟价格"""
        self._quotes[symbol] = MockQuote(symbol, price)
    
    def set_position_state(self, symbol: str, direction: str, volume: int, entry_price: float):
        """设置持仓状态"""
        pos = self._position[symbol]
        if direction == "LONG":
            pos.volume_long = volume
            pos.open_price_long = entry_price
        else:
            pos.volume_short = volume
            pos.open_price_short = entry_price
    
    def set_balance(self, balance: float):
        """设置账户余额"""
        self._account.balance = balance
        self._account.static_balance = balance
    
    def execute_pending_orders(self, current_price: float):
        """执行待成交订单（用于市价单的模拟成交）"""
        for order in self._orders:
            if order.status != "ALIVE":
                continue
            # 如果是市价单或价格达到限价
            if order.limit_price == 0 or (order.direction == "BUY" and current_price <= order.limit_price) or \
               (order.direction == "SELL" and current_price >= order.limit_price):
                order.status = "FINISHED"
                order.trade_price = current_price
                order.volume_left = 0


# ========== BacktestEngine ==========
class BacktestEngine:
    """
    回测引擎
    复现历史数据并运行交易策略
    """
    def __init__(self, config, index_fetcher):
        self.config = config
        self.index_fetcher = index_fetcher
        self.api = MockTqApi()
        self._quotes_history: Dict[str, List] = defaultdict(list)  # 历史行情
        self._current_idx = 0
        self._trades: List[Dict] = []  # 成交记录
        self._equity_curve: List[Dict] = []  # 权益曲线
        self._position = {
            "direction": None,
            "volume": 0,
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "take_profit": 0.0
        }
        self._balance = config.initial_balance
        self._initial_balance = config.initial_balance
        self._pending_orders: List[Dict] = []  # 待执行订单
        self._last_decision_time = None
        self._conditional_order = None
        
    def load_historical_data(self):
        """加载历史K线数据"""
        logger.info(f"加载历史数据: {self.config.start_date} ~ {self.config.end_date}")
        
        # 使用指数数据代替期货（忽略基差）
        index_name = "中证1000"
        periods = ["5min", "15min", "30min", "60min", "日线"]
        
        for period in periods:
            df = self.index_fetcher.get_kline_data(index_name, period)
            if df is not None and not df.empty:
                # 过滤日期范围
                df['datetime'] = pd.to_datetime(df['datetime'])
                df = df[(df['datetime'] >= self.config.start_date) & 
                      (df['datetime'] <= self.config.end_date)]
                self._quotes_history[period] = df.to_dict('records')
                logger.info(f"  {period}: {len(df)} 条")
    
    def _get_current_price(self, period: str = "5min") -> float:
        """获取当前时间的价格"""
        if period not in self._quotes_history or not self._quotes_history[period]:
            return 2500.0  # fallback
        idx = min(self._current_idx, len(self._quotes_history[period]) - 1)
        return self._quotes_history[period][idx].get('close', 2500.0)
    
    def _get_current_time(self) -> datetime:
        """获取当前时间"""
        if "5min" not in self._quotes_history or not self._quotes_history["5min"]:
            return datetime.now()
        idx = min(self._current_idx, len(self._quotes_history["5min"]) - 1)
        return self._quotes_history["5min"][idx].get('datetime', datetime.now())
    
    def _step(self):
        """单步执行"""
        current_price = self._get_current_price("5min")
        current_time = self._get_current_time()
        
        # 更新API价格
        self.api.set_price("CFFEX.IM2506", current_price)
        
        # 更新权益
        self._update_equity(current_price, current_time)
        
        # 检查止损止盈
        self._check_stop_profit(current_price)
        
        # 检查条件单
        self._check_conditional_order(current_price)
        
        # 模拟订单成交
        self.api.execute_pending_orders(current_price)
        
        # AI决策（每15分钟）
        if self._last_decision_time is None or \
           (current_time - self._last_decision_time).total_seconds() >= self.config.decision_interval:
            self._last_decision_time = current_time
            # 实际AI决策在这里调用
        
        self._current_idx += 1
        
    def _update_equity(self, price: float, dt: datetime):
        """更新权益曲线"""
        unreal_pnl = 0.0
        if self._position['direction'] == 'LONG':
            unreal_pnl = (price - self._position['entry_price']) * self._position['volume'] * 200
        elif self._position['direction'] == 'SHORT':
            unreal_pnl = (self._position['entry_price'] - price) * self._position['volume'] * 200
        
        equity = self._balance + unreal_pnl
        self._equity_curve.append({
            'datetime': dt,
            'price': price,
            'equity': equity,
            'unreal_pnl': unreal_pnl,
            'position': self._position.copy()
        })
    
    def _check_stop_profit(self, current_price: float):
        """检查止损止盈"""
        if not self._position['direction']:
            return
        
        trigger = None
        if self._position['direction'] == 'LONG':
            if current_price <= self._position['stop_loss'] > 0:
                trigger = "止损"
            elif current_price >= self._position['take_profit'] > 0:
                trigger = "止盈"
        elif self._position['direction'] == 'SHORT':
            if current_price >= self._position['stop_loss'] > 0:
                trigger = "止损"
            elif current_price <= self._position['take_profit'] > 0:
                trigger = "止盈"
        
        if trigger:
            self._close_position(trigger, current_price)
    
    def _check_conditional_order(self, current_price: float):
        """检查条件单"""
        if not self._conditional_order:
            return
        
        cond = self._conditional_order
        triggered = False
        if cond.get('trigger_type') == 'PRICE_ABOVE' and current_price >= cond.get('trigger_price', 0):
            triggered = True
        elif cond.get('trigger_type') == 'PRICE_BELOW' and current_price <= cond.get('trigger_price', 0):
            triggered = True
        
        if triggered:
            self._execute_entry(cond.get('action'), cond.get('volume', 1), 
                            cond.get('stop_loss', 0), cond.get('take_profit', 0),
                            current_price)
            self._conditional_order = None
    
    def _execute_entry(self, action: str, volume: int, stop_loss: float, 
                    take_profit: float, price: float):
        """执行入场"""
        direction = "LONG" if action == "BUY" else "SHORT"
        
        # 检查资金
        margin = price * 200 * 0.15 * volume
        if margin > self._balance:
            logger.warning(f"资金不足，跳过入场: 需要{margin}, 可用{self._balance}")
            return
        
        # 记录交易
        self._trades.append({
            'entry_time': self._get_current_time(),
            'direction': direction,
            'volume': volume,
            'entry_price': price,
            'stop_loss': stop_loss,
            'take_profit': take_profit
        })
        
        # 更新持仓
        self._position.update({
            'direction': direction,
            'volume': volume,
            'entry_price': price,
            'stop_loss': stop_loss,
            'take_profit': take_profit
        })
        
        # 冻结保证金
        self._balance -= margin
        logger.info(f"入场: {direction} {volume}手 @ {price}")
    
    def _close_position(self, reason: str, price: float):
        """平仓"""
        if not self._position['direction']:
            return
        
        entry_price = self._position['entry_price']
        volume = self._position['volume']
        direction = self._position['direction']
        
        # 计算盈亏
        if direction == 'LONG':
            pnl = (price - entry_price) * volume * 200
        else:
            pnl = (entry_price - price) * volume * 200
        
        # 释放保证金
        margin = entry_price * 200 * 0.15 * volume
        self._balance += margin + pnl
        
        # 记录交易
        if self._trades:
            self._trades[-1].update({
                'exit_time': self._get_current_time(),
                'exit_price': price,
                'pnl': pnl,
                'reason': reason
            })
        
        logger.info(f"平仓: {reason}, 盈亏: {pnl:.2f}")
        
        # 清空持仓
        self._position = {
            'direction': None,
            'volume': 0,
            'entry_price': 0.0,
            'stop_loss': 0.0,
            'take_profit': 0.0
        }
    
    def run(self) -> List[Dict]:
        """运行回测"""
        logger.info("开始回测...")
        self.load_historical_data()
        
        total_steps = len(self._quotes_history.get("5min", []))
        logger.info(f"总步数: {total_steps}")
        
        for i in range(total_steps):
            self._step()
            if (i + 1) % 1000 == 0:
                logger.info(f"进度: {i+1}/{total_steps}")
        
        logger.info("回测完成!")
        return self._trades
    
    def set_position(self, direction: str, volume: int, entry_price: float,
                    stop_loss: float = 0, take_profit: float = 0):
        """设置持仓（用于外部调用）"""
        self._position = {
            'direction': direction,
            'volume': volume,
            'entry_price': entry_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit
        }
    
    def set_conditional_order(self, order: Dict):
        """设置条件单"""
        self._conditional_order = order
    
    def get_equity_curve(self) -> List[Dict]:
        return self._equity_curve
    
    def get_final_balance(self) -> float:
        return self._balance


# ========== PerformanceAnalyzer ==========
class PerformanceAnalyzer:
    """
    性能分析器
    计算回测绩效指标
    """
    def __init__(self, trades: List[Dict], equity_curve: List[Dict], initial_balance: float):
        self.trades = trades
        self.equity_curve = equity_curve
        self.initial_balance = initial_balance
        
    def calculate(self) -> Dict:
        """计算所有绩效指标"""
        if not self.trades:
            return self._empty_metrics()
        
        # 基础数据
        closed_trades = [t for t in self.trades if 'pnl' in t]
        total_trades = len(closed_trades)
        if total_trades == 0:
            return self._empty_metrics()
        
        # 盈亏统计
        profits = [t['pnl'] for t in closed_trades]
        gross_profit = sum([p for p in profits if p > 0])
        gross_loss = abs(sum([p for p in profits if p < 0]))
        
        wins = sum([1 for p in profits if p > 0])
        losses = sum([1 for p in profits if p < 0])
        
        # 总收益率
        final_equity = self.equity_curve[-1]['equity'] if self.equity_curve else self.initial_balance
        total_return = (final_equity - self.initial_balance) / self.initial_balance
        
        # 夏普比率（简化版，年化）
        if len(self.equity_curve) > 1:
            returns = []
            for i in range(1, len(self.equity_curve)):
                ret = (self.equity_curve[i]['equity'] - self.equity_curve[i-1]['equity']) / self.equity_curve[i-1]['equity']
                returns.append(ret)
            avg_return = sum(returns) / len(returns) if returns else 0
            std_return = (sum([(r - avg_return) ** 2 for r in returns]) / len(returns)) ** 0.5 if returns else 1
            sharpe = (avg_return / std_return * (252 ** 0.5)) if std_return > 0 else 0
        else:
            sharpe = 0
        
        # 最大回撤
        peak = self.initial_balance
        max_drawdown = 0
        for eq in self.equity_curve:
            if eq['equity'] > peak:
                peak = eq['equity']
            dd = (peak - eq['equity']) / peak if peak > 0 else 0
            if dd > max_drawdown:
                max_drawdown = dd
        
        # 胜率
        win_rate = wins / total_trades if total_trades > 0 else 0
        
        # 盈亏比
        avg_win = gross_profit / wins if wins > 0 else 0
        avg_loss = gross_loss / losses if losses > 0 else 1
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        # 平均持仓时间
        holding_times = []
        for t in closed_trades:
            if 'entry_time' in t and 'exit_time' in t:
                hours = (t['exit_time'] - t['entry_time']).total_seconds() / 3600
                holding_times.append(hours)
        avg_holding = sum(holding_times) / len(holding_times) if holding_times else 0
        
        return {
            'total_return': total_return * 100,  # 百分比
            'total_return_pct': f"{total_return * 100:.2f}%",
            'sharpe_ratio': round(sharpe, 2),
            'max_drawdown': round(max_drawdown * 100, 2),
            'max_drawdown_pct': f"{max_drawdown * 100:.2f}%",
            'win_rate': round(win_rate * 100, 2),
            'win_rate_pct': f"{win_rate * 100:.2f}%",
            'profit_factor': round(profit_factor, 2),
            'total_trades': total_trades,
            'wins': wins,
            'losses': losses,
            'gross_profit': round(gross_profit, 2),
            'gross_loss': round(gross_loss, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'avg_holding_hours': round(avg_holding, 2),
            'final_equity': round(final_equity, 2)
        }
    
    def _empty_metrics(self) -> Dict:
        return {
            'total_return_pct': '0.00%',
            'sharpe_ratio': 0,
            'max_drawdown_pct': '0.00%',
            'win_rate_pct': '0.00%',
            'profit_factor': 0,
            'total_trades': 0,
            'wins': 0,
            'losses': 0,
            'gross_profit': 0,
            'gross_loss': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'avg_holding_hours': 0,
            'final_equity': self.initial_balance
        }
    
    def print_report(self):
        """打印报告"""
        metrics = self.calculate()
        print("\n" + "="*50)
        print(" 回测绩效报告 ")
        print("="*50)
        print(f"  总收益率:     {metrics['total_return_pct']}")
        print(f"  夏普比率:     {metrics['sharpe_ratio']}")
        print(f"  最大回撤:     {metrics['max_drawdown_pct']}")
        print(f"  胜率:        {metrics['win_rate_pct']}")
        print(f"  盈亏比:      {metrics['profit_factor']}")
        print(f"  总交易次数:   {metrics['total_trades']}")
        print(f"  盈利次数:    {metrics['wins']}")
        print(f"  亏损次数:    {metrics['losses']}")
        print(f"  总盈利:      {metrics['gross_profit']}")
        print(f"  总亏损:      {metrics['gross_loss']}")
        print(f"  平均盈利:    {metrics['avg_win']}")
        print(f"  平均亏损:    {metrics['avg_loss']}")
        print(f"  平均持仓时间: {metrics['avg_holding_hours']}小时")
        print(f"  最终权益:    {metrics['final_equity']}")
        print("="*50 + "\n")
        return metrics
    
    def save_to_csv(self, filepath: str):
        """保存交易记录"""
        if not self.trades:
            return
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['入场时间', '出场时间', '方向', '手数', '入场价', '出场价', '盈亏', '原因'])
            for t in self.trades:
                writer.writerow([
                    t.get('entry_time', '').strftime('%Y-%m-%d %H:%M:%S') if t.get('entry_time') else '',
                    t.get('exit_time', '').strftime('%Y-%m-%d %H:%M:%S') if t.get('exit_time') else '',
                    t.get('direction', ''),
                    t.get('volume', 0),
                    t.get('entry_price', 0),
                    t.get('exit_price', 0),
                    t.get('pnl', 0),
                    t.get('reason', '')
                ])
        logger.info(f"交易记录已保存: {filepath}")
    
    def save_equity_to_csv(self, filepath: str):
        """保存权益曲线"""
        if not self.equity_curve:
            return
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['时间', '价格', '权益', '未实现盈亏', '持仓方向', '持仓手数'])
            for eq in self.equity_curve:
                writer.writerow([
                    eq.get('datetime', '').strftime('%Y-%m-%d %H:%M:%S') if eq.get('datetime') else '',
                    eq.get('price', 0),
                    eq.get('equity', 0),
                    eq.get('unreal_pnl', 0),
                    eq.get('position', {}).get('direction', ''),
                    eq.get('position', {}).get('volume', 0)
                ])
        logger.info(f"权益曲线已保存: {filepath}")


# ========== ParameterOptimizer ==========
class ParameterOptimizer:
    """
    参数优化器
    使用网格搜索优化策略参数
    """
    def __init__(self, config, index_fetcher):
        self.config = config
        self.index_fetcher = index_fetcher
        self.results: List[Dict] = []
        
    def grid_search(self, param_grid: Dict[str, List], metric: str = 'sharpe_ratio') -> Dict:
        """
        网格搜索
        
        Args:
            param_grid: 参数网格，如 {'MIN_CONFIDENCE': [0.5, 0.55, 0.6]}
            metric: 优化指标
            
        Returns:
            最佳参数组合
        """
        logger.info(f"开始网格搜索: {param_grid}")
        
        # 生成所有参数组合
        import itertools
        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combinations = list(itertools.product(*values))
        
        logger.info(f"共 {len(combinations)} 种组合")
        
        for i, combo in enumerate(combinations):
            params = dict(zip(keys, combo))
            logger.info(f"  [{i+1}/{len(combinations)}] 测试参数: {params}")
            
            # 运行回测
            engine = BacktestEngine(self.config, self.index_fetcher)
            # 这里需要注入参数，然后在外部调用时使用
            trades = engine.run()
            
            # 计算指标
            analyzer = PerformanceAnalyzer(
                trades, 
                engine.get_equity_curve(),
                self.config.initial_balance
            )
            metrics = analyzer.calculate()
            
            self.results.append({
                'params': params,
                'metrics': metrics,
                'metric_value': metrics.get(metric, 0)
            })
        
        # 排序
        self.results.sort(key=lambda x: x['metric_value'], reverse=True)
        
        # 返回最佳
        best = self.results[0] if self.results else {}
        logger.info(f"最佳参数: {best.get('params')}")
        logger.info(f"最佳指标: {best.get('metric_value')}")
        
        return best
    
    def get_top_results(self, n: int = 10) -> List[Dict]:
        return self.results[:n]
    
    def save_results(self, filepath: str):
        """保存优化结果"""
        if not self.results:
            return
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['参数', '总收益率', '夏普比率', '最大回撤', '胜率', '盈亏比', '交易次数'])
            for r in self.results:
                writer.writerow([
                    str(r['params']),
                    r['metrics'].get('total_return_pct', ''),
                    r['metrics'].get('sharpe_ratio', ''),
                    r['metrics'].get('max_drawdown_pct', ''),
                    r['metrics'].get('win_rate_pct', ''),
                    r['metrics'].get('profit_factor', ''),
                    r['metrics'].get('total_trades', 0)
                ])
        logger.info(f"优化结果已保存: {filepath}")