"""ai_decision 单测（阶段 5）— 行为对拍真源 L980–1356 / L2051–2105 / L5325–5374。

覆盖:
- detect_signal_type: 关键词表 + 循环序优先级（L12a 先于 L3）
- compute_signal_stats_text: 手算胜率/均值/排序 + OPEN/坏行跳过 + n<5 与缺文件兜底
- save_ai_decision: JSONL 追加 + ensure_ascii=False + timestamp
- analyze_market_state: 手算表（IDLE/SCALPING/SWING 三态 + 高波动空仓禁开）
- SessionWarner: 同 key 同天去重 / 跨天重置
- PromptBuilder: mode 分派 + 系统 prompt 骨架 + 用户 prompt 手算
  （基差 -20 点/-0.40% 贴水、保证金 4980×200×0.15=149400、冷却剩 10.0 分钟、
   休市倒计时 75 分钟）
"""
import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from quantai.ai_decision import (PromptBuilder, SessionWarner,
                                 analyze_market_state,
                                 compute_signal_stats_text,
                                 detect_signal_type, save_ai_decision)
from quantai.config import MIN_CONFIDENCE


# ---------- detect_signal_type（真源 L1339–1356） ----------

def test_detect_signal_type_keyword_table():
    assert detect_signal_type("L12a突破入场") == "L12a"
    assert detect_signal_type("L3回踩确认") == "L3"
    assert detect_signal_type("L22趋势跟随") == "L22"
    assert detect_signal_type("D17跌破支撑") == "D17"
    assert detect_signal_type("D0首仓试探") == "D0"
    assert detect_signal_type("条件单触发入场") == "条件单"
    assert detect_signal_type("conditional order") == "条件单"
    assert detect_signal_type("同向加仓1手") == "加仓"
    assert detect_signal_type("换月移仓") == "换月"
    assert detect_signal_type("止盈离场") == "持仓平仓"
    assert detect_signal_type("止损离场") == "持仓平仓"
    assert detect_signal_type("") == "未标注"
    assert detect_signal_type(None) == "未标注"
    assert detect_signal_type("随手开一单") == "普通开仓"


def test_detect_signal_type_loop_order_priority():
    # 循环序 ("L12a", "L3", "L22", "D17", "D0") 先命中先返回
    assert detect_signal_type("L12a与L3同时出现") == "L12a"
    assert detect_signal_type("L3条件单") == "L3"          # 关键词表优先于"条件单"
    assert detect_signal_type("加仓后止损") == "加仓"       # "加仓"先于"止损"


# ---------- compute_signal_stats_text（真源 L1287–1337） ----------

def _write_trade_log(path, rows):
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "event_type", "symbol", "direction",
                         "volume", "price", "pnl", "balance_after", "ai_reason"])
        for r in rows:
            writer.writerow(r)


def test_compute_signal_stats_text_missing_file():
    assert compute_signal_stats_text(trade_log_file="Z:/no/such/file.csv") == ""


def test_compute_signal_stats_text_sample_too_small(tmp_path):
    f = tmp_path / "trade_log.csv"
    _write_trade_log(str(f), [
        ["2026-08-28 09:40:00", "CLOSE", "IM2608", "LONG", 1, 5100, "100", "", "L3"],
    ])
    assert compute_signal_stats_text(trade_log_file=str(f)) == ""   # n=1 < 5


def test_compute_signal_stats_text_hand_calc(tmp_path):
    f = tmp_path / "trade_log.csv"
    _write_trade_log(str(f), [
        # L3 ×3 全胜（+100/+100/+100），D0 ×2 全亏（-50/-60）
        ["2026-08-28 09:40:00", "CLOSE", "IM2608", "LONG", 1, 5100, "100", "200100", "L3突破止盈"],
        ["2026-08-28 10:00:00", "CLOSE", "IM2608", "LONG", 1, 5100, "100", "200200", "L3再次止盈"],
        ["2026-08-28 10:30:00", "CLOSE", "IM2608", "SHORT", 1, 5000, "100", "200300", "L3第三次"],
        ["2026-08-28 11:00:00", "CLOSE", "IM2608", "SHORT", 1, 4950, "-50", "200250", "D0止损"],
        ["2026-08-28 13:30:00", "CLOSE", "IM2608", "SHORT", 1, 4950, "-60", "200190", "D0再次止损"],
    ])
    text = compute_signal_stats_text(trade_log_file=str(f))
    # 手算: L3 胜率 3/3=100%, 平均 300/3=+100; D0 胜率 0/2=0%, 平均 -110/2=-55
    assert "L3: 3笔, 胜率100%, 平均+100元" in text
    assert "D0: 2笔, 胜率0%, 平均-55元" in text
    assert "总计: 5 笔" in text
    assert text.index("L3") < text.index("D0")   # 按 n 降序排列


