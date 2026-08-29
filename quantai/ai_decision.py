"""AI 决策层（阶段 5）：Prompt 构建 + 信号统计 + 决策落盘 + 市场状态判定.

真源映射（design.md §4.2 ai_decision 9 方法）:
- _build_shared_system_prompt L980–1124  → PromptBuilder.build_shared_system_prompt
- _build_shared_user_prompt   L1126–1274 → PromptBuilder.build_shared_user_prompt
- _warn_once_per_session      L1276–1285 → SessionWarner.warn（上帝类方法收拢为独立类，
                                            LeftSideStrategy.warn_fn 注入接线，阶段 3 决策 8 收口）
- _compute_signal_stats_text  L1287–1337 → compute_signal_stats_text（模块级纯函数）
- _detect_signal_type         L1339–1356 → detect_signal_type（真源 @staticmethod → 模块级）
- _build_swing_prompt         L2051–2074 → PromptBuilder.build_swing_prompt
- _build_scalping_prompt      L2076–2105 → PromptBuilder.build_scalping_prompt
- save_ai_decision            L5325–5338 → save_ai_decision（模块级）
- _analyze_market_state       L5355–5374 → analyze_market_state（纯决策化：状态值由编排层传入）

结构差异（行为等价）:
- 真源 prompt 方法读上帝类字段（news_cache/im_quote/atr_* 等）→ 本版经构造注入的
  服务引用读取（mds/mcs/pm/cb/limiter/stopout/calendar/left_side_fn 等）
- 新闻缓存读取的 ``with self.news_lock`` 移入 NewsManager.get_news（线程安全取副本），
  ``[-NEWS_CACHE_MAX:]`` 截断保留在本模块（真源 L1133）
- 真源 L5327 函数内 ``import json`` → 本版模块级导入（行为等价）
- ``build_prompt(mode)`` 为 pipeline.execute_ai_cycle 的 prompt_fn 注入点
  （真源 L5405–5410 的 mode 分派）
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, time as dt_time
from typing import Callable, Optional, Tuple

from .config import (AI_DECISIONS_FILE, MIN_CONFIDENCE, NEWS_CACHE_MAX,
                     SCALPING_ATR_RATIO, STOP_ADJUST_COOLDOWN,
                     STOPOUT_COOLDOWN_SEC, TRADE_LOG_FILE)

logger = logging.getLogger(__name__)


# ========== 真源 _detect_signal_type L1339–1356（@staticmethod → 模块级） ==========

def detect_signal_type(reason: str) -> str:
    """从交易原因中识别信号类型（8/14 新增）"""
    if not reason:
        return "未标注"
    r = reason
    for kw in ("L12a", "L3", "L22", "D17", "D0"):
        if kw in r:
            return kw
    if "条件单" in r or "conditional" in r.lower():
        return "条件单"
    if "加仓" in r:
        return "加仓"
    if "换月" in r:
        return "换月"
    if "止盈" in r or "止损" in r:
        return "持仓平仓"
    return "普通开仓"


# ========== 真源 _compute_signal_stats_text L1287–1337 ==========

def compute_signal_stats_text(trade_log_file: str = TRADE_LOG_FILE) -> str:
    """
    从历史交易记录统计各信号类型胜率，回喂给 AI（8/14 新增）
    LLM 风控最佳实践：用真实历史统计校准 AI 信心，而非让 AI 凭感觉打分
    trade_log.csv 的 OPEN 记录含 ai_reason，可识别信号来源
    """
    try:
        stats = {}
        n = 0
        if not os.path.exists(trade_log_file):
            return ""
        import csv as _csv
        with open(trade_log_file, 'r', encoding='utf-8') as f:
            reader = _csv.DictReader(f)
            for row in reader:
                if row.get('event_type') != 'CLOSE':
                    continue
                pnl = 0.0
                try:
                    pnl = float(row.get('pnl', 0) or 0)
                except Exception:
                    continue
                reason = (row.get('ai_reason', '') or '')
                direction = (row.get('direction', '') or 'unknown')
                # 从平仓 reason 中识别信号类型
                sig = detect_signal_type(reason)
                if sig not in stats:
                    stats[sig] = {'n': 0, 'win': 0, 'pnl': 0.0}
                stats[sig]['n'] += 1
                n += 1
                if pnl > 0:
                    stats[sig]['win'] += 1
                stats[sig]['pnl'] += pnl

        if n < 5:
            return ""  # 样本太少无统计意义，不注入 prompt

        lines = ["## 📊 历史信号统计（真实成交回测，用于校准信心）"]
        for sig, st in sorted(stats.items(), key=lambda x: -x[1]['n']):
            if st['n'] < 1:
                continue
            win_rate = st['win'] / st['n'] * 100
            avg_pnl = st['pnl'] / st['n']
            lines.append(
                f"- {sig}: {st['n']}笔, 胜率{win_rate:.0f}%, 平均{avg_pnl:+.0f}元"
            )
        lines.append(f"- 总计: {n} 笔（信心评分请参考该信号历史胜率，不要凭感觉）")
        return "\n".join(lines) + "\n"
    except Exception as e:
        logging.warning(f"历史信号统计失败: {e}")
        return ""


# ========== 真源 save_ai_decision L5325–5338 ==========

def save_ai_decision(decision: dict, log_file: str = AI_DECISIONS_FILE) -> None:
    """将 AI 原始决策 JSON 追加到 ai_decisions.jsonl"""
    # 修复 M6: 路径基于脚本目录（本版经 config.AI_DECISIONS_FILE 统一 DATA_DIR）
    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "decision": decision
    }
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception as e:
        logging.error(f"保存 AI 决策失败: {e}")


# ========== 真源 _analyze_market_state L5355–5374（纯决策化） ==========

def analyze_market_state(*, is_trading_time: bool, stress_level: float,
                         position_direction: Optional[str],
                         atr_15: float, atr_5: float) -> str:
    """
    分析当前市场状态，返回 "SCALPING" | "SWING" | "IDLE"
    - SCALPING: 短线波动放大/突破行情，适合5min高频决策
    - SWING: 趋势行情，适合15min波段决策
    - IDLE: 不适合交易（高波动禁止开仓 / 非交易时段）
    （真源读 self._is_trading_time()/self.stress_level/current_position/self.atr_*，
    本版纯决策化：状态值由编排层 system.run 传入）
    """
    if not is_trading_time:
        return "IDLE"
    # 高波动且无持仓 → 禁止开仓
    if stress_level >= 2.0 and not position_direction:
        return "IDLE"

    # 用5minATR/15minATR比值判断短线活跃度
    if atr_15 > 0 and atr_5 > 0:
        atr_ratio = atr_5 / atr_15
        if atr_ratio > SCALPING_ATR_RATIO:
            return "SCALPING"

    return "SWING"


# ========== 真源 _warn_once_per_session L1276–1285（收拢为独立类） ==========

class SessionWarner:
    """同一天内同一 key 只告警一次，避免刷屏（真源 _warn_once_per_session）.

    真源为上帝类方法（``self._warn_log`` hasattr 懒初始化）→ 本版收拢为独立类，
    LeftSideStrategy 的 ``warn_fn`` 注入接线（阶段 3 决策 8 的统一收口）。
    """

    def __init__(self, now_fn: Callable[[], datetime] = datetime.now):
        self._warn_log = {}   # 真源 hasattr 懒初始化 → 构造初始化（行为等价）
        self.now_fn = now_fn

    def warn(self, key: str, msg: str) -> None:
        today = self.now_fn().date()
        last_date = self._warn_log.get(key)
        if last_date == today:
            return
        self._warn_log[key] = today
        logging.warning(msg)


# ========== 真源 _build_*_prompt L980/1126/2051/2076 ==========

class PromptBuilder:
    """系统/用户 Prompt 构造器（SWING + SCALPING 双频）.

    真源四个方法读上帝类字段 → 本版经构造注入的服务引用读取；
    ``mode`` 参数在真源函数体中未使用（L980/L1126），签名原样保留。
    """

    def __init__(self, *, mds, mcs, pm, calendar, circuit_breaker, daily_limiter,
                 stopout, tail_fn: Callable[[], Tuple[bool, str]],
                 left_side_fn: Callable[[], str], account_fn: Callable,
                 sizer, news_items_fn: Callable[[], list],
                 now_fn: Callable[[], datetime] = datetime.now):
        self.mds = mds                      # MarketDataService（symbol/im_quote/tech_data_text/get_basis_info）
        self.mcs = mcs                      # MarketContextService（atr_5/15/60/stress_level/oi_state_text）
        self.pm = pm                        # PositionManager（position/conditional_order）
        self.calendar = calendar            # TradingCalendar（is_trading_time）
        self.circuit_breaker = circuit_breaker  # CircuitBreaker.check()
        self.daily_limiter = daily_limiter  # DailyTradeLimiter.check()
        self.stopout = stopout              # StopOutCooldown（last_stopout_dir/last_stopout_time）
        self.tail_fn = tail_fn              # → SessionPlaysService.check_tail_session
        self.left_side_fn = left_side_fn    # → LeftSideStrategy.compute_left_side_signals
        self.account_fn = account_fn        # → api.get_account
        self.sizer = sizer                  # PositionSizer.get_max_lots
        self.news_items_fn = news_items_fn  # → NewsManager.get_news
        self.now_fn = now_fn

    def build_prompt(self, mode: str) -> Tuple[str, str]:
        """pipeline.execute_ai_cycle 的 prompt_fn 注入点（真源 L5405–5410 mode 分派）"""
        if mode == "SWING":
            return self.build_swing_prompt()
        return self.build_scalping_prompt()

    def build_swing_prompt(self) -> Tuple[str, str]:
        """
        波段决策Prompt（15分钟级别）
        共享完整风控框架 + 波段特有规则
        """
        shared = self.build_shared_system_prompt("SWING")
        swing_specific = f"""
