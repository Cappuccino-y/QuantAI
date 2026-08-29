"""QuantAI 入口 — 阶段 5（编排期）版本。

当前状态（design.md §5.2）:
    ✅ quantai 包结构 + config/models/logger/notifier/performance/news_manager（阶段 1）
    ✅ vendor（以 MainToy 版为准，哈希校验一致）
    ✅ market_data / jp_indices（阶段 2）
    ✅ strategies 子包（阶段 3）: indicators + market_context + left_side
       + entry_filters + exemptions + session_plays
    ✅ risk/position/order/conditional_orders/rollover + execution_pipeline（阶段 4）
    ✅ system 装配 + run 主循环 + ai_decision 接线（阶段 5）

用法:
    python main.py --check     # 自检：配置加载 + 组件可构造性验证（不连网、不下单）
    python main.py --dry-run   # dry_run 影子模式（下单/撤单拦截，需 .env 账密连行情）
"""
import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quantai import config


def _check() -> int:
    """自检：验证配置加载与各组件可构造（不连网、不下单）。"""
    from quantai.logger import TradeLogger, setup_logging
    from quantai.models import (AIDecision, ConditionalOrder, FilterResult,
                                LeftSideSignal, LunchContext, Position,
                                SignalRegime, TradeEvent)
    from quantai.news_manager import NewsManager
    from quantai.notifier import DingTalkNotifier
    from quantai.performance import PerformanceMetrics

    print(f"[1/12] config: DATA_DIR = {config.DATA_DIR}")
    print(f"      ACCOUNT {'已配置' if config.ACCOUNT else '未配置(.env)'} / "
          f"PASSWORD {'已配置' if config.PASSWORD else '未配置(.env)'}")
    print(f"      DRY_RUN = {config.DRY_RUN}")

    setup_logging(log_file=os.path.join(config.DATA_DIR, "selfcheck.log"))
    print("[2/12] logger: setup_logging + TradeLogger OK")
    tl = TradeLogger(log_file=os.path.join(config.DATA_DIR, "selfcheck_trade_log.csv"))
    tl.log("SELFCHECK", "TEST", "LONG", 1, 0.0)
    print("      trade_log 写入 OK")

    print("[3/12] models: Position/ConditionalOrder/AIDecision/TradeEvent/"
          "LeftSideSignal/FilterResult/SignalRegime/LunchContext/JPIndexSnapshot OK")
    p = Position.from_dict({"direction": "LONG", "volume": 1, "entry_price": 4000.0})
    assert p.to_dict()["direction"] == "LONG"
    co = ConditionalOrder.from_dict({"action": "BUY", "trigger_price": 4000.0})
    assert co.to_dict()["source"] == ""

    n = DingTalkNotifier(sender=lambda m: None)
    print("[4/12] notifier: DingTalkNotifier(注入假 sender) OK")

    pm = PerformanceMetrics()
    s = pm.summary()
    assert s["trade_count"] >= 0
    print(f"[5/12] performance: PerformanceMetrics OK（历史交易 {s['trade_count']} 笔）")

    nm = NewsManager(fetcher=_NullFetcher(), prev_trading_day_fn=None)
    print("[6/12] news_manager: NewsManager(注入假 fetcher) OK")

    # 阶段 2: 数据层组件可构造性（假 api/fetcher，不连网）
    from quantai.jp_indices import JPIndicesService, create_default_lunch_context
    from quantai.market_data import (AccountView, ContractResolver,
                                     MarketDataService, TradingCalendar)

    class _NullApi:
        def get_quote(self, sym):
            class _Q:
                open_interest = None
                last_price = 0.0
            return _Q()

    class _NullIndexFetcher:
        def get_kline_data(self, index_name, frequency):
            return None

        def get_asian_indices_5min_bars(self):
            return {}

    api = _NullApi()
    resolver = ContractResolver(api)
    cal = TradingCalendar()
    acct = AccountView(api)
    mds = MarketDataService(api, _NullIndexFetcher(), symbol="CFFEX.IM2608")
    jp = JPIndicesService(_NullIndexFetcher())
    ctx = create_default_lunch_context()
    assert cal.is_trading_time() in (True, False)
    assert acct.get_equity() == 0.0
    assert mds.get_yesterday_index_close() is None
    jp_data = jp.fetch_jp_indices()
    assert jp_data is not None and jp_data["nk225_now"] is None  # 空数据 → 全 None 字段（真源行为）
    assert ctx.get("nk225_9am_pct") is None
    print("[7/12] market_data + jp_indices: ContractResolver/TradingCalendar/"
          "AccountView/MarketDataService/JPIndicesService/LunchContext OK")

    # 阶段 3: 策略层组件可构造性 + 纯函数冒烟（假 api，不连网）
    import pandas as pd

    from quantai.strategies.indicators import calc_atr
    from quantai.strategies.market_context import MarketContextService

    mcs = MarketContextService(api, symbol="CFFEX.IM2608")
    assert (mcs.atr_5, mcs.atr_15, mcs.atr_60) == (0.0, 0.0, 0.0)
    assert mcs.stress_level == 1.0
    assert mcs.oi_state_text == "持仓量数据不可用"
    assert calc_atr(None) == 0.0  # 数据不足兜底（真源 L474–475）
    # 动态位阶 n<5 兜底路径（真源 L1538–1539）
    empty_df = pd.DataFrame({"close": [1.0] * 4, "high": [1.0] * 4, "low": [1.0] * 4})
    assert mcs.compute_dynamic_levels(empty_df, 5000.0, "LONG") == ([5030.0], [4970.0])
    print("[8/12] strategies: indicators.calc_atr + MarketContextService(ATR/OI/动态位阶) OK")

    # 阶段 3 第二批: 过滤器/豁免/左侧信号/时段策略可构造性 + 纯决策冒烟（假 fetcher，不连网）
    from datetime import time as dt_time

    from quantai.strategies.entry_filters import EntryFilters
    from quantai.strategies.exemptions import Exemptions
    from quantai.strategies.left_side import LeftSideStrategy
    from quantai.strategies.session_plays import SessionPlaysService

    filters = EntryFilters(_NullIndexFetcher())
    assert filters.check_entry_volume().allowed is True       # 数据不足 → 放行（真源 L4633）
    assert filters.check_trend_alignment("LONG").allowed is True
    exempts = Exemptions(_NullIndexFetcher())
    assert exempts.volume_vcp_check().allowed is False        # 数据不足 → 不豁免（真源 L4860）
    assert exempts.vwap_alignment("LONG").allowed is True     # 数据不足 → 放行（真源 L4896）
    left = LeftSideStrategy(_NullIndexFetcher())
    prompt = left.compute_left_side_signals()
    assert "⚠️ 5min 数据不足或获取失败，跳过信号计算" in prompt  # 真源 L1652 提前返回
    sps = SessionPlaysService(jp_service=jp, mds=mds, mcs=mcs,
                              notifier=DingTalkNotifier(sender=lambda m: None))
    blocked, tail_reason = sps.check_tail_session(dt_time(14, 50))
    assert blocked and "尾盘时段" in tail_reason              # 真源 L3665–3666
    assert sps.lunch_force_close_check({"direction": None}) is False  # 未触发守护
    assert sps.lunch_breakout_today["force_close_deadline"] is None  # 真源 quirk（死路径保真）
    print("[9/12] strategies batch2: EntryFilters/Exemptions/LeftSideStrategy/SessionPlaysService OK")

    # 阶段 4: 业务层组件可构造性 + 手算冒烟（假 api，不连网不下单）
    from datetime import datetime as _dt
    from types import SimpleNamespace as _NS

    from quantai.conditional_orders import ConditionalOrderChecker
    from quantai.execution_pipeline import ExecutionPipeline
    from quantai.order_executor import OrderExecutor
    from quantai.position_manager import PositionManager
    from quantai.risk_manager import (CircuitBreaker, DailyTradeLimiter,
                                      EmergencyState, PositionSizer,
                                      StopOutCooldown)
    from quantai.rollover_manager import RolloverManager

    now = _dt(2026, 8, 28, 10, 0, 0)
    now_fn = lambda: now
    limiter = DailyTradeLimiter(now_fn)
    stopout = StopOutCooldown(now_fn)
    emergency = EmergencyState()
    cb = CircuitBreaker(equity_fn=lambda: 200000.0, now_fn=now_fn,
                        state_file=os.path.join(config.DATA_DIR, "selfcheck_cb.json"))
    assert cb.check() == (False, "无交易历史")   # 未记录前语义（真源 hasattr 模式）
    sizer = PositionSizer(
        account_fn=lambda: _NS(balance=1000000.0, position_profit=0.0),
        last_price_fn=lambda: 5000.0, equity_fn=lambda: 1000000.0,
        daily_loss_fn=lambda: None)
    assert sizer.get_max_lots() == 4   # 手算: margin/lot=150k → min(6, int(600k//150k))=4
    assert sizer.max_lots_by_risk(10.0) == 5   # 1M×1%/(10×200) = 10000/2000 = 5
    sizer2 = PositionSizer(account_fn=lambda: _NS(balance=200000.0, position_profit=0.0),
                           last_price_fn=lambda: 5000.0, equity_fn=lambda: 200000.0,
                           daily_loss_fn=lambda: None)
    assert sizer2.max_lots_by_risk(10.0) == 1  # 200k×1%/(10×200) = 2000/2000 = 1
    assert sizer2.max_lots_by_risk(20.0) == 0  # 2000/4000 = 0.5 → int = 0（超预算拒绝）

    pm = PositionManager(position_file=os.path.join(config.DATA_DIR, "selfcheck_position.pkl"),
                         now_fn=now_fn)
    pm.save_position_state()
    pm.load_position_state()   # plain-dict 守护 + 往返
    assert pm.position["direction"] is None

    api = _NullApi()
    oe = OrderExecutor(api=api, quote_fn=lambda: _NS(last_price=5000.0, ask_price1=5000.2,
                                                     bid_price1=4999.8),
                       atr5_fn=lambda: 0.0, symbol_fn=lambda: "CFFEX.IM2608",
                       logger=tl, notifier=DingTalkNotifier(sender=lambda m: None),
                       position_manager=pm, circuit_breaker=cb, emergency=emergency)
    rollover = RolloverManager(mds=mds, mcs=mcs, api=api, pm=pm, oe=oe,
                               notifier=DingTalkNotifier(sender=lambda m: None),
                               logger=tl, emergency=emergency)
    assert rollover.get_next_dominant_im() == "CFFEX.IM2609"   # 2608 → 2609
    checker = ConditionalOrderChecker(
        pm=pm, mds=mds, mcs=mcs, calendar=cal, filters=filters, exemptions=exempts,
        sizer=sizer, daily_limiter=limiter, circuit_breaker=cb, stopout=stopout,
        oe=oe, emergency=emergency, tail_fn=lambda: (False, ""),
        notifier=DingTalkNotifier(sender=lambda m: None), logger=tl, now_fn=now_fn)
    pipe = ExecutionPipeline(
        pm=pm, mds=mds, mcs=mcs, sizer=sizer, daily_limiter=limiter,
        circuit_breaker=cb, stopout=stopout, filters=filters, exemptions=exempts,
        oe=oe, tail_fn=lambda: (False, ""),
        notifier=DingTalkNotifier(sender=lambda m: None), logger=tl, now_fn=now_fn)
    pipe.execute_decision({"action": "WAIT", "confidence": 0.3})   # WAIT → 无动作
    interval = pipe.execute_ai_cycle("SWING")   # prompt_fn 未注入 → 默认间隔
    assert interval == 900                       # BASE_DECISION_INTERVAL
    print("[10/12] business layer: risk_manager/position_manager/order_executor/"
          "rollover/conditional_orders/execution_pipeline OK")

    # 阶段 5: AI 决策层组件可构造性 + 手算冒烟（不连网、不调 LLM）
    from quantai.ai_decision import (PromptBuilder, SessionWarner,
                                     analyze_market_state,
                                     compute_signal_stats_text,
                                     detect_signal_type, save_ai_decision)

    assert detect_signal_type("L12a突破入场") == "L12a"
    assert detect_signal_type("D17回踩确认") == "D17"
    assert detect_signal_type("条件单触发") == "条件单"
    assert detect_signal_type("加仓1手") == "加仓"
    assert detect_signal_type("换月平仓") == "换月"
    assert detect_signal_type("止盈离场") == "持仓平仓"
    assert detect_signal_type("") == "未标注"
    assert detect_signal_type("随机原因") == "普通开仓"
    # analyze_market_state 手算: 非交易时段→IDLE / 高波动空仓→IDLE /
    # atr5/atr15=30/20=1.5>1.3→SCALPING / 20/20=1.0→SWING
    assert analyze_market_state(is_trading_time=False, stress_level=1.0,
                                position_direction=None,
                                atr_15=20.0, atr_5=30.0) == "IDLE"
    assert analyze_market_state(is_trading_time=True, stress_level=2.5,
                                position_direction=None,
                                atr_15=20.0, atr_5=30.0) == "IDLE"
    assert analyze_market_state(is_trading_time=True, stress_level=1.0,
                                position_direction=None,
                                atr_15=20.0, atr_5=30.0) == "SCALPING"
    assert analyze_market_state(is_trading_time=True, stress_level=1.0,
                                position_direction="LONG",
                                atr_15=20.0, atr_5=20.0) == "SWING"
    # SessionWarner 按天去重（同 key 同天仅 1 次）
    warn_count = {"n": 0}
    _orig_warn = logging.warning
    logging.warning = lambda msg, *a, **k: warn_count.__setitem__(
        "n", warn_count["n"] + 1)
    warner = SessionWarner(now_fn=lambda: _dt(2026, 8, 28, 10, 0, 0))
    warner.warn("k1", "第一次")
    warner.warn("k1", "同天去重")
    warner.warn("k2", "不同 key")
    logging.warning = _orig_warn
    assert warn_count["n"] == 2   # k1 一次 + k2 一次
    # save_ai_decision 往返（JSONL 追加 + 中文不转义）
    decision_file = os.path.join(config.DATA_DIR, "selfcheck_ai_decisions.jsonl")
    save_ai_decision({"action": "WAIT", "reason": "自检"}, log_file=decision_file)
    with open(decision_file, "r", encoding="utf-8") as f:
        rec = json.loads(f.readline())
    assert rec["decision"]["action"] == "WAIT" and "timestamp" in rec
    # 历史信号统计: 文件不存在 → ""（样本不足不注入 prompt）
    assert compute_signal_stats_text(
        trade_log_file=os.path.join(config.DATA_DIR,
                                    "selfcheck_nonexist.csv")) == ""
    # PromptBuilder 构造 + mode 分派 + 手算（基差 -20 点 / -0.40% 贴水）
    _pb_mds = _NS(symbol="CFFEX.IM2608", im_quote=_NS(last_price=4980.0),
                  tech_data_text="自检技术数据",
                  get_basis_info=lambda: {"index_price": 5000.0,
                                          "im_price": 4980.0, "basis": -20.0,
                                          "basis_pct": -0.4,
                                          "days_to_expiry": 12})
    _pb_mcs = _NS(atr_5=20.0, atr_15=50.0, atr_60=60.0, stress_level=1.0,
                  oi_state_text="持仓量数据不可用")
    _pb_pm = _NS(position={"direction": None, "volume": 0, "entry_price": 0.0,
                           "stop_loss": 0.0, "take_profit": 0.0},
                 conditional_order=None)
    pb = PromptBuilder(
        mds=_pb_mds, mcs=_pb_mcs, pm=_pb_pm,
        calendar=_NS(is_trading_time=lambda now=None: False),
        circuit_breaker=_NS(check=lambda: (False, "")),
        daily_limiter=_NS(check=lambda: (False, "")),
        stopout=_NS(last_stopout_dir=None, last_stopout_time=None),
        tail_fn=lambda: (False, ""), left_side_fn=lambda: "（左侧信号）",
        account_fn=lambda: _NS(balance=200000.0, position_profit=0.0),
        sizer=_NS(get_max_lots=lambda: 3), news_items_fn=lambda: [],
        now_fn=lambda: _dt(2026, 8, 28, 10, 15, 0))
    sys_prompt, user_prompt = pb.build_prompt("SWING")
    assert "波段模式特有规则" in sys_prompt and "0.55" in sys_prompt
    assert "基差: -20.00点 (-0.40%)" in user_prompt      # 4980-5000=-20
    assert "状态: 贴水" in user_prompt
    assert "## 当前持仓: 空仓" in user_prompt
    assert "每手保证金约: 149400.00 元" in user_prompt   # 4980*200*0.15
    assert "当前非交易时段" in user_prompt
    print("[11/12] ai_decision: detect_signal_type/analyze_market_state/"
          "SessionWarner/save_ai_decision/PromptBuilder OK")

    # 阶段 5: system 装配 + DryRunApiProxy 拦截语义（假 api，不连网不下单）
    from quantai.system import DryRunApiProxy, IMTradingSystem

    class _SelfCheckApi:
        def __init__(self):
            self.closed = False

        def get_quote(self, sym):
            return _NS(open_interest=10000, last_price=5000.0,
                       ask_price1=5000.2, bid_price1=4999.8,
                       settlement=4990.0, upper_limit=5500.0,
                       lower_limit=4500.0)

        def get_position(self, sym):
            return _NS(volume_long=0, volume_short=0, open_price_long=0.0,
                       open_price_short=0.0)

        def get_account(self):
            return _NS(balance=200000.0, position_profit=0.0)

        def wait_update(self, deadline=None):
            return None

        def cancel_order(self, order):
            pass

        def close(self):
            self.closed = True

    class _SelfCheckFetcher:
        def get_kline_data(self, index_name, frequency):
            return pd.DataFrame({"close": [5001.0]})

        def get_asian_indices_5min_bars(self):
            return {}

        def generate_ai_prompt(self, index_name, periods):
            return "自检技术数据(假)"

    _sc_api = _SelfCheckApi()
    sys_inst = IMTradingSystem(
        dry_run=True,
        api=_sc_api,
        index_fetcher=_SelfCheckFetcher(),
        news_fetcher=_NullFetcher(),
        ai_chat_fn=lambda messages: '{"action": "WAIT", "confidence": 0.3}',
        notifier=DingTalkNotifier(sender=lambda m: None),
        metrics=PerformanceMetrics(),
        position_file=os.path.join(config.DATA_DIR,
                                   "selfcheck_position5.pkl"),
        cb_state_file=os.path.join(config.DATA_DIR, "selfcheck_cb5.json"))
    assert isinstance(sys_inst.api, DryRunApiProxy)   # dry_run → 代理包装
    _order = sys_inst.api.insert_order("CFFEX.IM2608", "BUY", "OPEN", 1, 5000.2)
    assert _order.is_error and _order.status == "REJECTED"   # 下单拦截
    assert sys_inst.api.intercepted_orders == 1
    assert _sc_api.closed is False                   # 未触达真实 api
    assert sys_inst.mds.index_price == 5001.0        # 装配即拉指数价（真源 L405）
    assert sys_inst.mds.tech_data_text == "自检技术数据(假)"  # 装配即刷新技术面（L418）
    assert sys_inst.filters.atr5_fn() == sys_inst.mcs.atr_5  # 备忘 item 1 接线
    assert sys_inst.pipeline.prompt_fn == sys_inst.prompt_builder.build_prompt
    assert sys_inst.pipeline.save_decision_fn.__name__ == "save_ai_decision"
    assert sys_inst.sps.lunch_context is sys_inst.lunch_context  # 共享实例
    sys_inst.stop()                                  # 退出清理
    assert _sc_api.closed is True                    # stop → api.close
    print("[12/12] system: IMTradingSystem 装配 + DryRunApiProxy 拦截 + stop 清理 OK")

    print("\n自检全部通过 ✅")
    return 0