def test_compute_signal_stats_text_skips_open_and_bad_rows(tmp_path):
    f = tmp_path / "trade_log.csv"
    _write_trade_log(str(f), [
        ["2026-08-28 09:31:00", "OPEN", "IM2608", "LONG", 1, 5000, "", "", "OPENX开仓"],
        ["2026-08-28 09:40:00", "CLOSE", "IM2608", "LONG", 1, 5100, "100", "", "L3"],
        ["2026-08-28 10:00:00", "CLOSE", "IM2608", "LONG", 1, 5100, "100", "", "L3"],
        ["2026-08-28 10:30:00", "CLOSE", "IM2608", "SHORT", 1, 5000, "100", "", "L3"],
        ["2026-08-28 11:00:00", "CLOSE", "IM2608", "SHORT", 1, 4950, "-50", "", "D0"],
        ["2026-08-28 13:30:00", "CLOSE", "IM2608", "SHORT", 1, 4950, "-60", "", "D0"],
        ["2026-08-28 14:00:00", "CLOSE", "IM2608", "LONG", 1, 5000, "abc", "", "BADX"],  # pnl 非法 → 跳过
    ])
    text = compute_signal_stats_text(trade_log_file=str(f))
    assert text != ""                              # 有效 CLOSE 恰好 5 条
    assert "普通开仓" not in text                   # BADX 行被跳过（未计入任何桶）
    assert "总计: 5 笔" in text


# ---------- save_ai_decision（真源 L5325–5338） ----------

def test_save_ai_decision_roundtrip_and_append(tmp_path):
    f = tmp_path / "ai_decisions.jsonl"
    save_ai_decision({"action": "WAIT", "reason": "观望"}, log_file=str(f))
    save_ai_decision({"action": "BUY", "volume": 1}, log_file=str(f))
    lines = f.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2                          # 追加模式
    rec1 = json.loads(lines[0])
    assert rec1["decision"]["action"] == "WAIT"
    assert "timestamp" in rec1
    assert "观望" in lines[0]                       # ensure_ascii=False（中文原样）


# ---------- analyze_market_state（真源 L5355–5374，纯决策化） ----------

def test_analyze_market_state_hand_calc():
    # 非交易时段 → IDLE（最优先）
    assert analyze_market_state(is_trading_time=False, stress_level=1.0,
                                position_direction=None,
                                atr_15=20.0, atr_5=30.0) == "IDLE"
    # 高波动且空仓 → 禁止开仓 → IDLE
    assert analyze_market_state(is_trading_time=True, stress_level=2.0,
                                position_direction=None,
                                atr_15=20.0, atr_5=30.0) == "IDLE"
    # 高波动但有持仓 → 不走禁开分支，继续 ATR 判定: 30/20=1.5>1.3 → SCALPING
    assert analyze_market_state(is_trading_time=True, stress_level=2.5,
                                position_direction="LONG",
                                atr_15=20.0, atr_5=30.0) == "SCALPING"
    # 正常波动 + 比值 1.5 > 1.3 → SCALPING
    assert analyze_market_state(is_trading_time=True, stress_level=1.0,
                                position_direction=None,
                                atr_15=20.0, atr_5=30.0) == "SCALPING"
    # 比值 1.0 ≤ 1.3 → SWING
    assert analyze_market_state(is_trading_time=True, stress_level=1.0,
                                position_direction="LONG",
                                atr_15=20.0, atr_5=20.0) == "SWING"
    # ATR 未就绪（任一为 0）→ 无法判定 → SWING
    assert analyze_market_state(is_trading_time=True, stress_level=1.0,
                                position_direction=None,
                                atr_15=0.0, atr_5=30.0) == "SWING"
    assert analyze_market_state(is_trading_time=True, stress_level=1.0,
                                position_direction=None,
                                atr_15=20.0, atr_5=0.0) == "SWING"


