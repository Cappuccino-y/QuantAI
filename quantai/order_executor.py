"""order_executor — 下单执行层（真源 6 个方法，design.md §4.2 order_executor 表逐行映射）。

方法映射:
- OrderExecutor.execute_market_order_with_retry ← _execute_market_order_with_retry L2959–3004
- OrderExecutor.execute_order_safe             ← execute_order_safe L3006–3204
- OrderExecutor.notify_order_filled            ← _notify_order_filled L3206–3234
- OrderExecutor.cancel_all_orders              ← cancel_all_orders L3236–3250
- OrderExecutor.close_position                 ← close_position L3251–3381
- OrderExecutor.emergency_close                ← emergency_close L3383–3404

结构差异（ARCHITECTURE.md 阶段 4 决策记录）:
- self.im_quote → quote_fn() 注入；self.atr_5 → atr5_fn()（接线 MarketContextService）
- current_position/conditional_order/save_position_state/last_entry_time → pm
  （PositionManager）读写；_record_trade_result → cb（CircuitBreaker）；
  metrics.record_trade/update_equity → metrics（PerformanceMetrics）注入
- emergency_mode/emergency_enter_time → EmergencyState 容器
- notify_order_filled 读 pos.get('last_pnl', 0)：真源该键只读从不写入（死键，
  恒 0 → CLOSE 分支盈亏行不追加），保真保留
- dry_run 硬约束（design.md §5.2 验收期）: 阶段 5 装配时以 mock api 注入实现
  "不得发出任何真实下单/撤单"，本模块不内嵌模式分支（保持与真源行为一致）
"""
import logging
import threading
import time
from datetime import datetime
from typing import Callable, Optional