## 🎯 波段模式特有规则

### 趋势判断
用多周期均线排列（查看技术面各周期MA值）：
- 强势：5min/15min/30min/60min/日线至少4个同向 → 至少 confidence 0.7+
- 中性：3个同向 → confidence {MIN_CONFIDENCE}-0.7
- 弱势：均线缠绕无方向 → WAIT

### 止损特别注意
- 正常(Stress<2.0)：止损距=1.5×15minATR
- 高波动(Stress≥2.0)：禁止新开仓；已有持仓止损收紧至1.0×5minATR
- 止损与现价保持1.5×15minATR距离

### next_interval_sec
建议下次波段决策间隔(秒)，范围600-1200。趋势明确→短间隔，震荡→长间隔。
"""
        return shared + swing_specific, self.build_shared_user_prompt("SWING")

    def build_scalping_prompt(self) -> Tuple[str, str]:
        """
        短线/超短线决策Prompt（5分钟级别）
        共享完整风控框架 + 短线特有规则
        """
        shared = self.build_shared_system_prompt("SCALPING")
        scalping_specific = f"""
## 🎯 短线模式特有规则

### 核心原则
1. **只做突破**：明确突破前5分高点(多)/低点(空)才入场
2. **利润目标**：止盈15-30指数点，盈亏比≥1.5:1
3. **止损**：8-15指数点，用5minATR的0.8-1.2倍
4. **不追**：已连续同向>10点则等回调
5. **震荡不交易**：最近10分振幅<0.15%则WAIT
6. **不加仓**：短线已有持仓不再加仓；若反向信号出现，先平仓

