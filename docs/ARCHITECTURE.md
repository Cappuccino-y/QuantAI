# QuantAI · 系统架构设计

## 1. 设计目标

| 目标 | 实现策略 |
|------|----------|
| **可解释性** | LLM 输出严格 JSON + 信心分量化 + JSONL 决策日志全量回溯 |
| **稳健性** | 14 模块 SOLID 分层 + 关键路径单测 + 失败兜底 |
| **生产级风控** | 止损 ratchet / 加仓硬约束 / 止损后冷却 / 应急模式 |
| **可观测性** | 钉钉实时告警 + CSV 交易日志 + JSONL 决策日志 + 绩效指标 |
| **可演进性** | 依赖注入 / 接口分层 / 配置外置 / V3 可平滑升级到 LangGraph |

---

## 2. 模块依赖图

```
                        ┌──────────────┐
                        │   config.py  │ (foundation, no deps)
                        └──────┬───────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
   ┌────▼─────┐         ┌──────▼──────┐         ┌────▼─────┐
   │ models.py│         │  logger.py  │         │notifier.py│
   └────┬─────┘         └──────┬──────┘         └────┬─────┘
        │                      │                      │
        └──────────┬───────────┴──────────┬──────────┘
                   │                      │
            ┌──────▼──────┐         ┌─────▼───────┐
            │performance  │         │ news_manager│
            │   .py       │         │    .py      │
            └─────────────┘         └─────┬───────┘
                                          │
                              ┌───────────▼───────────┐
                              │     market_data.py    │
                              │ (Calendar + ATR +     │
                              │  ContractResolver)    │
                              └───────────┬───────────┘
                                          │
        ┌──────────────────┬──────────────┴─────────────┐
        │                  │                            │
 ┌──────▼──────┐    ┌──────▼───────┐           ┌────────▼────────┐
 │risk_manager │    │position_     │           │ jp_indices.py   │
 │   .py       │    │manager.py    │           │ (Lunch breakout)│
 └──────┬──────┘    └──────┬───────┘           └─────────────────┘
        │                  │
        │           ┌──────▼──────┐
        │           │order_       │
        │           │executor.py  │
        │           └──────┬──────┘
        │                  │
        │       ┌──────────┼──────────┐
        │       │          │          │
        │  ┌────▼────┐ ┌───▼────┐ ┌──▼────────┐
        │  │ai_      │ │condit. │ │rollover_  │
        │  │decision │ │_orders │ │manager.py │
        │  └─────────┘ └────────┘ └───────────┘
        │
        └──────────────────────────────────────────┐
                                                   │
                                          ┌────────▼─────────┐
                                          │   system.py      │
                                          │  (IMTradingSystem│
                                          │   orchestrator)  │
                                          └────────┬─────────┘
                                                   │
                                          ┌────────▼─────────┐
                                          │ execution_       │
                                          │ pipeline.py      │
                                          └──────────────────┘
```

依赖方向严格自上而下，**禁止反向依赖**。`system.py` 是唯一的"上帝对象"，但其职责仅限装配与调度。

---

## 3. 核心数据流

### 3.1 主循环 tick 流程

