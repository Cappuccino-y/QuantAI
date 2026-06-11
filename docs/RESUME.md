# QuantAI · 简历项目描述模板

> 直接复制到简历项目栏；3 行 / 5 行 / 短描述 三档可选。

---

## 1. 推荐模板（5 行，80~120 字，最完整）

```markdown
**QuantAI · 多源数据驱动的 T+0 量化交易机器人**
*Python · LLM · TqSdk · 自研项目 · 2025.09 – 2026.06 · [GitHub]*

- 独立设计并实现基于 LLM 的 IM 股指期货日内交易系统，对接天勤期货 / 东财 / 金十快讯 / 日韩指数 4 数据源，
  融合 6 周期（5min~周线）技术面 + 实时新闻 + 基差监控，完整跑通"数据采集→LLM 决策→
  条件单/即时单→风控止损→状态持久化"全链路
- 设计 4 级信心评分算法（基础 0.5 + 技术/基差/消息加分 − 黑天鹅减分）与波动率自适应止损
  框架（Stress Level = 5minATR/60minATR 动态调整），单笔风险硬约束 ≤ 2% 账户权益
- 14 模块 SOLID 重构（770 行单文件 → market_data / risk / ai_decision / order_executor /
  position_manager / conditional_orders 等分层），关键路径单测覆盖；引入云端-本地持仓
  一致性校验、止损 ratchet、JSONL 决策日志、钉钉实时告警
- 系统持续模拟盘运行 X 月，迭代 X 版 Prompt，零重大故障；附回测框架支持离线策略验证
```

---

## 2. 精简版（3 行，适合空间紧张）

```markdown
**QuantAI · LLM 驱动的中证 1000 股指期货 T+0 交易机器人**
*Python · TqSdk · OpenAI Compatible · 自研 · 2025.09 –*

- 4 数据源 + 6 周期技术面 + 4 级信心评分 + ATR 波动率自适应止损（单笔风险 ≤ 2%）
- 14 模块 SOLID 分层（770 行单文件 → market_data / risk / ai_decision / executor），关键路径单测覆盖
- 实现止损 ratchet、加仓 4 重硬约束、止损后 15min 冷却、应急平仓自动复位等生产级风控
```

---

## 3. 一句话版（用于 LinkedIn 简介或副项目列表）

> **QuantAI** · LLM 驱动的中证 1000 IM 股指期货 T+0 自动交易系统，
> 4 数据源 + 6 周期 + 4 级信心评分 + ATR 自适应风控 + 14 模块 SOLID 架构。

---

## 4. 在简历中的项目排序建议

```
1. [主体经验项目]  (2 年, 强相关)
2. [其它主体项目]   (1 年, 多人协作)
3. QuantAI · T+0 交易机器人  ← 杀手锏（独立 + 端到端 + 金融场景）
4. [Agent 项目]    (考点覆盖)
5. [RAG 项目]      (考点覆盖)
6. [其它技术 Demo] (考点覆盖)
```

**T+0 排序理由**：
- 前 2 个是协作工程 → 体现"工程化 + 团队"
- T+0 是单人实战 → 体现"独立 + 端到端 + 学习能力"
- 后续 3 个是技术 Demo → 体现"考点覆盖"
- 形成"主体 + 杀手锏 + 考点全覆盖"三层结构

---

## 5. 关键原则

### ✅ 必须有的元素

- **具体数字**：4 数据源、6 周期、14 模块、2% 风险约束、3 层兜底
- **风控优先**：金融机构最看重；"风控" / "硬约束" / "ratchet" / "冷却"
- **工程化**：SOLID、单测、持久化、可观测性
- **真实问题**：云端-本地不一致、LLM 幻觉、Prompt 解析失败 → 都给了解决方案

### ❌ 绝对不要写

- "**年化 X%**" / "**收益率 X%**" — 合规雷区
- "**打败市场 X%**" — 容易被挑战
- "**绝对盈利**" — 误导性陈述
- "**实盘运行**"（除非真的合规运行了），建议写"模拟盘 + 小仓位实盘验证"
- 完整 Prompt 内容 — 知识产权问题，可贴片段，不贴 200 行原文

---

## 6. 适配不同公司的微调

### 蚂蚁 / 腾讯金融 / 招行 AI Lab

强调：
- 多层风控体系（合规友好）
- 单笔风险硬约束
- 云端-本地一致性
- 钉钉告警 + 决策日志可追溯

调高：金融场景词汇密度（基差、应激、ATR、Profit Factor）

### 字节 / 美团 / 快手 AI

强调：
- LLM 工程化（Prompt 版本管理、JSON 输出约束、JSONL 回溯）
- 多 Agent 演进路线（V3 LangGraph）
- 高并发场景（行情 tick + 后台新闻线程）

调高：工程化词汇密度（SOLID、单测、可观测性、依赖注入）

### 小米 / OPPO / 荣耀 AI

强调：
- 跨界能力（Android 底层经验 + AI Agent）
- 端到端独立交付
- 资源约束下的工程取舍（LLM 成本 1-2 美元/天）

调高：差异化叙事（"既懂 Android 又懂金融 Agent 的稀缺组合"）

### 字节豆包 / DeepSeek / 智谱

强调：
- 复杂 Prompt 设计（200 行 system prompt + JSON schema 约束）
- LLM 决策可解释性（信心分量化 + reason 字段）
- 模型路由（V3 不同 Agent 用不同模型，成本/质量平衡）

---

## 7. 项目链接配置

GitHub 仓库设置：

- **Description**：`LLM-powered T+0 trading bot for CSI 1000 index futures (IM) with multi-source data and ATR-adaptive risk control`
- **Topics**：`agent`, `llm`, `quant`, `trading`, `futures`, `im`, `tqsdk`, `akshare`, `risk-management`, `prompt-engineering`
- **README badges**：Python 版本 / License / Tests / Code Style
- **置顶 commit**：使用规范前缀 `feat: add ATR-based risk manager` / `refactor: split into 14 SOLID modules`

---

## 8. 自检清单

- [ ] 描述里有具体数字（**至少 4 个**）
- [ ] 不包含任何具体盈利数字
- [ ] 强调风控（**至少 2 处**）
- [ ] 强调工程化（**至少 1 处**：SOLID / 单测 / 持久化）
- [ ] GitHub 仓库 README 完整，含架构图 + 快速开始
- [ ] `.env` 确认在 `.gitignore` 中，无真实凭证泄露
- [ ] 至少 2 次有意义的 commit（不是 "init commit"）

---

**最后一句话**：T+0 项目不是简历的全部，但它能让你在面试 5 分钟内
**清晰展示"独立完成端到端 + 真实业务理解 + 工程化能力 + LLM 实战"**——这是 2026 校招最稀缺的组合。