### 信心调整
短线模式将上述通用信心评分框架的结果按如下映射：
- 通用信心 0.7+ 且满足突破+放量+均线支撑 → 可立即入场
- 通用信心 {MIN_CONFIDENCE}-0.7 且突破但量不足 → 设条件单
- 通用信心 < {MIN_CONFIDENCE} → WAIT

### 仓位
短线统一1手。adjust_existing 始终为 null。

### next_interval_sec
建议下次短线间隔(秒)，120-600：高确定性突破→120，正常→300，横盘→600。
"""
        return shared + scalping_specific, self.build_shared_user_prompt("SCALPING")

    def build_shared_system_prompt(self, mode: str) -> str:
        """
        构建两种模式共享的完整风控框架，涵盖原版 autotrade.py 的所有详细规则。
        mode: "SWING" | "SCALPING"
        （真源函数体未使用 mode，签名原样保留）
        """
        shared = f"""你是中证1000指数期货(IM)量化交易执行器。核心使命：**在市场混沌中捕捉具有正向期望的交易机会**，而非追求完美信号。

输出严格JSON：必须包含所有字段，不要添加额外文字或注释。
{{
  "action": "BUY"|"SELL"|"WAIT",
  "volume": 数字,
  "conditional_entry": {{
    "trigger_type": "PRICE_ABOVE"|"PRICE_BELOW",
    "trigger_price": 数字,
    "stop_loss": 数字,
    "take_profit": 数字
  }}|null,
  "stop_loss": 数字,
  "take_profit": 数字,
  "confidence": 0.0-1.0,
  "adjust_existing": {{"new_stop_loss":数字|null,"new_take_profit":数字|null}}|null,
  "next_interval_sec": 数字,
  "reason": "简要决策逻辑（必须包含信号评分明细、为何使用或不使用条件单的说明）"
}}

