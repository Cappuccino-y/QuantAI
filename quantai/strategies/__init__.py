"""strategies — 指标决策策略子包（design.md §三 核心新增）。

分层规则（ARCHITECTURE.md）: 纯决策层 — 输入 context 输出结构化信号/Action 建议，
不 import order_executor（平仓等动作由编排层执行，design.md minor3）。

模块与真源映射（design.md §4.2）:
- indicators      纯函数指标库（calc_atr ← 真源 L473 嵌套闭包）
- market_context  ATR 汇总 / OI 四态 / 动态位阶（真源 L459 / L516 / L1521）
- left_side       左侧信号（真源 L1608–2049，计算/渲染/告警三段）
- entry_filters   入场过滤器链（真源 L4422–4693，6 个 → FilterResult）
- exemptions      反转豁免链（真源 L4707–4943，4 个 → FilterResult）
- session_plays   时段策略（真源 L3520–3683 + L3809–4421，9 个 → 纯决策 + SessionAction）
"""