# ---------- SessionWarner（真源 _warn_once_per_session L1276–1285） ----------

def test_session_warner_dedup_same_day_and_reset_next_day(caplog):
    day1 = datetime(2026, 8, 28, 10, 0)
    day2 = datetime(2026, 8, 29, 10, 0)
    clock = {"now": day1}
    w = SessionWarner(now_fn=lambda: clock["now"])
    with caplog.at_level(logging.WARNING):
        w.warn("k1", "第一次")
        w.warn("k1", "同天同 key 去重")       # 不告警
        w.warn("k2", "不同 key 照告")
        clock["now"] = day2
        w.warn("k1", "跨天重新告警")
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert msgs == ["第一次", "不同 key 照告", "跨天重新告警"]


# ---------- PromptBuilder（真源 L980–1274 / L2051–2105） ----------

def _make_prompt_builder(**overrides):
    mds = SimpleNamespace(
        symbol="CFFEX.IM2608",
        im_quote=SimpleNamespace(last_price=4980.0),
        tech_data_text="FAKE_TECH_DATA",
        get_basis_info=lambda: {"index_price": 5000.0, "im_price": 4980.0,
                                "basis": -20.0, "basis_pct": -0.4,
                                "days_to_expiry": 12})
    mcs = SimpleNamespace(atr_5=20.0, atr_15=50.0, atr_60=60.0,
                          stress_level=1.0, oi_state_text="持仓量数据不可用")
    pm = SimpleNamespace(
        position={"direction": None, "volume": 0, "entry_price": 0.0,
                  "stop_loss": 0.0, "take_profit": 0.0},
        conditional_order=None)
    kwargs = dict(
        mds=mds, mcs=mcs, pm=pm,
        calendar=SimpleNamespace(is_trading_time=lambda now=None: False),
        circuit_breaker=SimpleNamespace(check=lambda: (False, "")),
        daily_limiter=SimpleNamespace(check=lambda: (False, "")),
        stopout=SimpleNamespace(last_stopout_dir=None, last_stopout_time=None),
        tail_fn=lambda: (False, ""),
        left_side_fn=lambda: "（左侧信号区）",
        account_fn=lambda: SimpleNamespace(balance=200000.0, position_profit=0.0),
        sizer=SimpleNamespace(get_max_lots=lambda: 3),
        news_items_fn=lambda: [],
        now_fn=lambda: datetime(2026, 8, 28, 10, 15))
    kwargs.update(overrides)
    return PromptBuilder(**kwargs)


def test_prompt_builder_mode_dispatch():
    pb = _make_prompt_builder()
    swing_sys, swing_user = pb.build_prompt("SWING")
    assert "波段模式特有规则" in swing_sys
    assert "600-1200" in swing_sys               # 波段间隔建议范围
    scalping_sys, _ = pb.build_prompt("SCALPING")
    assert "短线模式特有规则" in scalping_sys
    assert "120-600" in scalping_sys             # 短线间隔建议范围
    # 未知 mode → else 分支 → SCALPING（真源 L5405–5410 分派语义）
    other_sys, _ = pb.build_prompt("OTHER")
    assert "短线模式特有规则" in other_sys


def test_prompt_builder_system_prompt_skeleton():
    pb = _make_prompt_builder()
    shared = pb.build_shared_system_prompt("SWING")
    assert '"action": "BUY"|"SELL"|"WAIT"' in shared
    assert f"{MIN_CONFIDENCE}" in shared          # 阈值插值（0.55）
    assert "止损距离的动态选择" in shared
    assert "conditional_entry" in shared
    assert "next_interval_sec" in shared