## 📈 波动率风控框架（基于 ATR）
系统已提供5minATR、15minATR、60minATR（均为IM期货价格点数），以及 Stress Level = 5minATR / 60minATR。

### 止损距离的动态选择（紧凑型日内交易）
- **平静期（Stress < 1.2）**：止损距离 = **0.8~1.2 倍 5minATR**（紧凑！绝不能超过1.2×5minATR）
- **正常期（1.2 ≤ Stress < 2.0）**：止损距离 = **1.0~1.5 倍 5minATR**（优先1.0）
- **高波动期（Stress ≥ 2.0）**：禁止新开仓；已有持仓止损收紧至 **1.0~1.5 倍 5minATR**
- **绝对上限**：止损距离 ≤ 1.5×5minATR（6/11 案例：5minATR=23, 上限=35点；过去用 1.5×15minATR=75点太宽）

止损计算方式：
1. 用对应倍数 × 15minATR(或5minATR) 得标准距离 D
2. 寻找最近关键技术位（前低/前高、密集成交区、布林带上下轨），计算技术位到入场价的距离 D_tech
3. **仅当 D_tech 与 D 的差值 ≤ 0.3 倍 ATR 时**，才可将止损放在技术位外侧（技术位 ± 3 最小变动价位）。否则**必须严格使用 D**
4. 止损价必须在入场价基础上沿持仓不利方向位移精确的 D 点，不得随意取整或向远侧大幅偏移

**禁止行为**：
- 禁止以"技术位很远"为由，把止损放在比 ATR 计算值更远的位置
- 禁止为使盈亏比达标而人为拉宽止损
- 禁止在开仓时设置过宽止损，然后依赖后续 adjust 去收紧

### 止盈设置
- **默认盈亏比 ≥ 1.5:1**（止盈距离至少为止损的1.5倍），确保正向期望
- **趋势强烈信号（ADX>25 且多周期均线完美排列）**：允许且鼓励将盈亏比上调至 **2:1**，让利润充分奔跑
- 当价格运行到关键阻力/支撑位，可通过 adjust_existing 主动收紧止盈，提前锁定利润

### 移动止损与调整
- **保本上移门槛**：浮盈 > **1.5×15minATR** 才能把止损上移至开仓成本价（不是1.0×，避免被正常回踩扫出）
- **后续移动止损**：新止损与现价距离 **1.0~1.5×15minATR**，每次至少 0.5×ATR 步长，严禁贴价
- ADX>30 趋势加速期可放宽移动止损距离

### 动态止盈调整（主动锁定利润）

