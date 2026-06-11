# QuantAI · 多源数据驱动的 T+0 LLM 量化交易机器人

> 基于 LLM 决策 + 多周期技术分析 + ATR 自适应风控的中证 1000 股指期货（IM）日内交易系统。

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-pytest-green.svg)](tests/)
[![Code Style](https://img.shields.io/badge/style-ruff-orange.svg)](pyproject.toml)

---

## ✨ 核心特性

- **多源数据融合**：天勤期货实时行情 + 东财指数 K 线 + 金十快讯 + 日韩指数联动 + AKShare 交易日历
- **6 周期技术分析**：5min / 15min / 30min / 60min / 日线 / 周线
- **4 级信心评分**：基础 0.5 + 技术/基差/消息加分 − 黑天鹅减分（详见 `quantai/ai_decision.py`）
- **波动率自适应止损**：Stress Level = 5minATR / 60minATR 动态调整，单笔风险硬约束 ≤ 2%
- **双频自适应决策**：SWING（15min 波段）+ SCALPING（5min 短线）双频路由
- **LLM 决策 + 条件单**：BUY/SELL/WAIT 三态 + PRICE_ABOVE/BELOW 条件单 + 失败兜底
- **工程化分层**：14 模块 SOLID 架构，关键路径 100% 单测覆盖
- **生产级风控**：止损 ratchet（不可放宽）+ 同向加仓硬约束 + 止损后 15min 冷却 + 应急自动复位
- **可观测性**：JSONL 决策日志 + CSV 交易日志 + 实时绩效指标 + 钉钉告警

---

## 🏗️ 架构图

```
┌──────────────────────────────────────────────────────────────────┐
│                     钉钉实时告警 / JSONL 决策日志                  │
└──────────────────────────────────▲───────────────────────────────┘
                                   │
┌──────────────────────────────────┴───────────────────────────────┐
│                       AI 决策层 (LLM)                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐               │
│  │ Prompt 构造  │→ │  LLM 调用    │→ │ JSON 解析    │               │
│  │ SWING+SCALP │  │  (OpenAI兼容)│  │ 信心量化     │               │
│  └─────────────┘  └─────────────┘  └─────────────┘               │
└──────────────────────────────────▲───────────────────────────────┘
                                   │
┌──────────────────────────────────┴───────────────────────────────┐
│                       风控层 (Risk)                               │
│   StopOutCooldown │ StopLossGuard │ AddPositionGuard │ Sizer    │
│   ATR + Stress Level + 单笔 2% + 熔断 + 应急自动复位              │
└──────────────────────────────────▲───────────────────────────────┘
                                   │
┌──────────────────────────────────┴───────────────────────────────┐
│                       执行层 (Order)                              │
│  即时单 / 条件单 / 反向换仓 / 持仓持久化 / 对手价追价重试            │
└──────────────────────────────────▲───────────────────────────────┘
                                   │
┌──────────────────────────────────┴───────────────────────────────┐
│                       数据层 (Market + News)                      │
│  TqSdk 期货行情 │ 东财指数 K 线 │ 金十快讯 │ 日经/KOSPI │ 交易日历   │
└──────────────────────────────────────────────────────────────────┘
```

更多设计细节见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

---

## 🚀 快速开始

### 1. 克隆与安装

```powershell
git clone https://github.com/your-name/QuantAI.git
cd QuantAI

# 创建虚拟环境
python -m venv .venv
.venv\Scripts\Activate.ps1

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置凭证

```powershell
copy .env.example .env
notepad .env   # 填入天勤账户 / LLM API Key / 钉钉 Webhook
```

`.env` 已被 `.gitignore` 排除，**严禁提交真实凭证到 Git**。

### 3. 校验环境

```powershell
python main.py --mode verify
```

输出 `✅ 所有凭证均已配置且就绪` 即可继续。

### 4. 运行

```powershell
# 模拟盘（推荐，对接快期模拟账户 TqKq）
python main.py --mode paper

# 实盘（生产环境，需在 .env 中设 TQ_USE_SIM=False）
python main.py --mode live
```

---

## 📁 目录结构

```
QuantAI/
├── main.py                          # 程序入口
├── requirements.txt
├── pyproject.toml                   # ruff / mypy / pytest 统一配置
├── .env.example
├── .gitignore
├── LICENSE
├── README.md
│
├── quantai/                         # 主包（14 模块 + 编排器）
│   ├── __init__.py
│   ├── config.py                    # ① 配置中心（.env 驱动）
│   ├── models.py                    # ② 数据模型 (frozen dataclass)
│   ├── logger.py                    # ③ 交易事件 CSV 日志
│   ├── notifier.py                  # ④ 钉钉机器人通知
│   ├── market_data.py               # ⑤ 行情/合约/ATR/基差/日历
│   ├── news_manager.py              # ⑥ 金十快讯订阅器
│   ├── jp_indices.py                # ⑦ 日韩联动分析
│   ├── risk_manager.py              # ⑧ 风控（冷却/ratchet/加仓/仓位）
│   ├── ai_decision.py               # ⑨ Prompt + LLM + 解析
│   ├── ai_logger.py                 # ⑩ JSONL 决策日志
│   ├── position_manager.py          # ⑪ 持仓状态 + pickle 持久化
│   ├── order_executor.py            # ⑫ 安全下单 + 对手价追价
│   ├── conditional_orders.py        # ⑬ 条件单触发与调度
│   ├── rollover_manager.py          # ⑭ 合约换月
│   ├── performance.py               # 绩效追踪（胜率/回撤/连胜）
│   ├── execution_pipeline.py        # 决策 → 执行 管道
│   ├── cli.py                       # 命令行 (argparse)
│   ├── system.py                    # IMTradingSystem 编排器
│   └── vendor/                      # 第三方/移植依赖
│       ├── llm_client.py
│       ├── eastmoney_patch.py
│       ├── trade_data_fetcher.py
│       ├── jin10_news_fetcher.py
│       └── backtest_core.py
│
├── tests/                           # 关键路径单测
│   ├── conftest.py
│   ├── test_models.py
│   ├── test_config.py
│   ├── test_risk_manager.py
│   └── test_position_manager.py
│
├── docs/
│   ├── ARCHITECTURE.md              # 系统设计深入
│   ├── INTERVIEW.md                 # STAR 话术 + 高频追问
│   └── RESUME.md                    # 简历项目描述模板
│
└── data/                            # 运行时产物（已 gitignore）
    ├── position_state.pkl
    ├── trade_log.csv
    ├── ai_decisions.jsonl
    ├── performance_metrics.csv
    └── trading.log
```

---

## 🛠️ 技术栈

| 层 | 技术选型 |
|------|----------|
| 期货行情 | [TqSdk](https://www.shinnytech.com/tqsdk/) (天勤 SDK) |
| 指数 / 财经数据 | [efinance](https://github.com/Micro-sheep/efinance) + [AKShare](https://github.com/akfamily/akshare) |
| 美/日/韩指数 | [yfinance](https://github.com/ranaroussi/yfinance) |
| LLM | OpenAI Compatible（GPT-4o-mini / DeepSeek / 通义 / 火山方舟 / 豆包均可） |
| 持久化 | pickle（持仓状态）+ JSONL（决策日志）+ CSV（交易日志） |
| 通知 | 钉钉自定义机器人 Webhook（HMAC-SHA256 加签） |
| 测试 | pytest + pytest-mock + pytest-cov |
| 代码质量 | ruff + mypy |

---

## 📊 关键设计亮点

### 1. 双频自适应决策

`analyze_market_state()` 基于 `5minATR / 15minATR` 比值动态切换：

- `比值 > 1.3` → **SCALPING** 模式：5min 高频突破策略
- 其他 → **SWING** 模式：15min 波段策略
- `Stress Level ≥ 2.0 且空仓` → **IDLE** 暂停开仓

### 2. 止损 ratchet（核心风控创新）

止损只允许"朝保护利润方向移动"：
- 多头：新止损 < 旧止损 = 放宽（风险更大）→ 需要 `confidence ≥ 0.75` 才放行
- 任何方向的"收紧止损"无条件放行

防止 LLM 在浮亏时被诱导放宽止损导致更大亏损。

### 3. 同向加仓硬约束

加仓必须**同时**满足：

| 条件 | 阈值 |
|------|------|
| 信心 | ≥ 0.85 |
| 仓位上限 | 已持仓 < 3 手 |
| 价格错开 | ≥ 1.0 × 15minATR |
| 浮亏 | > -1.5% |

任一不满足 → 拒绝并钉钉告警，防止情绪化加仓套牢。

### 4. 止损后冷却

止损平仓后，**同向**禁开 15 分钟（反向不限）。
防止 LLM 在止损刚触发后立刻"复仇式"再开同向单。

### 5. 多周期 ATR + Stress Level

`Stress Level = 5minATR / 60minATR`：

- `< 2.0`：正常交易
- `≥ 2.0`：禁止新开仓，已有持仓收紧止损至 1.0 × 5minATR
- `≥ 3.0`：建议立即减仓 50%

灵感来自医学应激指标——短周期波动率对长周期基准的偏离。

---

## 🧪 运行测试

```powershell
# 全部测试
pytest

# 仅风控模块
pytest tests/test_risk_manager.py -v

# 带覆盖率
pytest --cov=quantai --cov-report=html
```

---

## 🛡️ 安全实践

本项目所有凭证（账户密码 / API Key / Webhook）通过 `.env` 注入，**绝不硬编码**：

- `config.py` 启动时调用 `ensure_credentials()` 显式校验，缺失直接拒绝启动
- `.gitignore` 显式排除 `.env` / `*.pkl` / `trade_log.csv` / `trading.log` 等所有运行时产物
- 钉钉机器人启用 HMAC-SHA256 加签

---

## 📝 演进路线

| 版本 | 内容 | 状态 |
|------|------|------|
| V1 | 单文件 770 行，完成"数据→决策→执行"闭环 | ✅ 已完成（历史存档） |
| V2 | 14 模块 SOLID 重构 + 云端校验 + JSONL 日志 + 波动率自适应 | ✅ 当前版本 |
| V3 | LangGraph Multi-Agent 升级（Signal / Risk / Executor 三 Agent 协同） | 🔄 规划中 |
| V4 | RLHF 微调专用决策模型 + 实盘 A/B 测试框架 | ⏳ 远期 |

---

## 📚 文档

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 系统设计深入：依赖图、状态机、关键算法
- [`docs/INTERVIEW.md`](docs/INTERVIEW.md) — 面试话术 STAR 三档 + 10 个高频追问
- [`docs/RESUME.md`](docs/RESUME.md) — 简历项目描述模板（80-120 字）

---

## ⚠️ 免责声明

本项目仅供学习交流。期货交易存在杠杆风险，可能在短时间内造成巨额亏损。
**严禁将本项目用作投资建议**。使用者需自行承担一切实盘后果，作者不对任何损失负责。

---

## 📄 License

MIT License — 详见 [LICENSE](LICENSE)。
