"""IMTradingSystem 编排器（阶段 5）：依赖装配 + run 主循环 + 重连看门狗 + 优雅退出.

真源映射（design.md §4.2 system.py 4 方法 + __main__ 块）:
- __init__      L388–456   → 装配（全局状态 → 各服务持有，见 ARCHITECTURE.md 阶段 2–4 决策记录）
- _reconnect_api L5436–5473 → 重连看门狗（C1 修复：断线不退出）
- run           L5475–5646 → 主循环（三节点 + 应急自动重置 + 双频自适应 + 时段节点 + 换月）
- stop          L5648–5654 → 退出清理
- __main__      L5657–5659 → main.py 入口

编排层执行职责（ARCHITECTURE.md 阶段 5 备忘 item 5，session_plays 纯决策的消费方）:
- a. SessionAction(CLOSE_POSITION) 执行 + 清仓持久化 + ✅/❌ 通知（真源 L3940–3974 语义）
- b. 14:00 强平 close_position（真源 L4311；当前死路径保真）
- c. post_open_analysis 的 ADJUST 建议写入 pm.position + 持久化（真源 L4393–4406）
- d. 12:50 条件单 dict 写入 pm.conditional_order + 持久化（真源 L4238–4250 等价）

dry_run 硬约束（决策 12 / design.md §5.2 验收期）:
DryRunApiProxy 拦截 insert_order/cancel_order（空转，不触达柜台），其余调用委托
真实 api（只读）——保证 dry_run 期间不发出任何真实下单/撤单。
注意：dry_run 请搭配独立 sim 账户/空账户使用（get_position/get_account 仍为只读真实数据）。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, time as dt_time, timedelta
from typing import Optional

from tqsdk import TqApi, TqAuth, TqKq

from .ai_decision import (PromptBuilder, SessionWarner, analyze_market_state,
                          save_ai_decision)
from .conditional_orders import ConditionalOrderChecker
from .config import (ACCOUNT, BASE_DECISION_INTERVAL, CIRCUIT_BREAKER_FILE,
                     EMERGENCY_AUTO_RESET_SEC, PASSWORD, POSITION_FILE,
                     SHORT_TERM_INTERVAL)
from .execution_pipeline import ExecutionPipeline
from .jp_indices import (JPIndicesService, create_default_lunch_context,
                         refresh_lunch_context)
from .logger import TradeLogger, setup_logging
from .market_data import (AccountView, ContractResolver, MarketDataService,
                          TradingCalendar)
from .news_manager import NewsManager
from .notifier import DingTalkNotifier
from .order_executor import OrderExecutor
from .performance import PerformanceMetrics
from .position_manager import PositionManager
from .risk_manager import (CircuitBreaker, DailyTradeLimiter, EmergencyState,
                           PositionSizer, StopOutCooldown)
from .rollover_manager import RolloverManager
from .strategies.entry_filters import EntryFilters
from .strategies.exemptions import Exemptions
from .strategies.left_side import LeftSideStrategy
from .strategies.market_context import MarketContextService
from .strategies.session_plays import SessionAction, SessionPlaysService
from .vendor.trade_data_fetcher import IndexDataFetcher

logger = logging.getLogger(__name__)


# ========== dry_run mock api（决策 12 / design.md §5.2 验收期硬约束） ==========

class _DryRunOrder:
    """DryRunApiProxy.insert_order 返回的假 order 对象.

    字段集 = OrderExecutor 读取面（is_error/status/last_msg/volume_left/trade_price）；
    立即 REJECTED → execute_order_safe 快速走失败路径返回 None（空转，不挂起主循环），
    决策编排照常运行但不产生任何真实委托与持仓状态写入。
    """

    def __init__(self, volume: int):
        self.is_error = True
        self.status = "REJECTED"
        self.last_msg = "DRY_RUN: 下单已拦截（未发送到柜台）"
        self.volume_left = volume
        self.trade_price = 0.0


class DryRunApiProxy:
    """dry_run 模式 api 代理：下单/撤单入口统一空转.

    - insert_order → 返回立即 REJECTED 的假 order（不触达柜台）
    - cancel_order → 空转
    - 其余属性（get_quote/get_kline_serial/get_account/get_position/wait_update/close 等）
      委托真实 api（只读观察）
    """

    def __init__(self, real_api):
        self._real_api = real_api
        self.intercepted_orders = 0   # 拦截计数（观测用）

    def insert_order(self, symbol, direction, offset, volume, limit_price):
        self.intercepted_orders += 1
        logging.warning(
            f"[DRY_RUN] 拦截下单: {direction} {offset} {symbol} {volume}手 "
            f"@ {limit_price}（未发送到柜台）"
        )
        return _DryRunOrder(volume)

    def cancel_order(self, order):
        logging.warning("[DRY_RUN] 拦截撤单（未发送到柜台）")

    def __getattr__(self, name):
        # 仅在常规属性查找失败时触发；_real_api 已在实例字典中，不会递归
        if name.startswith('_'):
            raise AttributeError(name)
        return getattr(self._real_api, name)


# ========== 编排器（真源 IMTradingSystem L387–5656 的编排层残体） ==========

class IMTradingSystem:
    """中证 1000 IM 股指期货 T+0 LLM 量化交易系统.

    仅做依赖装配 + 主循环调度；业务逻辑在各 quantai.* 模块（阶段 1–4）。
    可注入 seam（api/index_fetcher/news_fetcher/ai_chat_fn/notifier/metrics/
    position_file/cb_state_file）仅用于测试与 dry_run，生产路径全部缺省构造。
    """

    def __init__(self, *, dry_run: bool = False,
                 api=None, index_fetcher=None, news_fetcher=None,
                 ai_chat_fn: Optional[object] = None,
                 notifier: Optional[DingTalkNotifier] = None,
                 metrics: Optional[PerformanceMetrics] = None,
                 position_file: Optional[str] = None,
                 cb_state_file: Optional[str] = None):
        self.dry_run = dry_run
        setup_logging()

        # 真源 L390: TqApi(TqKq(), auth=TqAuth(ACCOUNT, PASSWORD))
        if api is None:
            api = TqApi(TqKq(), auth=TqAuth(ACCOUNT, PASSWORD))
        if dry_run:
            # 决策 12: dry_run 下单/撤单入口统一空转（design.md §5.2 验收期硬约束）
            api = DryRunApiProxy(api)
        self.api = api

        # 真源 L395–396: IndexDataFetcher + 指数名
        self.index_fetcher = index_fetcher or IndexDataFetcher()

        # 真源 L391–392: 识别主力合约 + 订阅行情（symbol/im_quote 归 MarketDataService）
        symbol = ContractResolver(api).get_dominant_im()
        self.mds = MarketDataService(api, self.index_fetcher, symbol=symbol)
        self.calendar = TradingCalendar()
        self.acct = AccountView(api)
        # 真源 L399/L421–424: ATR/OI 状态字段归 MarketContextService（阶段 3 决策 1）
        self.mcs = MarketContextService(api, symbol)

        # 真源 L405: 启动即拉一次指数价（阶段 5 备忘: 避免启动初期 index_price=0 基差异常）
        self.mds.update_index_price()

        # 真源 L400–401/L408–410: 新闻缓存/锁 + 后台抓取线程 → NewsManager（阶段 1）
        self.news_manager = NewsManager(
            fetcher=news_fetcher,
            prev_trading_day_fn=self.calendar.get_previous_trading_day_15)
        self.news_manager.start()

        # 真源 L403/L435: TradeLogger + PerformanceMetrics
        self.trade_logger = TradeLogger()
        self.metrics = metrics or PerformanceMetrics()

        # 真源 L389/L431–433: 风控状态（emergency/止损冷却/日次数）
        self.emergency = EmergencyState()
        self.stopout = StopOutCooldown()
        self.daily_limiter = DailyTradeLimiter()

        # 真源 L147–157 全局状态 + L413 load → PositionManager（阶段 4）
        self.notifier = notifier or DingTalkNotifier()
        self.pm = PositionManager(
            position_file=position_file or POSITION_FILE,
            notifier=self.notifier,
            daily_limiter=self.daily_limiter)
        self.pm.load_position_state()

        # 真源 L415 【修复 M8】启动时恢复熔断状态（连亏/日亏），重启不绕过风控
        self.cb = CircuitBreaker(
            equity_fn=self.acct.get_equity,
            state_file=cb_state_file or CIRCUIT_BREAKER_FILE)
        self.cb.load_state()

        # 真源 L417: 校验本地状态与云端持仓是否一致
        self.pm.validate_position_state(api, self.mds.symbol)

        # 真源 L418: 启动刷新技术面数据（阶段 5 备忘）
        self.mds.refresh_tech_data()

        # 真源 L786–874/L805: 仓位计算（account/last_price/equity 经注入读取）
        self.sizer = PositionSizer(
            account_fn=lambda: self.api.get_account(),
            last_price_fn=lambda: self.mds.im_quote.last_price,
            equity_fn=self.acct.get_equity,
            daily_loss_fn=self.cb.daily_loss)

        # 真源 L2959–3410: 下单执行（阶段 4）
        self.oe = OrderExecutor(
            api=api,
            quote_fn=lambda: self.mds.im_quote,
            atr5_fn=lambda: self.mcs.atr_5,
            symbol_fn=lambda: self.mds.symbol,
            logger=self.trade_logger,
            notifier=self.notifier,
            position_manager=self.pm,
            circuit_breaker=self.cb,
            metrics=self.metrics,
            emergency=self.emergency)

        # 真源 L4422–4943: 过滤器链 + 豁免链（阶段 3）
        # 备忘 item 1: atr5_fn 必须接线 mcs.atr_5（未注入视为 0 = ATR 未就绪静默放行）
        self.filters = EntryFilters(self.index_fetcher,
                                    atr5_fn=lambda: self.mcs.atr_5)
        self.exemptions = Exemptions(self.index_fetcher)

        # 真源 L1276–1285 + L1608–2049: 会话告警去重 + 左侧信号（备忘 item 2）
        self.warner = SessionWarner()
        self.left_side = LeftSideStrategy(
            self.index_fetcher,
            index_price_fn=lambda: self.mds.index_price,
            yesterday_close_fn=self.mds.get_yesterday_index_close,
            dynamic_levels_fn=self.mcs.compute_dynamic_levels,
            notifier=self.notifier,
            warn_fn=self.warner.warn)

        # 真源 L3684–3808/L438–456: 日韩联动 + lunch_context（共享实例，备忘 item 3）
        self.jp = JPIndicesService(self.index_fetcher)
        self.lunch_context = create_default_lunch_context()

        # 真源 L102: AI_CLIENT → vendor llm_client（可注入 fake 供测试/dry_run）
        if ai_chat_fn is None:
            from .vendor.llm_client import OpenAICompatibleClient
            ai_chat_fn = OpenAICompatibleClient().chat
        self.ai_chat_fn = ai_chat_fn

        # 真源 L3520–4421: 时段策略（纯决策，备忘 item 3 接线）
        self.sps = SessionPlaysService(
            jp_service=self.jp,
            mds=self.mds,
            mcs=self.mcs,
            notifier=self.notifier,
            ai_chat_fn=self.ai_chat_fn,
            logger=self.trade_logger,
            news_items_fn=self.news_manager.get_news,
            lunch_context=self.lunch_context)

        # 真源 L4944–5324: 条件单检查（阶段 4）
        self.conditional = ConditionalOrderChecker(
            pm=self.pm, mds=self.mds, mcs=self.mcs, calendar=self.calendar,
            filters=self.filters, exemptions=self.exemptions, sizer=self.sizer,
            daily_limiter=self.daily_limiter, circuit_breaker=self.cb,
            stopout=self.stopout, oe=self.oe, emergency=self.emergency,
            tail_fn=self.sps.check_tail_session,
            notifier=self.notifier, logger=self.trade_logger)

        # 真源 L3407–3518: 换月（阶段 4）
        self.rollover = RolloverManager(
            mds=self.mds, mcs=self.mcs, api=api, pm=self.pm, oe=self.oe,
            notifier=self.notifier, logger=self.trade_logger,
            emergency=self.emergency)

        # 真源 L980–2105: PromptBuilder（本阶段落位）
        self.prompt_builder = PromptBuilder(
            mds=self.mds, mcs=self.mcs, pm=self.pm, calendar=self.calendar,
            circuit_breaker=self.cb, daily_limiter=self.daily_limiter,
            stopout=self.stopout, tail_fn=self.sps.check_tail_session,
            left_side_fn=self.left_side.compute_left_side_signals,
            account_fn=lambda: self.api.get_account(),
            sizer=self.sizer,
            news_items_fn=self.news_manager.get_news)

        # 真源 L2108–2925/L5376–5433: 八步编排 + AI 决策循环（阶段 4 + 本阶段接线）
        self.pipeline = ExecutionPipeline(
            pm=self.pm, mds=self.mds, mcs=self.mcs, sizer=self.sizer,
            daily_limiter=self.daily_limiter, circuit_breaker=self.cb,
            stopout=self.stopout, filters=self.filters,
            exemptions=self.exemptions, oe=self.oe,
            tail_fn=self.sps.check_tail_session,
            notifier=self.notifier, logger=self.trade_logger,
            prompt_fn=self.prompt_builder.build_prompt,
            ai_chat_fn=self.ai_chat_fn,
            save_decision_fn=save_ai_decision)

        # 真源 L5576 hasattr 懒初始化 / L5585 → 构造初始化（行为等价）
        self._last_equity_update = None
        self._overnight_done_date = None

    # ---------- 编排层执行职责（备忘 item 5） ----------

    def _execute_close_action(self, act: SessionAction) -> None:
        """SessionAction(CLOSE_POSITION) 执行半边（备忘 item 5a，真源 L3940–3974 语义）.

        注意：pnl 计算保真真源 L3952 的 ``* 200``（硬编码乘数、未乘 volume 的 quirk）。
        """
        if act.action != "CLOSE_POSITION":
            logging.warning(f"未知 SessionAction: {act.action}，忽略")
            return
        pos_direction = self.pm.position['direction']
        pos_entry = self.pm.position['entry_price']
        try:
            close_price = self.oe.execute_order_safe(
                symbol=self.mds.symbol,
                direction=act.close_direction,
                offset='CLOSE',
                volume=act.volume,
                limit_price=None,  # 市价单
                timeout=15
            )
            if close_price is not None:
                pnl = (pos_entry - close_price) * 200 if pos_direction == 'SHORT' else (close_price - pos_entry) * 200
                logging.info(f"✅ 盘前主动平仓成功 @ {close_price:.2f}, 盈亏 {pnl:+.0f}元")
                # 清空 current_position（止损不再触发）
                self.pm.position.update({
                    "direction": None,
                    "volume": 0,
                    "entry_price": 0.0,
                    "stop_loss": 0.0,
                    "take_profit": 0.0,
                    "last_ai_decision": f"盘前跳空主动平仓 @ {close_price:.2f}",
                })
                self.pm.save_position_state()
                self.notifier.send(
                    f"✅ 盘前主动平仓完成 @ {close_price:.2f}, 盈亏 {pnl:+.0f}元"
                )
            else:
                logging.error("盘前主动平仓失败！市价单未成交")
                self.notifier.send(
                    f"❌ 盘前主动平仓失败！请手动处理 {pos_direction} @ {pos_entry:.2f}"
                )
        except Exception as e:
            logging.error(f"盘前主动平仓异常: {e}", exc_info=True)
            self.notifier.send(f"❌ 盘前主动平仓异常: {e}")

    def _morning_pre_open_node(self) -> None:
        """盘前节点（真源 L5489/L5491/L5502/L5510 调用点）: 纯决策 + 编排执行。"""
        actions = self.sps.morning_pre_open_analysis(self.pm.position)
        for act in actions:
            self._execute_close_action(act)

    def _post_open_node(self) -> None:
        """9:30+ 节点执行半边（备忘 item 5c，真源 L4393–4406）.

        ADJUST_* CSV 落盘与通知已在 sps.post_open_analysis 内完成；
        此处只做 pm.position 写入 + 持久化。
        """
        adjust = self.sps.post_open_analysis(self.pm.position)
        if adjust is None:
            return
        changed = False
        if adjust.get("adjust_stop_loss") is not None:
            self.pm.position['stop_loss'] = adjust["adjust_stop_loss"]
            changed = True
        if adjust.get("adjust_take_profit") is not None:
            self.pm.position['take_profit'] = adjust["adjust_take_profit"]
            changed = True
        if changed:
            self.pm.save_position_state()

    def _overnight_node(self) -> None:
        """14:55 隔夜评估节点执行半边（真源 L3641–3642 语义）.

        CLOSE 建议的 reason 已由 sps 格式化为
        ``收盘前平仓（AI建议不过夜，理由：...）``。
        """
        result = self.sps.evaluate_overnight_holding(self.pm.position)
        if result and result.get('action') == 'CLOSE':
            self.oe.close_position(result['reason'])

    def _lunch_breakout_node(self) -> None:
        """12:50 节点执行半边（备忘 item 5d，真源 L4238–4250 写全局的等价物）."""
        order = self.sps.lunch_breakout_check(self.pm.position)
        if order is not None:
            self.pm.conditional_order = order
            self.pm.save_position_state()

    def _lunch_force_close_node(self) -> None:
        """14:00 节点执行半边（备忘 item 5b，真源 L4311；当前死路径保真）."""
        if self.sps.lunch_force_close_check(self.pm.position):
            self.oe.close_position(reason="12:50顺势单14:00强平")

    def _check_stop_profit(self) -> None:
        """SL/TP 监控编排（真源 check_stop_profit L2926–2957 的执行半边）.

        pm.check_stop_profit 纯决策返回 trigger_reason → 编排层
        close_position + 失败转 emergency_close（阶段 4 决策 5）。
        """
        trigger = self.pm.check_stop_profit(
            self.pm.position, self.mds.im_quote.last_price,
            closing=self.oe.is_closing, on_stopout=self.stopout.record)
        if trigger:
            if not self.oe.close_position(trigger):
                self.oe.emergency_close(trigger)

    # ---------- 重连看门狗（真源 _reconnect_api L5436–5473） ----------

    def _bind_api(self, new_api) -> None:
        """重连后重绑各服务的 api 引用（真源单一 self.api 赋值的等价物）."""
        self.api = new_api
        self.mds.api = new_api
        self.mcs.api = new_api
        self.oe.api = new_api
        self.acct.api = new_api
        self.rollover.api = new_api

    def _reconnect_api(self, max_attempts: int = 5) -> bool:
        """
        C1 修复: 天勤连接断开后的重连看门狗。
        - 重建 TqApi(TqKq) 连接并重新订阅主力合约行情
        - 失败指数退避重试（10s/20s/30s/40s），期间发钉钉告警
        - 返回 True 表示重连成功；False 表示彻底失败（由调用方决定退出，避免无人接管持仓）
        """
        for attempt in range(1, max_attempts + 1):
            try:
                logging.warning(f"尝试重新连接天勤 (第 {attempt}/{max_attempts} 次)...")
                self.notifier.send(
                    f"⚠️ 天勤连接断开，正在重连 (第 {attempt}/{max_attempts} 次)..."
                )
                old_api = getattr(self, 'api', None)   # 真源死变量保真（L5449 赋值未使用）
                old_symbol = getattr(self.mds, 'symbol', None)
                new_api = TqApi(TqKq(), auth=TqAuth(ACCOUNT, PASSWORD))
                self._bind_api(new_api)
                try:
                    # 等行情推送就绪后再识别主力（可能已换月），失败则退回旧合约
                    self.api.wait_update(deadline=time.time() + 3)
                    new_symbol = ContractResolver(self.api).get_dominant_im()
                except Exception:
                    new_symbol = old_symbol
                self.mds.symbol = new_symbol
                self.mcs.symbol = new_symbol  # symbol 双服务同步（阶段 3 决策 3）
                self.mds.im_quote = self.api.get_quote(self.mds.symbol)
                # 重连后重新校验本地持仓与云端一致性
                try:
                    self.pm.validate_position_state(self.api, self.mds.symbol)
                except Exception as e:
                    logging.warning(f"重连后持仓校验失败（不影响继续运行）: {e}")
                logging.warning(f"✅ 天勤重连成功: {self.mds.symbol}")
                self.notifier.send(f"✅ 天勤重连成功: {self.mds.symbol}")
                return True
            except Exception as e:
                logging.error(f"天勤重连失败 (第 {attempt} 次): {e}")
                if attempt < max_attempts:
                    time.sleep(10 * attempt)  # 指数退避 10s/20s/30s/40s
        self.notifier.send("🚨 天勤重连失败，系统即将退出，请人工接管持仓")
        return False

    # ---------- 主循环（真源 run L5475–5646） ----------

    def run(self) -> None:
        """主循环：双频自适应决策系统"""
        logging.info("IM AI交易系统启动（双频自适应）...")
        self.oe.cancel_all_orders()

        # ===== 开盘前 / 早盘 / 午盘三个时间节点 =====
        now = datetime.now()
        if self.calendar.is_trading_day(now):
            # 节点 1: 9:00 早盘前分析（拉日经 + Topix + 隔夜新闻）
            target_9 = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now < target_9:
                wait_sec = (target_9 - now).total_seconds()
                logging.info(f"等待 {wait_sec:.0f} 秒至 9:00 执行早盘前分析...")
                time.sleep(wait_sec)
                self._morning_pre_open_node()
            elif dt_time(9, 0) <= now.time() < dt_time(9, 25):
                self._morning_pre_open_node()

            # 节点 2: 9:25:30 集合竞价撮合稳定后 / 期货开盘前
            #    9:25:00 撮合完成但 TqSdk 推送有 10-30s 延迟
            #    9:25:30 后数据稳定，避免拿到旧的 9:00 5min 收盘价
            now = datetime.now()
            target_925 = now.replace(hour=9, minute=25, second=30, microsecond=0)
            if now < target_925:
                wait_sec = (target_925 - now).total_seconds()
                logging.info(f"等待 {wait_sec:.0f} 秒至 9:25:30 拉集合竞价指数...")
                time.sleep(wait_sec)
                self._morning_pre_open_node()  # 二次：拉集合竞价指数
            elif dt_time(9, 25) <= now.time() < dt_time(9, 30):
                # 9:25:00-9:25:30 期间: 数据可能未稳定, 强制等到 9:25:30
                # 9:25:30-9:30 之间: 刷新集合竞价指数
                if now.time() < dt_time(9, 25, 30):
                    wait_sec = 5
                    logging.info(f"9:25 数据未稳定, 等待 {wait_sec} 秒至 9:25:30...")
                    time.sleep(wait_sec)
                self._morning_pre_open_node()

            # 节点 3: 9:30+ 期货开盘后常规分析（仅调整持仓）
            now = datetime.now()
            if now.time() >= dt_time(9, 30):
                self.mds.update_index_price()
                self._post_open_node()

        # 双频时间追踪
        last_swing_time = datetime.now() - timedelta(minutes=15)
        last_scalping_time = datetime.now() - timedelta(minutes=5)
        ai_swing_interval = BASE_DECISION_INTERVAL      # AI建议的波段间隔
        ai_scalping_interval = SHORT_TERM_INTERVAL       # AI建议的短线间隔

        try:
            while True:
                # 行情更新
                # C1 修复: 断线不再直接退出 —— wait_update 抛异常时进入重连看门狗
                try:
                    if self.calendar.is_trading_time():
                        self.api.wait_update()
                    else:
                        self.api.wait_update(deadline=time.time() + 1)
                except Exception as e:
                    logging.error(f"行情连接异常，触发重连看门狗: {e}")
                    ok = self._reconnect_api()
                    if not ok:
                        logging.critical("天勤重连彻底失败，系统退出（持仓未被接管，请人工介入）")
                        raise
                    continue  # 重连成功，跳过本次循环，下一轮继续 wait_update
                now = datetime.now()

                # 应急模式
                if self.emergency.mode:
                    # P2：emergency_mode 自动重置（30 分钟后）
                    if self.emergency.enter_time and \
                            (now - self.emergency.enter_time).total_seconds() > EMERGENCY_AUTO_RESET_SEC:
                        pos = self.api.get_position(self.mds.symbol)
                        if pos.volume_long == 0 and pos.volume_short == 0:
                            logging.warning(
                                f"⚠️ emergency_mode 自动重置：已空仓 {EMERGENCY_AUTO_RESET_SEC/60:.0f} 分钟"
                            )
                            self.notifier.send(
                                f"⚠️ emergency_mode 自动重置（已空仓超时）"
                            )
                            self.emergency.mode = False
                            self.emergency.enter_time = None
                        else:
                            logging.warning(
                                f"⚠️ emergency_mode 仍持仓中 ({pos.volume_long}/{pos.volume_short})，"
                                f"继续等待"
                            )
                    self._check_stop_profit()
                    time.sleep(1)
                    continue

                # 每tick检查
                self.conditional.check_conditional_order()
                self._check_stop_profit()

                # P1：每个 tick 更新权益曲线（追踪最大回撤）
                try:
                    account = self.api.get_account()
                    if account:
                        balance = account.balance + account.position_profit
                        # 限频：每 30 秒更新一次避免太频繁
                        if self._last_equity_update is None or \
                                (now - self._last_equity_update).total_seconds() > 30:
                            self.metrics.update_equity(balance, now)
                            self._last_equity_update = now
                except Exception:
                    pass

                # 收盘过夜评估（14:55）
                if self.calendar.is_trading_day(now) and now.hour == 14 and now.minute >= 55:
                    if self._overnight_done_date is None or self._overnight_done_date != now.date():
                        self._overnight_node()
                        # P1：收盘后打印日报（每天仅 1 次）
                        self.metrics.print_daily_report()
                        self._overnight_done_date = now.date()
                    last_swing_time = now
                    continue

                # 临近休市跳过决策
                if self.calendar.is_near_close():
                    continue

                # === 11:30 拉日经午休节点（写入 nk225_1130_pct） ===
                if dt_time(11, 30) <= now.time() <= dt_time(11, 31):
                    jp = self.jp.fetch_jp_indices()
                    if jp and self.lunch_context.get('nk225_1130_pct') is None:
                        refresh_lunch_context(self.lunch_context, 'nk225_1130_pct', jp['nk225_pct'])
                        logging.info(f"11:30 日经: {jp['nk225_pct']:+.2f}%")

                # === 12:30 顺势单预览节点（KOSPI 午盘开始 1h，发出预警和预估 SL/TP） ===
                if dt_time(12, 30) <= now.time() <= dt_time(12, 31):
                    self.sps.lunch_breakout_preview()

                # === 12:50 顺势单节点 ===
                if dt_time(12, 50) <= now.time() <= dt_time(12, 51):
                    self._lunch_breakout_node()

                # === 14:00 12:50顺势单强制平仓节点 ===
                if dt_time(14, 0) <= now.time() <= dt_time(14, 1):
                    self._lunch_force_close_node()

                if not self.calendar.is_trading_time(now):
                    continue

                # === 双频自适应决策 ===
                market_state = analyze_market_state(
                    is_trading_time=self.calendar.is_trading_time(now),
                    stress_level=self.mcs.stress_level,
                    position_direction=self.pm.position['direction'],
                    atr_15=self.mcs.atr_15,
                    atr_5=self.mcs.atr_5)
                if market_state == "IDLE":
                    continue

                swing_elapsed = (now - last_swing_time).total_seconds()
                scalping_elapsed = (now - last_scalping_time).total_seconds()

                # 波段决策（15min级别，始终可触发）
                if swing_elapsed >= ai_swing_interval:
                    last_swing_time = now
                    ai_swing_interval = self.pipeline.execute_ai_cycle("SWING")
                    # 一次只做一个决策，让行情有时间反应
                    continue

                # 短线决策（5min级别，仅在 SCALPING 市场状态下触发）
                if scalping_elapsed >= ai_scalping_interval and market_state == "SCALPING":
                    last_scalping_time = now
                    ai_scalping_interval = self.pipeline.execute_ai_cycle("SCALPING")
                    # 同时也更新波段计时器，避免紧跟着又触发波段
                    last_swing_time = now

                self.rollover.rollover_if_needed()

        except KeyboardInterrupt:
            logging.info("收到退出信号")
        finally:
            self.stop()

    # ---------- 退出清理（真源 stop L5648–5654） ----------

    def stop(self) -> None:
        self.oe.cancel_all_orders()
        self.news_manager.stop()   # 真源 news_thread_running = False
        if hasattr(self, 'api'):
            self.api.close()
        self.pm.save_position_state()
        logging.info("系统已安全退出")


__all__ = ["IMTradingSystem", "DryRunApiProxy"]
