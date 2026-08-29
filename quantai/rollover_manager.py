"""rollover_manager — 换月自动化（真源 2 个方法，design.md §4.2 rollover_manager 表）。

方法映射:
- RolloverManager.rollover_if_needed  ← rollover_if_needed L3407–3506
- RolloverManager.get_next_dominant_im ← _get_next_dominant_im L3508–3518

结构差异（ARCHITECTURE.md 阶段 4 决策记录）:
- self.symbol → mds.symbol；换月成功/失败后的合约切换同时更新
  MarketDataService.symbol 与 MarketContextService.symbol
  （ARCHITECTURE.md 阶段 3 决策 3: symbol 双服务同步备忘）
- self.im_quote = api.get_quote(new_symbol) → mds.im_quote 赋值（数据状态归 mds 持有）
- current_position/save_position_state → pm；close_position/execute_order_safe → oe
- emergency_mode/emergency_enter_time → EmergencyState.activate()（真源 L3488–3489）
"""
import logging
import time
from typing import Callable

from quantai.order_executor import OrderExecutor
from quantai.position_manager import PositionManager


class RolloverManager:
    """换月检查与执行（到期日 ≤2 天时平旧仓 → 新主力开仓，SL/TP 按基差偏移平移）。"""

    def __init__(self, *, mds, mcs, api, pm: PositionManager, oe: OrderExecutor,
                 notifier, logger, emergency, now_fn: Callable = None):
        self.mds = mds                  # MarketDataService（symbol/im_quote/index_price/get_basis_info）
        self.mcs = mcs                  # MarketContextService（symbol 双服务同步）
        self.api = api
        self.pm = pm                    # PositionManager
        self.oe = oe                    # OrderExecutor
        self.notifier = notifier
        self.logger = logger            # TradeLogger
        self.emergency = emergency      # EmergencyState

    def _send(self, msg: str) -> None:
        if self.notifier is not None:
            self.notifier.send(msg)

    # ---------- 真源 rollover_if_needed L3407–3506 ----------

    def rollover_if_needed(self):
        basis_info = self.mds.get_basis_info()
        if basis_info['days_to_expiry'] > 2:
            return
        symbol = self.mds.symbol
        pos = self.api.get_position(symbol)
        if pos.volume_long == 0 and pos.volume_short == 0:
            return
        new_symbol = self.get_next_dominant_im()
        if not new_symbol or new_symbol == symbol:
            return
        logging.info(f"开始换月：{symbol} → {new_symbol}")

        old_direction = "LONG" if pos.volume_long > 0 else "SHORT"
        old_volume = pos.volume_long if old_direction == "LONG" else pos.volume_short
        old_stop_loss = self.pm.position['stop_loss']
        old_take_profit = self.pm.position['take_profit']

        # 平旧仓
        success = self.oe.close_position("换月平仓")
        if not success:
            logging.error("换月平仓失败，终止换月操作")
            self._send("⚠️ 换月平仓失败，请手动处理")
            return
        self.api.wait_update(deadline=time.time() + 2)

        # 计算基差偏移
        old_quote = self.api.get_quote(symbol)
        new_quote = self.api.get_quote(new_symbol)
        old_index = self.mds.index_price
        new_index = old_index
        old_basis = old_quote.last_price - old_index if old_quote.last_price else 0
        new_basis = new_quote.last_price - new_index if new_quote.last_price else 0
        basis_shift = new_basis - old_basis

        # 新开仓限价（使用对手价提高成交率）
        if old_direction == "LONG":
            limit_price = new_quote.ask_price1 if new_quote.ask_price1 > 0 else new_quote.last_price
        else:
            limit_price = new_quote.bid_price1 if new_quote.bid_price1 > 0 else new_quote.last_price

        new_stop_loss = old_stop_loss + basis_shift
        new_take_profit = old_take_profit + basis_shift

        # 使用安全下单函数开仓
        avg_price = self.oe.execute_order_safe(
            symbol=new_symbol,
            direction='BUY' if old_direction == 'LONG' else 'SELL',
            offset='OPEN',
            volume=old_volume,
            limit_price=limit_price,
            timeout=30
        )

        if avg_price is not None:
            # 开仓成功
            self.pm.position.update({
                "direction": old_direction,
                "volume": old_volume,
                "entry_price": avg_price,
                "stop_loss": new_stop_loss,
                "take_profit": new_take_profit,
                "last_ai_decision": f"换月从 {symbol} 迁移"
            })
            # 真源 L3470–3471: self.symbol/im_quote 切换 → 双服务同步（阶段 3 决策 3）
            self.mds.symbol = new_symbol
            self.mcs.symbol = new_symbol
            self.mds.im_quote = self.api.get_quote(new_symbol)
            account = self.api.get_account()
            if account:
                balance = account.balance + account.position_profit
            else:
                balance = 0
            self.logger.log("OPEN", new_symbol, old_direction, old_volume, avg_price,
                            balance_after=balance, ai_reason="换月开仓")
            logging.info(f"换月完成: {old_direction} {old_volume}手 @ {avg_price:.2f}")
            self.pm.save_position_state()
            self._send(f"IM换月完成: {old_direction} {old_volume}手，新合约 {new_symbol}")
        else:
            # 开仓失败
            logging.error("换月开仓失败，账户处于空仓状态！")
            self._send(
                f"🚨 紧急：IM换月开仓失败！原持仓 {old_direction} {old_volume}手已平，新合约 {new_symbol} 开仓失败，请立即手动处理！")
            # 进入应急模式，暂停后续自动交易
            if self.emergency is not None:
                self.emergency.activate()   # 真源 L3488–3489
            # P3 修复：开仓失败时也要更新 self.symbol + 清空 current_position
            # 否则后续 AI 决策会继续用旧合约下单 → 全部 FAILED
            self.mds.symbol = new_symbol
            self.mcs.symbol = new_symbol
            self.mds.im_quote = self.api.get_quote(new_symbol)
            self.pm.position.update({
                "direction": None,
                "volume": 0,
                "entry_price": 0.0,
                "stop_loss": 0.0,
                "take_profit": 0.0,
                "last_ai_decision": f"换月开仓失败，原 {old_direction} 仓位已平",
            })
            self.pm.save_position_state()
            logging.warning(f"换月失败后已更新 self.symbol={new_symbol}，current_position 已清空")
            self._send(
                f"⚠️ 已更新交易合约到 {new_symbol}，并清空仓位状态以防后续 FAILED"
            )

    # ---------- 真源 _get_next_dominant_im L3508–3518 ----------

    def get_next_dominant_im(self) -> str:
        code = self.mds.symbol.split('.')[-1]
        year = 2000 + int(code[2:4])
        month = int(code[4:6])
        if month == 12:
            next_year = year + 1
            next_month = 1
        else:
            next_year = year
            next_month = month + 1
        return f"CFFEX.IM{next_year % 100:02d}{next_month:02d}"