def test_prompt_builder_user_prompt_hand_calc():
    pb = _make_prompt_builder()
    _, user = pb.build_prompt("SWING")
    # 基差手算: 4980-5000 = -20 点; -20/5000×100 = -0.40% → 贴水
    assert "中证1000指数: 5000.00" in user
    assert "IM主力(CFFEX.IM2608): 4980.00" in user
    assert "基差: -20.00点 (-0.40%)" in user
    assert "状态: 贴水" in user
    assert "距到期: 12天" in user
    # 空仓 + 无条件单
    assert "## 当前持仓: 空仓" in user
    assert "## 📌 当前挂单: 无条件单" in user
    # 资金手算: 权益 200000; 保证金 = 4980×200×0.15 = 149400
    assert "动态权益: 200000.00 元" in user
    assert "每手保证金约: 149400.00 元" in user
    assert "最大可开手数（安全线）: 3 手" in user
    # ATR 环境
    assert "15分钟ATR: 50.00 点" in user
    assert "5分钟ATR: 20.00 点" in user
    assert "当前 Stress Level: 1.00" in user
    # 非交易时段 + 空新闻 + 左侧信号 + 技术数据
    assert "当前非交易时段" in user
    assert "（无重要快讯）" in user
    assert "（左侧信号区）" in user
    assert "FAKE_TECH_DATA" in user


def test_prompt_builder_user_prompt_position_and_conditional():
    pm = SimpleNamespace(
        position={"direction": "LONG", "volume": 2, "entry_price": 5000.0,
                  "stop_loss": 4950.0, "take_profit": 5100.0},
        conditional_order={"action": "BUY", "trigger_type": "PRICE_ABOVE",
                           "trigger_price": 5010.0, "stop_loss": 4990.0,
                           "take_profit": 5060.0,
                           "created_date": "2026-08-28"})
    pb = _make_prompt_builder(pm=pm)
    _, user = pb.build_prompt("SWING")
    assert "方向: LONG" in user
    assert "手数: 2" in user
    assert "开仓均价（期货）: 5000.00" in user
    assert "当前止损（期货）: 4950.00" in user
    assert "当前止盈（期货）: 5100.00" in user
    assert "当前挂单（未触发条件单，本轮决策会覆盖它）" in user
    assert "BUY 触发: PRICE_ABOVE@5010.0" in user
    assert "止损: 4990.0 止盈: 5060.0" in user
    assert "创建于: 2026-08-28（仅当日有效）" in user


def test_prompt_builder_user_prompt_risk_state_injection():
    now = datetime(2026, 8, 28, 10, 15)
    pb = _make_prompt_builder(
        circuit_breaker=SimpleNamespace(check=lambda: (True, "当日亏损超1.5%")),
        daily_limiter=SimpleNamespace(check=lambda: (True, "已达6次上限")),
        tail_fn=lambda: (True, "尾盘时段滑点大"),
        stopout=SimpleNamespace(last_stopout_dir="LONG",
                                last_stopout_time=now - timedelta(seconds=300)),
        now_fn=lambda: now)
    _, user = pb.build_prompt("SWING")
    assert "🚫 熔断中：当日亏损超1.5%" in user
    assert "🛡️ 尾盘禁开仓：尾盘时段滑点大" in user
    assert "🔒 日次数上限：已达6次上限" in user
    # 冷却手算: 已过 300s=5 分钟, 剩 (900-300)/60=10.0 分钟
    assert "⏳ 止损冷却中：LONG 方向 5 分钟前止损，剩 10.0 分钟禁同向再开" in user


def test_prompt_builder_user_prompt_trading_time_text():
    # 10:15 → 距 11:30 休市 75 分钟; 不在动量/噪声/尾盘窗口
    pb = _make_prompt_builder(
        calendar=SimpleNamespace(is_trading_time=lambda now=None: True),
        now_fn=lambda: datetime(2026, 8, 28, 10, 15))
    _, user = pb.build_prompt("SWING")
    assert "下一休市: 上午休市 11:30（还有约75分钟）" in user
    assert "动量黄金时段" not in user
    assert "开盘噪声时段" not in user
    assert "尾盘禁开新仓时段" not in user
    # 10:45 → 动量黄金时段（10:30-11:00）
    pb2 = _make_prompt_builder(
        calendar=SimpleNamespace(is_trading_time=lambda now=None: True),
        now_fn=lambda: datetime(2026, 8, 28, 10, 45))
    _, user2 = pb2.build_prompt("SWING")
    assert "⏰ 当前为动量黄金时段" in user2
    assert "下一休市: 上午休市 11:30（还有约45分钟）" in user2
