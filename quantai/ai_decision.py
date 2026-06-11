"""AI 决策层：Prompt 构造 + LLM 调用 + JSON 解析 + 落库.

设计要点：
- **Prompt 分层**：``_build_shared_system_prompt`` 通用风控框架（200 行级），
  ``_build_swing_prompt`` / ``_build_scalping_prompt`` 各加 30~50 行模式特例
- **JSON 输出约束**：要求 LLM 严格按字段返回，便于程序化执行 + 信心量化
- **失败兜底**：解析失败 → 跳过本轮决策（不抛异常进主循环）
- **可观测性**：每次决策完整 raw response 写 JSONL，便于复盘
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time as dt_time
from typing import Optional, Tuple

from .ai_logger import AIDecisionLogger
from .config import llm as llm_config
from .config import trading
from .models import AIData, AIDecision, ConditionalOrder, Position
from .vendor.llm_client import OpenAICompatibleClient

logger = logging.getLogger(__name__)


class PromptBuilder:
    """系统/用户 Prompt 构造器（SWING + SCALPING 双频）."""

    def __init__(self) -> None:
        self.min_confidence = trading.min_confidence
        self.stop_adjust_cooldown = trading.stop_adjust_cooldown

    def build_swing(self, ctx: "DecisionContext") -> Tuple[str, str]:
        return (
            self._shared_system_prompt() + self._swing_addendum(),
            self._shared_user_prompt(ctx),
        )

    def build_scalping(self, ctx: "DecisionContext") -> Tuple[str, str]:
        return (
            self._shared_system_prompt() + self._scalping_addendum(),
            self._shared_user_prompt(ctx),
        )

    def _shared_system_prompt(self) -> str:
        mc = self.min_confidence
        return f"""你是中证1000指数期货(IM)量化交易执行器。核心使命：**在市场混沌中捕捉具有正向期望的交易机会**，而非追求完美信号。

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
- **平静期（Stress < 1.2）**：止损距离 = **0.8~1.2 倍 5minATR**
- **正常期（1.2 ≤ Stress < 2.0）**：止损距离 = **1.0~1.5 倍 5minATR**
- **高波动期（Stress ≥ 2.0）**：禁止新开仓；已有持仓止损收紧至 **1.0~1.5 倍 5minATR**
- **绝对上限**：止损距离 ≤ 1.5×5minATR

### 止盈设置
- 默认盈亏比 ≥ 1.5:1
- 趋势强烈（ADX>25 且多周期均线完美排列）允许盈亏比上调至 2:1

### 移动止损与调整
- 保本上移门槛：浮盈 > 1.5×15minATR 才能把止损上移至成本价
- 后续移动止损：新止损与现价距离 1.0~1.5×15minATR，严禁贴价
- ADX>30 趋势加速期可放宽移动止损距离

### 动态止盈调整
满足任一通过 adjust_existing.new_take_profit 收窄止盈：
1. 冲高乏力：浮盈>2.0×止损距离 + 长影线/5min均线拐头 → 止盈调至"当前价 ± 0.3×ATR"
2. 多周期分化：30min/60min 与 15min 不同向 → 止盈缩至 1.0×止损距离
3. 高波动转换：Stress 1.2→1.5 突变 → 止盈调至"当前价 ± 0.5×ATR"
4. 时间驱动：距休市<15分钟 + 浮盈≥1.0×止损距离 → 止盈调至"当前价 ± 0.5×ATR"

## ⚠️ 市场应激模式
- Stress < 2.0：正常执行
- Stress ≥ 2.0：暂停新开仓；已有持仓收紧止损
- Stress ≥ 3.0：建议减仓 50%；空仓不得开仓

## 🔄 核心决策哲学（积极交易导向）
- 你的存在价值是寻找交易机会，错失机会同样是风险
- 只有以下三种情形输出 WAIT：
  1. 多空信号剧烈冲突
  2. 行情极端无序（30 分钟振幅<0.3%）
  3. 距休市<5 分钟且当前空仓

## 📊 信号信心评分规则
基础分 0.5；加分项（叠加上限 +0.3）：
**技术面**：均线 3+ 同向 +0.1 / 突破前高低 +0.1 / MACD 持续扩张 +0.1 / 布林带极度收窄 +0.1
**基差**：深度贴水/升水 + 技术面同向 +0.05；单纯基差异常不加不减
**消息面**：明确未消化利好/利空 +0.1
减分：1 分钟波幅>0.5% 黑天鹅 -0.2
最终信心 = min(1.0, 0.5 + 加分 - 减分)
**最终信心 ≥ {mc} 时可开仓；< {mc} 必须 WAIT**

## 💰 仓位规则（按信心）
- 信心 {mc}~0.65：max(1, ceil(max_lots × 12%))
- 信心 0.65~0.75：max(1, ceil(max_lots × 22%))
- 信心 0.75~0.85：max(1, ceil(max_lots × 32%))
- 信心 ≥0.85：max(1, min(ceil(max_lots × 42%), floor(max_lots × 50%)))
- 止损约束：|入场价-止损价| × 200元 × 手数 ≤ 账户动态权益的 2%