满足任一即通过 `adjust_existing.new_take_profit` 收窄止盈：
1. **冲高乏力**：浮盈 > 2.0×止损距离 + 出现长影线/5min均线拐头/2 根 K 线未创新高新低 → 止盈调至"当前价 ± 0.3×ATR"
2. **多周期分化**：30min/60min 与 15min 方向不一致 → 止盈缩至 1.0×止损距离
3. **高波动转换**：Stress 1.2→1.5 突变 → 止盈调至"当前价 ± 0.5×ATR"
4. **时间驱动**：距休市<15分钟 + 浮盈≥1.0×止损距离 → 止盈调至"当前价 ± 0.5×ATR"

**禁止**：浮盈<1.0×止损距离时手动干预；多周期仍同向时不主动收窄。

## ⚠️ 市场应激模式（基于 Stress Level）
- **Stress < 2.0（正常）**：正常执行交易策略
- **Stress ≥ 2.0（高波动）**：
  - **暂停所有新开仓**（action 必须为 WAIT，conditional_entry 必须为 null）
  - **若已有持仓**：止损收紧至 **1.0～1.5 倍 5minATR**（优先用1.0倍）
  - 止盈目标可适度缩短，快速锁定利润
- **Stress ≥ 3.0（极端波动）**：
  - 除以上要求外，建议立即减仓 50%（通过 adjust_existing 给出更激进的止损或建议平仓）
  - 空仓时不得有任何开仓意图

## 🔄 核心决策哲学（积极交易导向）
- 你的存在价值是**寻找交易机会**，而不是避免交易。错失机会同样是风险
- 只有以下三种情形才应输出 WAIT：
  1. 多空信号剧烈冲突，完全无法分辨主次方向
  2. 行情处于极端无序的小幅横盘（价格振幅<0.3%且持续超过30分钟无任何方向突破迹象）
  3. 距离休市（午休/收盘）不足5分钟且当前无持仓
- 只要某个方向的证据多于另一个方向，即使优势微弱，也必须采取行动
- 你必须始终假设"空仓就是机会成本"，没有持仓时应更积极寻找入场点

## 📊 信号信心评分规则（定量计算）
信心采用"基础分 + 加分项 - 减分项"计算，起始基础分固定为 0.5。

### 加分项（可叠加，上限 +0.3）
**技术面加分**：
- 至少3个周期（5min/15min/30min/60min/日线）均线同向排列（多头或空头）：+0.1
- 价格突破过去30分钟最高/最低点并站稳（最新价保持在突破位同侧）：+0.1
- MACD 出现金叉/死叉且柱状线持续扩张：+0.1（系统已按 A 股周期参数计算：5min=(7,14,5)、15/30min/日线/周线=(10,20,7)、60min=(8,20,6)——不要使用美股默认 (12,26,9)）
- 布林带宽度收缩至近 20 根 K 线最低水平附近（预示变盘）：+0.1
**基差与合约加分**（**基差仅作辅助参考，技术面为主导**）：
- **重要原则**：方向判定以技术面（多周期均线、形态、突破）为准，基差**不能反转**技术面信号
- 贴水（负基差）天然有利于多头持仓（持有成本更低），但**不等于"应做多"**；升水同理
- 深度贴水（>1.5%）+ 技术面已出现筑底/突破信号：+0.05（轻微加分）
- 大幅升水（>0.5%）+ 技术面已出现顶部/下破信号：+0.05（轻微加分）
- 单纯基差异常（无论贴水还是升水）但技术面不支持：**不加分也不减分**
**消息面加分**：
- 新闻中出现明确且未被市场充分消化的产业利好/利空：+0.1

### 减分项
- 出现重大黑天鹅事件（1分钟内波幅>0.5%且无主导方向）：-0.2

最终信心 = min(1.0, 0.5 + 总加分 - 总减分)
**最终信心 ≥ {MIN_CONFIDENCE} 时可开仓；< {MIN_CONFIDENCE} 时必须 WAIT（conditional_entry 必须为 null）**