```
        ┌─────────────────────────┐
        │  TqApi.wait_update()    │ <—— 行情推送
        └────────────┬────────────┘
                     │
        ┌────────────▼────────────┐
   是 ─ │  emergency_mode 激活?    │
        └────────────┬────────────┘
              否     │
        ┌────────────▼────────────┐
        │ ConditionalOrderManager │ —— 检查所有未触发条件单
        │      .tick()            │
        └────────────┬────────────┘
                     │
        ┌────────────▼────────────┐
        │  check_stop_profit()    │ —— 价格触及 SL/TP？立即平仓
        └────────────┬────────────┘
                     │
        ┌────────────▼────────────┐
        │  is_near_close()?       │ —— 临近休市跳过 AI
        └────────────┬────────────┘
                     │否
        ┌────────────▼────────────┐
        │  analyze_market_state() │ —— SWING / SCALPING / IDLE
        └────────────┬────────────┘
                     │
        ┌────────────▼────────────┐
        │  execute_ai_cycle()     │ —— 调 LLM
        │  - refresh tech data    │
        │  - calc ATR             │
        │  - build prompt         │
        │  - parse decision       │
        │  - dispatch to pipeline │
        └────────────┬────────────┘
                     │
        ┌────────────▼────────────┐
        │ execute_decision()      │ —— 风控校验 + 下单
        └────────────┬────────────┘
                     │
        ┌────────────▼────────────┐
        │ rollover_if_needed()    │ —— 到期前 2 天自动换月
        └─────────────────────────┘
```

### 3.2 execute_decision 风控决策序列

`quantai.execution_pipeline.execute_decision` 严格按以下顺序执行，
任何一步失败立即返回，杜绝"已下单又被风控否决"的不一致：

1. **止损冷却期检查**（同向 15min 内禁开）
2. **adjust_existing 止损 ratchet**（放宽止损需 confidence ≥ 0.75）
3. **新条件单设置 / 清除旧条件单**
4. `WAIT` 或 `confidence < min_confidence` → 直接返回
5. **同向加仓校验链**（信心 / 上限 / 价差 / 浮亏）
6. **反向先平仓**
7. **新开仓**
   - 检查 SL/TP 有效性
   - 最小止损距离自动放宽
   - 单笔风险占比 ≤ 2% 削减手数

---

## 4. 关键算法

### 4.1 信心评分（4 级量化）

```
base = 0.5
score = base
      + max(0, +技术加分)        # 上限 +0.3
      + 基差加分                 # +0.05 (仅技术面同向时)
      + 消息加分                 # +0.1
      - 黑天鹅扣分               # -0.2
score = min(1.0, score)

if score >= 0.85: tier = "高度确信" → 32%~42% 资金
if score >= 0.75: tier = "中度确信" → 22%~32% 资金
if score >= 0.65: tier = "轻度确信" → 12%~22% 资金
if score >= 0.55: tier = "试错"     → 12% 资金
else:             WAIT
```

详见 [`quantai/risk_manager.py::PositionSizer.lots_for_confidence`](../quantai/risk_manager.py)。

### 4.2 ATR 计算

```python
TR = max(
    high - low,
    abs(high - prev_close),
    abs(low - prev_close),
)
ATR = TR.rolling(window=14).mean()
```

并行计算 5min / 15min / 60min 三周期；
`Stress Level = ATR_5m / ATR_60m` 反映短期相对长期的波动率突变。

### 4.3 止损距离选择

```
if Stress < 1.2:    dist = [0.8, 1.2] × 5minATR
elif Stress < 2.0:  dist = [1.0, 1.5] × 5minATR
else:              禁开仓；已持仓收紧至 1.0×5minATR

绝对上限：dist ≤ 1.5 × 5minATR
```

### 4.4 单笔风险约束

```python
max_loss = balance × 0.02
loss_per_lot = abs(entry - stop_loss) × 200
max_lots_by_risk = floor(max_loss / loss_per_lot)
final_volume = min(volume_from_confidence, max_lots_by_risk)
```

---

## 5. 状态机

### 5.1 持仓状态

```
        ┌──────────┐
        │  EMPTY   │  ← initial / after CLOSE
        └────┬─────┘
             │ OPEN
        ┌────▼─────┐
        │  LONG    │  ←──── reverse_open ────┐
        └────┬─────┘                          │
             │                                │
             │ ADD                            │
             ▼                                │
        ┌──────────┐                          │
        │ LONG (n) │                          │
        └────┬─────┘                          │
             │                                │
       SL/TP │  reverse_open                  │
             ▼                                │
        ┌──────────┐                          │
        │  SHORT   │  ←───────────────────────┘
        └──────────┘
```