class _NullFetcher:
    """自检用假 fetcher，不产生网络请求。"""

    def fetch_important_news(self, start_str, end_str):
        return []


def main() -> int:
    # Windows GBK 控制台健壮性: emoji/特殊字符不致崩溃（真源在 GBK 控制台下有同样问题）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="QuantAI — IM 股指期货量化交易系统")
    parser.add_argument("--check", action="store_true", help="骨架自检（不连网不下单）")
    parser.add_argument("--dry-run", action="store_true", help="dry_run 影子模式")
    args = parser.parse_args()

    if args.check:
        return _check()

    if args.dry_run:
        # 阶段 5 接线: system 装配 + run 主循环（dry_run → DryRunApiProxy 拦截下单/撤单，
        # design.md §5.2 验收期硬约束；需 .env 账密连行情，只读观察 + 空转下单）
        logging.getLogger(__name__).warning(
            "dry_run 影子模式启动: 下单/撤单入口已拦截（design.md §5.2 验收期硬约束）")
        from quantai.system import IMTradingSystem

        system = IMTradingSystem(dry_run=True)
        try:
            system.run()
        except Exception:
            # 重连看门狗彻底失败等致命路径: run 内部已发 🚨 通知，此处落日志并退出
            logging.getLogger(__name__).critical(
                "系统异常退出，持仓未被接管，请人工介入", exc_info=True)
            return 1
        return 0

    print("system 装配 + run 主循环已就绪（阶段 5）。")
    print("可用: python main.py --check / python main.py --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