## 💰 仓位规则（按信心级别）
- 仓位按信心级别占最大可开手数(max_lots)的百分比，统一向上取整，最少1手
- 信心 {MIN_CONFIDENCE} ~ 0.65（试错区间）：开 max(1, ceil(max_lots × 12%)) 手
- 信心 0.65 ~ 0.75（轻度确信）：开 max(1, ceil(max_lots × 22%)) 手
- 信心 0.75 ~ 0.85（中度确信）：开 max(1, ceil(max_lots × 32%)) 手
- 信心 ≥ 0.85（高度确信）：开 max(1, min(ceil(max_lots × 42%), floor(max_lots × 50%))) 手
- **止损约束（必须检查）**：(入场价-止损价)绝对值 × 200元/点 × 手数 ≤ 账户动态权益的 2%
  若手数过大，应自动削减手数至满足此条件
- 同向加仓手数同样按上述信心级别计算，上限为 (max_lots - 现有持仓手数)

## 🧭 数据体系说明（务必注意）
- 技术面数据全部基于**中证1000指数**点位
- 持仓信息（开仓均价、止损、止盈）全部是**IM 期货价格**
- 两者存在基差（一般为负，期货低于指数），不要直接比较绝对值
- 当你根据技术位调整时，先用指数点位思考，输出时填写**指数点位**，系统会自动转换
- ⚠️ 所有你填写的 stop_loss、take_profit、adjust_existing、conditional_entry 中的价格
  必须是**中证1000指数点位**，绝不能填期货价格！否则会导致止损严重偏移

## 📈 条件单使用规则
- 条件单是你的**进攻利器**，不要求在绝对完美的位置才设
- 只要你能识别出一个在30分钟内可能被触及的关键价位（均线、前高/前低、密集成交区上下沿、布林带轨），就应设置条件单
- **强制规则**：如果最近30分钟价格在窄幅震荡（振幅<0.4%），你必须选择一个可能突破的方向设置条件单
- trigger_price 与当前价格距离可低至 0.15%，提高触发概率
- 条件单的 stop_loss 和 take_profit 必须基于 trigger_price 科学设置，止损偏紧

## 📉 基差与到期日处理
- **基差不构成方向偏好**：贴水/升水仅作为"持仓成本/情绪"参考，不影响做多做空的方向选择
- 技术面看多做多、技术面看空做空；基差极值（贴水>2%或升水>1%）可作为情绪背离的**辅助确认**，但不强制开仓
- 距合约到期日 < 3 天时，避免开新仓，但仍可调整现有持仓的止损止盈

## 🔧 adjust_existing 说明
- 仅当已有持仓时才可填写此字段；空仓时为 null
- 根据新技术位动态移动止损止盈，让利润奔跑。趋势加速时可适当上移止盈
- 所有调整必须在 reason 中简述依据
- 最近 {STOP_ADJUST_COOLDOWN} 秒内已调过止损则跳过（输出 null）
"""
        return shared

    def build_shared_user_prompt(self, mode: str) -> str:
        """
        构建两种模式共享的用户提示数据块，包含完整新闻和交易时段信息
        mode: "SWING" | "SCALPING"
        （真源函数体未使用 mode，签名原样保留）
        """
        # 新闻 —— 只取最近 NEWS_CACHE_MAX 条（修复 M3: 防止全量注入导致 prompt 爆炸）
        # （真源 with self.news_lock 读缓存 → 本版 news_items_fn 内部加锁取副本）
        recent_news = self.news_items_fn()[-NEWS_CACHE_MAX:]
        news_text = "\n".join([
            f"- {item.get('time', '未知时间')}: {item.get('data', {}).get('content', '无内容')}"
            for item in recent_news
        ]) if recent_news else "（无重要快讯）"
        news_block = f"## 重要新闻\n{news_text}\n" if news_text.strip() else ""

        # 基差
        basis_info = self.mds.get_basis_info()
        basis_text = f"""## 基差与合约状态