## 🧭 数据体系说明
- 技术面 = **指数点位**；持仓信息 = **期货价格**
- ⚠️ 所有 stop_loss / take_profit / conditional_entry 输出**必须是指数点位**

## 📈 条件单使用规则
- 只要 30 分钟内可能被触及的关键价位，就应设条件单
- 最近 30 分钟振幅<0.4% 必须设方向条件单
- trigger_price 与当前价距离可低至 0.15%

## 🔧 adjust_existing
- 仅有持仓时填写
- 最近 {self.stop_adjust_cooldown} 秒内已调过则跳过（输出 null）
"""

    def _swing_addendum(self) -> str:
        mc = self.min_confidence
        return f"""
## 🎯 波段模式特有规则
### 趋势判断
- 强势：5/15/30/60min/日线 4+ 同向 → confidence 0.7+
- 中性：3 同向 → confidence {mc}~0.7
- 弱势：均线缠绕 → WAIT

### 止损
- 正常 Stress<2.0：止损距=1.5×15minATR
- 高波动 Stress≥2.0：禁开仓；持仓收紧 1.0×5minATR

### next_interval_sec
建议 600-1200 秒；趋势明确→短，震荡→长。
"""

    def _scalping_addendum(self) -> str:
        mc = self.min_confidence
        return f"""
## 🎯 短线模式特有规则
1. 只做突破：明确突破前 5 分高/低点
2. 利润目标 15-30 指数点，盈亏比≥1.5:1
3. 止损 8-15 指数点（0.8-1.2 倍 5minATR）
4. 已连续同向>10 点等回调
5. 最近 10 分振幅<0.15% WAIT
6. 不加仓，反向出现先平仓

### 信心调整
- 通用信心 0.7+ 且突破放量 → 立即入场
- 通用信心 {mc}~0.7 突破但量不足 → 条件单
- 通用信心 < {mc} → WAIT