### 5.2 emergency_mode

```
        ┌──────────┐
        │  NORMAL  │
        └────┬─────┘
             │ close_position 失败
             ▼
        ┌──────────┐
        │EMERGENCY │  ── 每 3 秒重试平仓 ──┐
        └────┬─────┘                       │
             │                              │
             │ 平仓成功 OR (空仓 + 30min)   │
             ▼                              │
        ┌──────────┐                       │
        │  NORMAL  │  ←────────────────────┘
        └──────────┘
```

`should_auto_reset` 触发条件：`emergency_mode` 激活 ≥ 30min **且** 当前空仓。

---

## 6. 持久化设计

| 文件 | 格式 | 写入时机 | 用途 |
|------|------|----------|------|
| `data/position_state.pkl` | pickle | 每次持仓/条件单变更 | 崩溃恢复 |
| `data/trade_log.csv` | CSV | 每笔订单事件 | 离线分析 |
| `data/ai_decisions.jsonl` | JSONL | 每次 LLM 决策 | Prompt A/B 测试 |
| `data/performance_metrics.csv` | CSV | 每笔平仓 | 绩效追踪 |
| `data/trading.log` | TimedRotating | 每条日志 | 7 天滚动 |

启动时 `PositionManager.reconcile_with_broker()` 用券商真实持仓覆盖本地状态，避免漂移。

---

## 7. 并发模型

- **主线程**：行情 tick 循环（TqSdk 单线程模型）
- **后台线程**：金十快讯定时拉取（5 min 周期）
- **锁机制**：
  - `PositionManager._lock` — RLock 保护持仓 / 条件单读写
  - `TradeLogger._lock` — Lock 保护 CSV append
  - `NewsManager._lock` — Lock 保护 news_cache 读写
  - `OrderExecutor._orders_lock` — Lock 保护活跃订单列表

---

## 8. 演进路径

### V3 · LangGraph Multi-Agent

把单 LLM 拆为 3 个独立 Agent：

```
                    ┌─────────────────┐
                    │   Supervisor    │
                    │  (Router LLM)   │
                    └────┬─────┬──────┘
                         │     │
            ┌────────────┘     └──────────────┐
            ▼                                  ▼
   ┌────────────────┐               ┌──────────────────┐
   │ Signal Agent   │               │ Risk Agent       │
   │ (GPT-4o-mini)  │ ──signals──>  │ (DeepSeek, 便宜) │
   │ 6 周期并行分析  │               │ ATR/仓位/熔断    │
   └────────────────┘               └────────┬─────────┘
                                              │
                                       ┌──────▼───────┐
                                       │Executor Agent│
                                       │下单/持久化     │
                                       └──────────────┘
                                              │
                                       ┌──────▼───────┐
                                       │ Checkpointer │
                                       │  (SQLite)    │
                                       └──────────────┘
```

复用本项目所有 `quantai/*` 模块作为 Agent 工具集，
仅替换 `ai_decision.py` + `system.py` 为 LangGraph 实现，
其余模块（risk / order / position）保持不变。

---

## 9. 测试矩阵

| 模块 | 覆盖率 | 关键测试 |
|------|--------|----------|
| `models.py` | 100% | dataclass roundtrip, copy 独立性 |
| `config.py` | 95%+ | 缺凭证拒绝启动, 路径正确性 |
| `risk_manager.py` | 90%+ | 冷却 / ratchet / 加仓 5 重门槛 / Sizer / Emergency |
| `position_manager.py` | 90%+ | pickle roundtrip, 云端漂移修正, SL/TP 清零 |
| `order_executor.py` | TODO | 防呆 / 超时 / 重试 (依赖 TqApi mock) |
| `ai_decision.py` | TODO | Prompt 模板 / JSON 解析 / 异常兜底 |

运行：`pytest --cov=quantai`