- 中证1000指数: {basis_info['index_price']:.2f}
- IM主力({self.mds.symbol}): {basis_info['im_price']:.2f}
- 基差: {basis_info['basis']:.2f}点 ({basis_info['basis_pct']:.2f}%)
- 状态: {"贴水" if basis_info['basis'] < 0 else "升水"}
- 距到期: {basis_info['days_to_expiry']}天
"""

        # 持仓
        position = self.pm.position
        if position['direction']:
            pos_text = f"""## 当前持仓（⚠️ 以下为 IM 期货价格）
- 方向: {position['direction']}
- 手数: {position['volume']}
- 开仓均价（期货）: {position['entry_price']:.2f}
- 当前止损（期货）: {position['stop_loss']:.2f}
- 当前止盈（期货）: {position['take_profit']:.2f}
"""
        else:
            pos_text = "## 当前持仓: 空仓\n"

        # 8/17 修复: 风控状态注入 prompt（熔断/冷却/日次数）
        # 让 AI 在熔断/冷却/日次数受限时自然倾向 WAIT 或仅做持仓管理，
        # 避免反复生成会被系统拦截的 BUY/SELL 决策（8/17 实测 628 次拦截噪音）
        cb_blocked, cb_reason = self.circuit_breaker.check()
        tail_blocked, tail_reason = self.tail_fn()
        daily_blocked, daily_reason = self.daily_limiter.check()
        risk_state_lines = []
        if cb_blocked:
            risk_state_lines.append(f"🚫 熔断中：{cb_reason}")
        if tail_blocked:
            risk_state_lines.append(f"🛡️ 尾盘禁开仓：{tail_reason}")
        if daily_blocked:
            risk_state_lines.append(f"🔒 日次数上限：{daily_reason}")
        cooldown_active = False
        if self.stopout.last_stopout_dir:
            cooldown_elapsed = (self.now_fn() - self.stopout.last_stopout_time).total_seconds()
            if cooldown_elapsed < STOPOUT_COOLDOWN_SEC:
                cooldown_active = True
                risk_state_lines.append(
                    f"⏳ 止损冷却中：{self.stopout.last_stopout_dir} 方向 "
                    f"{cooldown_elapsed/60:.0f} 分钟前止损，"
                    f"剩 {(STOPOUT_COOLDOWN_SEC - cooldown_elapsed)/60:.1f} 分钟禁同向再开"
                )
        if risk_state_lines:
            pos_text += "\n## ⚠️ 当前风控状态（系统强制，AI 必须遵守）\n- " + "\n- ".join(risk_state_lines) + "\n"

        # 8/27: 当前挂单信息注入 prompt（AI 可感知已有条件单，避免重复/矛盾设置）
        conditional_order = self.pm.conditional_order
        if conditional_order and isinstance(conditional_order, dict):
            cd = conditional_order.get('created_date', '今日')
            pos_text += (
                f"\n## 📌 当前挂单（未触发条件单，本轮决策会覆盖它）\n"
                f"- 方向: {conditional_order.get('action')} 触发: "
                f"{conditional_order.get('trigger_type')}@{conditional_order.get('trigger_price')}\n"
                f"- 止损: {conditional_order.get('stop_loss')} 止盈: {conditional_order.get('take_profit')}\n"
                f"- 创建于: {cd}（仅当日有效）\n"
                f"若维持该单请在 conditional_entry 返回相同内容；若改变计划请返回新条件单或 null\n"
            )
        else:
            pos_text += "\n## 📌 当前挂单: 无条件单\n"

        # 资金
        account = self.account_fn()
        balance = (account.balance + account.position_profit) if account else 0
        max_lots = self.sizer.get_max_lots()
        margin_per_lot = (self.mds.im_quote.last_price * 200 * 0.15) if self.mds.im_quote.last_price > 0 else 0
        fund_text = f"""## 账户资金与仓位限制