### 仓位
短线统一 1 手，adjust_existing 始终 null。
next_interval_sec 120-600。
"""

    def _shared_user_prompt(self, ctx: "DecisionContext") -> str:
        news_block = (
            f"## 重要新闻\n{ctx.news_text}\n"
            if ctx.news_text and ctx.news_text.strip()
            else ""
        )

        basis = ctx.basis
        basis_text = (
            "## 基差与合约状态\n"
            f"- 中证1000指数: {basis.index_price:.2f}\n"
            f"- IM主力({basis.symbol}): {basis.im_price:.2f}\n"
            f"- 基差: {basis.basis:.2f}点 ({basis.basis_pct:.2f}%)\n"
            f"- 状态: {'贴水' if basis.basis < 0 else '升水'}\n"
            f"- 距到期: {basis.days_to_expiry}天\n"
        )

        if ctx.position.direction:
            pos_text = (
                "## 当前持仓（⚠️ 以下为 IM 期货价格）\n"
                f"- 方向: {ctx.position.direction}\n"
                f"- 手数: {ctx.position.volume}\n"
                f"- 开仓均价（期货）: {ctx.position.entry_price:.2f}\n"
                f"- 当前止损（期货）: {ctx.position.stop_loss:.2f}\n"
                f"- 当前止盈（期货）: {ctx.position.take_profit:.2f}\n"
            )
        else:
            pos_text = "## 当前持仓: 空仓\n"

        fund_text = (
            "## 账户资金与仓位限制\n"
            f"- 动态权益: {ctx.balance:.2f} 元\n"
            f"- 每手保证金约: {ctx.margin_per_lot:.2f} 元\n"
            f"- 最大可开手数（安全线）: {ctx.max_lots} 手\n"
        )

        atr_text = (
            "## 波动率环境\n"
            f"- 5分钟ATR: {ctx.atr.atr_5:.2f} 点\n"
            f"- 15分钟ATR: {ctx.atr.atr_15:.2f} 点\n"
            f"- 60分钟ATR: {ctx.atr.atr_60:.2f} 点\n"
            f"- 当前 Stress Level: {ctx.atr.stress_level:.2f}\n"
        )

        now = ctx.now
        time_text = (
            "## 交易时间\n"
            f"当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        t = now.time()
        minutes_to_break: Optional[int] = None
        if dt_time(9, 30) <= t <= dt_time(11, 29):
            minutes_to_break = (datetime.combine(now.date(), dt_time(11, 30)) - now).seconds // 60
            time_text += f"下一休市: 上午 11:30（还有约 {minutes_to_break} 分钟）\n"
        elif dt_time(13, 0) <= t <= dt_time(14, 59):
            minutes_to_break = (datetime.combine(now.date(), dt_time(15, 0)) - now).seconds // 60
            time_text += f"下一休市: 下午 15:00（还有约 {minutes_to_break} 分钟）\n"
        elif dt_time(21, 0) <= t <= dt_time(22, 59):
            minutes_to_break = (datetime.combine(now.date(), dt_time(23, 0)) - now).seconds // 60
            time_text += f"下一休市: 夜盘 23:00（还有约 {minutes_to_break} 分钟）\n"
        if minutes_to_break is not None and minutes_to_break <= 5:
            time_text += "（⚠️ 即将休市，慎开新仓）\n"

        tech = ctx.tech_text or "技术数据获取中..."
        return (
            f"{news_block}{basis_text}{pos_text}{fund_text}{atr_text}{time_text}"
            f"## 技术面数据\n{tech}\n"
        )


class DecisionContext:
    """打包传给 PromptBuilder 的所有数据."""

    def __init__(
        self,
        position: Position,
        atr: AIData,
        basis,
        balance: float,
        margin_per_lot: float,
        max_lots: int,
        news_text: str,
        tech_text: str,
        now: Optional[datetime] = None,
    ) -> None:
        self.position = position
        self.atr = atr
        self.basis = basis
        self.balance = balance
        self.margin_per_lot = margin_per_lot
        self.max_lots = max_lots
        self.news_text = news_text
        self.tech_text = tech_text
        self.now = now or datetime.now()


class DecisionParser:
    """LLM 输出 → :class:`AIDecision` 反序列化器."""

    def parse(self, raw_response: str, mode: str = "SWING") -> Optional[AIDecision]:
        m = re.search(r"\{.*\}", raw_response, re.DOTALL)
        if not m:
            logger.warning("AI response contains no JSON object.")
            return None
        try:
            data = json.loads(m.group())
        except json.JSONDecodeError as exc:
            logger.warning("AI response JSON parse failed: %s", exc)
            return None

        cond_raw = data.get("conditional_entry")
        cond = None
        if isinstance(cond_raw, dict):
            try:
                cond_raw["action"] = data.get("action", "BUY")
                cond_raw["volume"] = data.get("volume", 1)
                cond = ConditionalOrder.from_dict(cond_raw)
            except Exception as exc:
                logger.warning("Conditional entry parse failed: %s", exc)

        try:
            interval = int(data.get("next_interval_sec") or 0)
        except (TypeError, ValueError):
            interval = 0

        return AIDecision(
            action=data.get("action", "WAIT"),
            volume=int(data.get("volume", 0) or 0),
            stop_loss=float(data.get("stop_loss", 0) or 0),
            take_profit=float(data.get("take_profit", 0) or 0),
            confidence=float(data.get("confidence", 0) or 0),
            reason=data.get("reason", ""),
            conditional_entry=cond,
            adjust_existing=data.get("adjust_existing"),
            next_interval_sec=interval or trading.base_decision_interval,
            mode=mode,
            raw=data,
        )


class AIDecisionEngine:
    """LLM 决策引擎：组合 PromptBuilder + LLM Client + Parser."""

    def __init__(
        self,
        llm_client: Optional[OpenAICompatibleClient] = None,
        prompt_builder: Optional[PromptBuilder] = None,
        decision_logger: Optional[AIDecisionLogger] = None,
    ) -> None:
        self.llm_client = llm_client or OpenAICompatibleClient(
            model=llm_config.model_id or None,
            api_key=llm_config.api_key or None,
            base_url=llm_config.base_url or None,
        )
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.decision_logger = decision_logger or AIDecisionLogger()
        self.parser = DecisionParser()

    def decide(self, ctx: DecisionContext, mode: str) -> Optional[AIDecision]:
        if mode == "SWING":
            sys_p, usr_p = self.prompt_builder.build_swing(ctx)
        else:
            sys_p, usr_p = self.prompt_builder.build_scalping(ctx)
        logger.info("Trigger %s AI decision ...", mode)
        try:
            response = self.llm_client.chat([
                {"role": "system", "content": sys_p},
                {"role": "user", "content": usr_p},
            ])
        except Exception as exc:
            logger.error("LLM call failed: %s", exc)
            return None

        decision = self.parser.parse(response, mode=mode)
        if decision is None:
            return None
        self.decision_logger.save(decision.raw, mode=mode)
        return decision

    def evaluate_overnight(self, sys_prompt: str, user_prompt: str) -> Optional[dict]:
        try:
            response = self.llm_client.chat([
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ])
            return json.loads(response)
        except Exception as exc:
            logger.error("Overnight eval failed: %s", exc)
            return None

    def evaluate_post_open(self, sys_prompt: str, user_prompt: str) -> Optional[dict]:
        try:
            response = self.llm_client.chat([
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ])
            m = re.search(r"\{.*\}", response, re.DOTALL)
            if m:
                return json.loads(m.group())
        except Exception as exc:
            logger.error("Post-open eval failed: %s", exc)
        return None


__all__ = [
    "PromptBuilder",
    "DecisionContext",
    "DecisionParser",
    "AIDecisionEngine",
]