class OrderExecutor:
    """市价重试 / 安全下单 / 撤单 / 平仓 / 应急平仓。"""

    def __init__(self, *, api, quote_fn: Callable, atr5_fn: Callable,
                 symbol_fn: Callable, logger, notifier, position_manager=None,
                 circuit_breaker=None, metrics=None, emergency=None):
        self.api = api
        self.quote_fn = quote_fn          # → mds.im_quote（真源 self.im_quote）
        self.atr5_fn = atr5_fn            # → mcs.atr_5
        self.symbol_fn = symbol_fn        # → mds.symbol（真源 self.symbol）
        self.logger = logger              # TradeLogger
        self.notifier = notifier          # DingTalkNotifier
        self.pm = position_manager        # PositionManager（close_position 读写状态）
        self.cb = circuit_breaker         # CircuitBreaker（close_position 记录盈亏）
        self.metrics = metrics            # PerformanceMetrics（平仓绩效）
        self.emergency = emergency        # EmergencyState
        self._orders = []                 # 真源 L411
        self._orders_lock = threading.Lock()  # 真源 L412
        self._closing = False             # 真源 L402

    @property
    def is_closing(self) -> bool:
        """真源 self._closing（编排层 check_stop_profit 守卫用）。"""
        return self._closing

    def _send(self, msg: str) -> None:
        """钉钉发送（真源 notifycation.send_dingtalk_message → 注入 notifier）。"""
        if self.notifier is not None:
            self.notifier.send(msg)

    # ---------- 真源 _execute_market_order_with_retry L2959–3004 ----------

    def execute_market_order_with_retry(self, symbol: str, direction: str, offset: str,
                                        volume: int, max_retries: int = 3,
                                        base_market_price: Optional[float] = None,
                                        tolerance: float = 2.0) -> Optional[float]:
        """
        使用对手价下单，并支持追价重试。
        base_market_price: 初始触发时的对手价（用于重试偏差比较），若为 None 则不限制重试偏差。
        tolerance: 重试时允许的对手价相对于 base_market_price 的最大绝对偏差。
        """
        for attempt in range(max_retries):
            self.api.wait_update(deadline=time.time() + 2)
            quote = self.quote_fn()
            ask = quote.ask_price1
            bid = quote.bid_price1
            last = quote.last_price

            if direction == 'BUY':
                if ask <= 0:
                    price = last if last > 0 else 0
                else:
                    price = ask  # 首次直接用对手价，不加偏移（因为偏差已检查）
                current_market = ask if ask > 0 else last
            else:
                if bid <= 0:
                    price = last if last > 0 else 0
                else:
                    price = bid
                current_market = bid if bid > 0 else last

            if price <= 0:
                logging.error("无法获取有效对手价，放弃下单")
                return None

            # 如果提供了基准价，检查当前对手价与基准价的偏差（仅在重试时检查？也可以在第一次检查，但第一次已在外部检查过）
            if base_market_price is not None and attempt > 0:
                deviation = abs(current_market - base_market_price)
                if deviation > tolerance:
                    logging.warning(f"重试时对手价偏离过大({deviation:.2f}>{tolerance:.2f})，停止追单")
                    self._send(f"⚠️ 追单中止：对手价偏差 {deviation:.2f} 点")
                    break

            logging.info(f"第{attempt + 1}次尝试下单: {direction} {volume}手 @ {price:.2f}")
            avg_price = self.execute_order_safe(symbol, direction, offset, volume, price, timeout=5)
            if avg_price is not None:
                return avg_price

        return None

    # ---------- 真源 execute_order_safe L3006–3204 ----------

    def execute_order_safe(self, symbol: str, direction: str, offset: str,
                           volume: int, limit_price: float, timeout: int = 30) -> Optional[float]:
        try:
            # 诊断：打印真实下单参数，便于排查"价格超出涨跌停"等笼统错误
            quote = self.api.get_quote(symbol)
            self.api.wait_update(deadline=time.time() + 1)
            logging.info(
                f"[下单] {symbol} {direction} {offset} {volume}手 "
                f"限价={limit_price} "
                f"当前last={quote.last_price} ask1={quote.ask_price1} bid1={quote.bid_price1} "
                f"昨结={quote.settlement} 涨停={quote.upper_limit} 跌停={quote.lower_limit}"
            )

            # ========== P0 修复：限价抢成交策略 ==========
            # 问题：TqKq 模拟账户对手价单不保证成交（限价=ask1 排队等不到）
            # 6/12 案例: 6/6 买单 30s 超时失败（限价=ask1）
            # 6/22 案例: SELL bid1-2tick 在急跌市里挂单等不到，本质是 PASSIVE
            # 修复（双向 cross-spread 策略）：
            #   BUY 时限价 = ask1 + 0.4（2 tick）→ 主动吃 + 小费
            #   SELL 时限价 = bid1 本身 → cross-spread，最激进（任何 ≥ bid1 立即成交）
            #   仅当原 limit_price 比 aggressive 更保守时才修正（防止无效修改）
            #   限价仍在涨跌停范围内 + 不超过 0.5×ATR
            # ==================================================
            if limit_price is not None and limit_price > 0:
                # 计算 0.5×5minATR 作为最大滑点容忍
                atr_5 = self.atr5_fn()
                max_slip = atr_5 * 0.5 if atr_5 > 0 else 1.0
                # IM 期货最小变动价位 = 0.2
                tick = 0.2
                if direction == 'BUY' and quote.ask_price1 > 0:
                    # 当前限价 = ask1（最保守）→ 加 2 tick 抢成交
                    aggressive_price = quote.ask_price1 + tick * 2
                    # 仅当原限价低于 ask1+2tick（更保守）时才修正
                    if aggressive_price > limit_price and aggressive_price <= quote.upper_limit:
                        # 不超过 max_slip 上限
                        if abs(aggressive_price - quote.last_price) <= max_slip:
                            if abs(aggressive_price - limit_price) > tick:  # 真的需要加价才记录
                                logging.info(
                                    f"  [抢成交] BUY 限价 {limit_price:.2f} → {aggressive_price:.2f} "
                                    f"(ask1+2tick={quote.ask_price1+tick*2:.2f}, max_slip={max_slip:.2f})"
                                )
                            limit_price = aggressive_price
                elif direction == 'SELL' and quote.bid_price1 > 0:
                    # ========== P0 修复：SELL 抢成交改为 bid1 本身 ==========
                    # 原代码 bid1-2tick (0.4) 在急跌市里挂单等不到，本质是 PASSIVE
                    # bid1 自身是最激进的 SELL 限价（任何 ≥ bid1 都立即成交）
                    # 6/22 案例：3 次 FAILED SELL 都是因为 limit < bid1
                    aggressive_price = quote.bid_price1
                    if aggressive_price < limit_price and aggressive_price >= quote.lower_limit:
                        if abs(aggressive_price - quote.last_price) <= max_slip:
                            if abs(aggressive_price - limit_price) > tick:
                                logging.info(
                                    f"  [抢成交] SELL 限价 {limit_price:.2f} → {aggressive_price:.2f} "
                                    f"(bid1={quote.bid_price1:.2f}, max_slip={max_slip:.2f})"
                                )
                            limit_price = aggressive_price

            # 防呆：限价单价格异常检测（昨结×0.5 以下 或 ×2 以上 视为异常）
            if limit_price is not None and quote.settlement > 0:
                if limit_price < quote.settlement * 0.5 or limit_price > quote.settlement * 2.0:
                    logging.error(
                        f"[下单拦截] 限价 {limit_price} 严重偏离昨结 {quote.settlement}，"
                        f"可能是行情缓存异常，改用涨跌停价限价"
                    )
                    # ========== P0 修复：涨跌停价限价代替市价单（CFFEX不支持市价单） ==========
                    if direction == 'BUY':
                        fallback_price = quote.upper_limit
                    else:
                        fallback_price = quote.lower_limit
                    order = self.api.insert_order(symbol, direction, offset, volume, fallback_price)
                else:
                    order = self.api.insert_order(symbol, direction, offset, volume, limit_price)
            else:
                order = self.api.insert_order(symbol, direction, offset, volume, limit_price)
            with self._orders_lock:
                self._orders.append(order)
            self.api.wait_update(deadline=time.time() + 2)
            start_time = time.time()
            while True:
                self.api.wait_update(deadline=time.time() + 2)
                if order.is_error or order.status == "REJECTED":
                    logging.error(f"订单失败: {order.last_msg}")
                    self.logger.log("FAILED", symbol, direction, volume, 0,
                                    ai_reason=f"下单失败: {order.last_msg}")
                    with self._orders_lock:
                        if order in self._orders:
                            self._orders.remove(order)
                    return None
                if order.status == "FINISHED":
                    with self._orders_lock:
                        if order in self._orders:
                            self._orders.remove(order)
                    if order.volume_left == 0:
                        trade_price = order.trade_price
                        # 成交成功 → 钉钉通知（带止损止盈/触发价）
                        self.notify_order_filled(symbol, direction, offset, volume, trade_price, limit_price)
                        return trade_price
                    else:
                        logging.warning(f"订单未完全成交: {order.volume_left}手未成交")
                        return None
                if time.time() - start_time > timeout:
                    # ========== P0 修复：超时改激进限价单重试 ==========
                    # 6/22 案例: 原市价单兜底 → CFFEX/SHFE/INE 不支持市价单 → 100% 失败
                    # 修复: 撤单后用更激进限价单重试（CFFEX 限价单必须）
                    #   BUY 重试: ask1 + 6tick (高于卖一 1.2，主动吃 + 大幅小费)
                    #   SELL 重试: bid1 - 6tick (低于买一 1.2，接受更低确保成交)
                    #   最坏情况: 触及涨跌停 → 用 upper_limit / lower_limit
                    # ==================================================
                    logging.warning(f"等待成交超时({timeout}s)，撤单后用更激进限价单重试")
                    self.api.cancel_order(order)
                    self.api.wait_update(deadline=time.time() + 2)
                    with self._orders_lock:
                        if order in self._orders:
                            self._orders.remove(order)

                    # 重新获取盘口（30s 内盘口已变）
                    quote2 = self.api.get_quote(symbol)
                    self.api.wait_update(deadline=time.time() + 1)
                    tick = 0.2

                    if direction == 'BUY':
                        # 重试限价: ask1 + 6tick (1.2 点)，最多 upper_limit
                        retry_price = quote2.ask_price1 + tick * 6 if quote2.ask_price1 > 0 else quote2.upper_limit
                        if quote2.upper_limit > 0 and retry_price > quote2.upper_limit:
                            retry_price = quote2.upper_limit
                    else:  # SELL
                        # 重试限价: bid1 - 6tick (1.2 点)，最少 lower_limit
                        retry_price = quote2.bid_price1 - tick * 6 if quote2.bid_price1 > 0 else quote2.lower_limit
                        if quote2.lower_limit > 0 and retry_price < quote2.lower_limit:
                            retry_price = quote2.lower_limit

                    retry_slip = abs(retry_price - quote2.last_price)
                    logging.warning(
                        f"  [重试] {direction} 新限价 {retry_price:.2f} "
                        f"(ask1={quote2.ask_price1:.2f}/bid1={quote2.bid_price1:.2f}, "
                        f"滑点={retry_slip:.2f}点)"
                    )

                    try:
                        retry_order = self.api.insert_order(symbol, direction, offset, volume, retry_price)
                        with self._orders_lock:
                            self._orders.append(retry_order)
                        retry_start = time.time()
                        retry_timeout = 30  # 重试再等 30s
                        while True:
                            self.api.wait_update(deadline=time.time() + 2)
                            if retry_order.is_error or retry_order.status == "REJECTED":
                                logging.error(f"重试单失败: {retry_order.last_msg}")
                                break
                            if retry_order.status == "FINISHED":
                                if retry_order.volume_left == 0:
                                    trade_price = retry_order.trade_price
                                    slippage = abs(trade_price - quote2.last_price) * volume * 200
                                    logging.warning(
                                        f"⚠️ 重试单成交 @ {trade_price:.2f}, "
                                        f"相对原 last={quote.last_price} 滑点 {abs(trade_price - quote.last_price):.1f}点, "
                                        f"相对重试 last={quote2.last_price} 滑点 {abs(trade_price - quote2.last_price):.1f}点 ≈ {slippage:.0f}元"
                                    )
                                    self._send(
                                        f"⚠️ 限价超时改激进限价单成交: 滑点 {abs(trade_price - quote.last_price):.1f}点 ≈ {slippage:.0f}元"
                                    )
                                    self.notify_order_filled(symbol, direction, offset, volume, trade_price, retry_price)
                                    return trade_price
                                else:
                                    logging.warning(f"重试单未完全成交: {retry_order.volume_left}手")
                                    break
                            if time.time() - retry_start > retry_timeout:
                                logging.error(f"重试单也超时({retry_timeout}s)")
                                self.api.cancel_order(retry_order)
                                self.api.wait_update(deadline=time.time() + 2)
                                break
                        # 走到这里说明重试单失败/超时
                        with self._orders_lock:
                            if retry_order in self._orders:
                                self._orders.remove(retry_order)
                        self.logger.log("FAILED", symbol, direction, volume, 0,
                                        ai_reason=f"原限价{limit_price}超时 + 重试限价{retry_price}失败")
                        self._send(
                            f"❌ IM开仓失败（限价超时+重试失败）: {direction} {offset} {symbol} {volume}手\n"
                            f"原限价 {limit_price} 30s 超时\n"
                            f"重试限价 {retry_price} {retry_timeout}s 也未成交\n"
                            f"盘口 ask1={quote2.ask_price1} bid1={quote2.bid_price1}"
                        )
                        return None
                    except Exception as retry_exc:
                        logging.error(f"重试单异常: {retry_exc}")
                        with self._orders_lock:
                            if 'retry_order' in locals() and retry_order in self._orders:
                                self._orders.remove(retry_order)
                        self.logger.log("FAILED", symbol, direction, volume, 0,
                                        ai_reason=f"重试单异常: {str(retry_exc)[:60]}")
                        self._send(
                            f"❌ IM开仓失败（重试异常）: {direction} {offset} {symbol} {volume}手\n"
                            f"原限价 {limit_price} 超时\n"
                            f"重试异常: {str(retry_exc)[:80]}"
                        )
                        return None
        except Exception as e:
            logging.error(f"下单异常: {e}")
            return None

    # ---------- 真源 _notify_order_filled L3206–3234 ----------

    def notify_order_filled(self, symbol: str, direction: str, offset: str,
                            volume: int, trade_price: float, limit_price: float) -> None:
        """成交钉钉通知：包括止损止盈（从 current_position 读）"""
        try:
            pos = self.pm.position if self.pm is not None else {}
            sl = pos.get('stop_loss', 0)
            tp = pos.get('take_profit', 0)
            msg = (
                f"✅ 成交: {direction} {offset} {symbol} {volume}手 @ {trade_price:.2f}\n"
                f"限价={limit_price if limit_price else '市价'}"
            )
            if offset == 'OPEN' and sl > 0 and tp > 0:
                sl_dist = abs(trade_price - sl)
                tp_dist = abs(tp - trade_price)
                risk_reward = tp_dist / sl_dist if sl_dist > 0 else 0
                msg += (
                    f"\n止损 {sl:.2f} (-{sl_dist:.1f}点)"
                    f"\n止盈 {tp:.2f} (+{tp_dist:.1f}点)"
                    f"\n盈亏比 1:{risk_reward:.2f}"
                )
            elif offset == 'CLOSE':
                pnl = pos.get('last_pnl', 0)   # 真源死键: 只读从不写入，恒 0
                if pnl:
                    emoji = "🟢" if pnl > 0 else "🔴"
                    msg += f"\n{emoji} 盈亏: {pnl:+.0f}元"
            logging.info(msg.replace('\n', ' | '))
            self._send(msg)
        except Exception as e:
            logging.warning(f"成交通知发送失败: {e}")

    # ---------- 真源 cancel_all_orders L3236–3250 ----------

    def cancel_all_orders(self):
        """撤销所有未成交订单"""
        with self._orders_lock:
            alive_orders = [o for o in self._orders if o.status == "ALIVE"]
        if not alive_orders:
            logging.info("没有需要撤销的活跃订单")
            return
        for order in alive_orders:
            logging.info(f"撤销订单 {order.order_id} ...")
            self.api.cancel_order(order)
        self.api.wait_update(deadline=time.time() + 2)
        with self._orders_lock:
            # 清理已撤销的订单
            self._orders = [o for o in self._orders if o.status == "ALIVE"]
        logging.info(f"已撤销 {len(alive_orders)} 个订单")

    # ---------- 真源 close_position L3251–3381 ----------

    def close_position(self, reason: str, is_emergency: bool = False) -> bool:
        # 防止并发平仓
        if self._closing:
            logging.warning("已有平仓操作正在进行，跳过本次平仓请求")
            return False
        self._closing = True
        """
        平仓操作，返回是否成功。
        若 is_emergency 为 True，表示应急模式，会持续重试直到成功。
        """
        try:
            symbol = self.symbol_fn()
            pos = self.api.get_position(symbol)
            if pos.volume_long == 0 and pos.volume_short == 0:
                logging.info("当前无持仓，无需平仓")
                return True

            if pos.volume_long > 0:
                volume = pos.volume_long
                direction_close = 'SELL'
                direction_full = 'LONG'
            elif pos.volume_short > 0:
                volume = pos.volume_short
                direction_close = 'BUY'
                direction_full = 'SHORT'
            else:
                return True

            # ---------- 对手价优化 ----------
            # 获取盘口价格（可能需要先 wait_update 确保盘口数据是最新的）
            self.api.wait_update(deadline=time.time() + 2)
            quote = self.quote_fn()
            ask_price = quote.ask_price1
            bid_price = quote.bid_price1
            last_price = quote.last_price

            if direction_full == 'LONG':
                # 卖出平多，最优对手价为买一价
                market_price = bid_price if bid_price > 0 else last_price
            else:
                # 买入平空，最优对手价为卖一价
                market_price = ask_price if ask_price > 0 else last_price

            # 应急模式强制使用对手价；正常模式也优先使用对手价，但若 AI 要求限价则可保留（这里简化处理）
            if is_emergency or market_price > 0:
                limit_price = market_price
                logging.info(f"使用对手价平仓: {limit_price:.2f}")
            else:
                limit_price = last_price if last_price > 0 else 0

            # 平仓前快照：用于绩效记录
            position = self.pm.position
            entry_price_snapshot = position.get('entry_price', 0.0)
            # 8/27 修复: entry_time 快照优先读持仓内记录（条件单/市价路径都写），
            # 兼容字符串(条件单路径)/datetime(市价路径)双格式；
            # 都缺失时回退 last_entry_time（内存值，进程重启后为 datetime.min）
            _et = position.get('entry_time')
            if isinstance(_et, datetime):
                entry_time_snapshot = _et
            elif isinstance(_et, str) and _et:
                try:
                    entry_time_snapshot = datetime.strptime(_et, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    try:
                        entry_time_snapshot = datetime.strptime(_et, '%Y-%m-%dT%H:%M:%S.%f')
                    except ValueError:
                        entry_time_snapshot = self.pm.last_entry_time
            else:
                entry_time_snapshot = self.pm.last_entry_time
            if entry_time_snapshot == datetime.min or entry_time_snapshot.year <= 1:
                entry_time_snapshot = datetime.now()  # 兜底: 避免 0001-01-01 再次入库

            avg_price = self.execute_order_safe(
                symbol=symbol,
                direction=direction_close,
                offset='CLOSE',
                volume=volume,
                limit_price=limit_price
            )
            # ---------- 后续处理不变 ----------
            if avg_price is not None:
                # 平仓成功
                entry = position['entry_price']
                pnl = (avg_price - entry) * volume * 200 if direction_full == "LONG" else (entry - avg_price) * volume * 200
                account = self.api.get_account()
                if account:
                    balance = account.balance + account.position_profit
                else:
                    balance = 0
                self.logger.log("CLOSE", symbol, direction_full, volume, avg_price,
                                pnl=pnl, balance_after=balance, ai_reason=reason)
                logging.info(f"平仓成功: {reason}, 盈亏: {pnl:.2f}")
                # ========== P1 修复：熔断统计记录 ==========
                try:
                    if self.cb is not None:
                        self.cb.record_trade_result(pnl)
                except Exception as cb_err:
                    logging.warning(f"熔断统计记录失败: {cb_err}")
                # ============================================
                # ========== P1 修复：先清 current_position 再 save ==========
                # 6/17 + 6/22 bug: save_position_state() 写在 current_position.update 之前
                # 导致 pickle 里写的是"已平仓前"的状态，第二天启动时误判有持仓
                position.update({
                    "direction": None,
                    "volume": 0,
                    "entry_price": 0.0,
                    "stop_loss": 0.0,
                    "take_profit": 0.0,
                    "last_ai_decision": None,
                    "entry_time": None,
                })
                # 清条件单
                self.pm.conditional_order = None
                self.pm.save_position_state()
                self._send(f"IM平仓成功: {reason}, 盈亏: {pnl:.2f}")
                # P1：把交易结果计入绩效
                try:
                    if self.metrics is not None:
                        self.metrics.record_trade(
                            pnl=pnl, direction=direction_full, volume=volume,
                            entry_price=entry_price_snapshot, exit_price=avg_price,
                            entry_time=entry_time_snapshot, exit_time=datetime.now()
                        )
                        self.metrics.update_equity(balance)
                except Exception as me:
                    logging.error(f"记录绩效失败: {me}")
                return True
            else:
                # 平仓失败
                logging.error(f"平仓失败！当前持仓: {direction_full} {volume}手")
                self._send(
                    f"⚠️ 紧急：IM平仓失败！{reason}，持仓 {direction_full} {volume}手，请立即处理！")
                return False
        finally:
            self._closing = False

    # ---------- 真源 emergency_close L3383–3404 ----------

    def emergency_close(self, reason: str):
        """
        应急平仓入口：持续尝试直到成功，期间暂停主策略
        """
        logging.critical("启动应急平仓模式...")
        self._send(f"🚨 应急平仓启动：{reason}")

        # 设置应急标志，暂停 AI 决策
        if self.emergency is not None:
            self.emergency.activate()   # 真源 L3391–3392

        while True:
            success = self.close_position(reason + " (应急)", is_emergency=True)
            if success:
                break
            # 若 close_position 本身不递归，则在此处重试
            time.sleep(3)

        if self.emergency is not None:
            self.emergency.deactivate()   # 真源 L3401

        logging.info("应急平仓完成，恢复正常运行")
        self._send("应急平仓完成，系统恢复")