- 动态权益: {balance:.2f} 元
- 每手保证金约: {margin_per_lot:.2f} 元
- 最大可开手数（安全线）: {max_lots} 手
"""

        # 技术数据
        tech = self.mds.tech_data_text if self.mds.tech_data_text else "技术数据获取中..."

        # ATR 和波动率环境
        atr_text = f"""## 波动率环境
- 15分钟ATR: {self.mcs.atr_15:.2f} 点
- 60分钟ATR: {self.mcs.atr_60:.2f} 点
- 5分钟ATR: {self.mcs.atr_5:.2f} 点
- 当前 Stress Level: {self.mcs.stress_level:.2f}（正常<2.0，高波动≥2.0，极端≥3.0）
"""

        # 量仓配合（期货核心指标，8/14 新增）
        oi_state = getattr(self.mcs, 'oi_state_text', '持仓量数据不可用')
        oi_text = f"""## 量仓配合（持仓量 OI）
{oi_state}
- 判定规则：增仓=主动进攻（强），减仓=平仓驱动（弱）
- 减仓上行/下行时禁止追多/追空（弱反弹/弱回落，持续性差）
"""

        # 当前时间和交易时段信息（恢复时间警告）
        now = self.now_fn()
        current_time_str = now.strftime('%Y-%m-%d %H:%M:%S')
        time_text = f"## 交易时间\n当前时间: {current_time_str}\n"
        if self.calendar.is_trading_time(now):
            t = now.time()
            if dt_time(9, 30) <= t <= dt_time(11, 29):
                minutes_to_break = (datetime.combine(now.date(), dt_time(11, 30)) - now).seconds // 60
                time_text += f"下一休市: 上午休市 11:30（还有约{minutes_to_break}分钟）\n"
            elif dt_time(13, 0) <= t <= dt_time(14, 59):
                minutes_to_break = (datetime.combine(now.date(), dt_time(15, 0)) - now).seconds // 60
                time_text += f"下一休市: 下午收盘 15:00（还有约{minutes_to_break}分钟）\n"
            elif dt_time(21, 0) <= t <= dt_time(22, 59):
                minutes_to_break = (datetime.combine(now.date(), dt_time(23, 0)) - now).seconds // 60
                time_text += f"下一休市: 夜盘收盘 23:00（还有约{minutes_to_break}分钟）\n"
            # 最后5分钟内警告
            if 'minutes_to_break' in locals() and minutes_to_break <= 5:
                time_text += "（⚠️ 即将休市，慎开新仓）\n"
            # ========== 8/14 新增：时段特征（BigQuant 中证1000 实证研究） ==========
            # 10:30-11:00 & 14:00-14:30: 成交+波动同步抬升 → 动量/突破最佳窗口
            # 09:30-10:00 & 14:45-15:00: 滑点大、噪声重 → 谨慎
            if dt_time(10, 30) <= t <= dt_time(11, 0) or dt_time(14, 0) <= t <= dt_time(14, 30):
                time_text += "⏰ 当前为动量黄金时段（10:30-11:00/14:00-14:30，成交+波动抬升），突破/趋势信号可信度更高\n"
            if dt_time(9, 30) <= t <= dt_time(10, 0):
                time_text += "⚠️ 开盘噪声时段（9:30-10:00），假突破概率高，入场需额外确认\n"
            if dt_time(14, 45) <= t <= dt_time(15, 0):
                time_text += "🚫 尾盘禁开新仓时段（14:45+，滑点大），只允许调整已有持仓\n"
            # ==============================================================
        else:
            time_text += "当前非交易时段\n"

        # 左侧机会信号
        left_side_signals = self.left_side_fn()

        # 历史信号统计（8/14 新增：校准 AI 信心用）
        signal_stats = compute_signal_stats_text()

        return f"""{news_block}{basis_text}{pos_text}{fund_text}{atr_text}{oi_text}{time_text}
{left_side_signals}
{signal_stats}
## 技术面数据
{tech}
"""


__all__ = [
    "PromptBuilder",
    "SessionWarner",
    "analyze_market_state",
    "compute_signal_stats_text",
    "detect_signal_type",
    "save_ai_decision",
]
