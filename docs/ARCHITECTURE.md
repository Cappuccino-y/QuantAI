# QuantAI 架构（骨架期 v0.1）

> 逻辑真源: `D:/PythonProject/MainToy/trade/autotrade_fix.py`（5659 行，96 def）
> 设计文档: 工作区 `design.md`（复审通过版）
> 本文件随实施阶段滚动更新。

## 依赖方向（自上而下，禁止反向/横向）

```
main.py（入口）
  └─ quantai/
       config ──→ models
                    │
       ┌────────────┼──────────────────────────────┐
       ▼            ▼                              ▼
   logger       notifier                    performance
       │            │                              │
       │            │                          news_manager ──→ vendor(jin10_news_fetcher)
       │            └────────────────────────────────────────→ vendor(notifycation)
       ▼
   market_data（阶段2）──→ vendor(trade_data_fetcher)
       ▼
   strategies/（阶段3: indicators/market_context/left_side/entry_filters/exemptions/session_plays）
       ▼
   risk_manager / position_manager / order_executor / conditional_orders / rollover_manager（阶段4）
       ▼
   ai_decision ──→ vendor(llm_client)
       ▼
   execution_pipeline（阶段4）
       ▼
   system（阶段5: 唯一装配点 + run 主循环）
```

分层规则（对照 QuantAI 参考架构）:
1. **基础设施层**: config / models / logger / notifier / performance / news_manager — 零业务逻辑
2. **数据层**: market_data / jp_indices — 行情与日历
3. **策略层**: strategies 子包 — **纯决策**，输入 context 输出结构化信号/Action 建议，
   不 import order_executor（平仓等动作由编排层执行，design.md minor3）
4. **业务层**: risk / position / order / conditional_orders / rollover
5. **编排层**: execution_pipeline / system — 依赖注入、无全局状态

## vendor 适配层（逐字节保真，哈希校验）

| 文件 | 来源 | 说明 |
|---|---|---|
| trade_data_fetcher.py | MainToy/trade/ | 1052 行新版（QuantAI 旧版仅 591 行） |
| jin10_news_fetcher.py | MainToy/trade/ | 371 行新版 |
| eastmoney_patch.py | MainToy/trade/ | 两版相同 |
| backtest_core.py | MainToy/trade/ | 两版相同；验收期稀有路径历史回放用 |
| akshare_multi_period.py | MainToy/trade/ | trade_data_fetcher 硬依赖 |
| llm_client.py | MainToy/tools/ | LLM 客户端 |
| notifycation.py | MainToy/tools/ | 钉钉传输层（notifier 默认 sender） |

vendor 文件禁止修改；升级 = 从 MainToy 重新拷贝覆盖。

## 实施进度（design.md §5.2 六阶段）

- [x] **阶段 1 骨架期**: 包结构 + config/models/logger/notifier/performance/news_manager + vendor ✅
- [x] **阶段 2 数据层**: market_data（ContractResolver/TradingCalendar/AccountView/MarketDataService）+ jp_indices（JPIndicesService + LunchContext）+ 单测 ✅
- [x] **阶段 3 策略层**: strategies 子包全量 — indicators.calc_atr + market_context（ATR/OI/动态位阶）✅（2026-08-29）；left_side + entry_filters + exemptions + session_plays ✅（2026-08-29 第二批）
- [x] **阶段 4 业务层**: risk_manager / position_manager / order_executor / conditional_orders / rollover_manager + execution_pipeline（含 pkl plain-dict 守护测试）✅（2026-08-29）
- [ ] **阶段 5 编排期**: system 装配 + run 主循环 + main.py --dry-run + ai_decision 模块（9 方法）落位接线
- [ ] **阶段 6 验收期**: dry_run 影子 ≥3 交易日 + 稀有路径历史回放对拍

> **阶段 4 备忘**（design.md §5.2）: 补 pkl plain-dict 守护测试——PositionManager 加载时校验
> pkl 内容为 plain dict，并验证旧版写出的 pkl 能被新 PositionManager 读取。
>
> **阶段 5 备忘（阶段 2 验收 minor2）**: 真源 `__init__` L405 启动即调 `_update_index_price()`、
> L418 调 `_refresh_tech_data()`（避免启动初期 index_price=0 导致基差异常）——system.py 装配时
> 必须显式调用 `mds.update_index_price()` + `mds.refresh_tech_data()`（与阶段 4 pkl 备忘同格式）。
>
> **阶段 5 备忘（阶段 3 二批追加）**:
> 1. `EntryFilters(atr5_fn=...)` 必须接线 `lambda: mcs.atr_5`（未注入视为 0 = ATR 未就绪路径，
>    入场确认过滤器会静默放行）
> 2. `LeftSideStrategy` 装配接线: `index_price_fn=lambda: mds.index_price`、
>    `yesterday_close_fn=mds.get_yesterday_index_close`、`dynamic_levels_fn=mcs.compute_dynamic_levels`、
>    `notifier=钉钉实例`
> 3. `SessionPlaysService` 装配接线: `ai_chat_fn=llm_client.chat 封装`、`logger=TradeLogger`、
>    `news_items_fn=news_manager.get_news`、`lunch_context` 与 jp 侧共享同一实例；
>    run 主循环各节点传入 `pm.position`（真源读全局 current_position）
> 4. **14:00 强平激活决策**: 真源 `lunch_breakout_today['force_close_deadline']` 在活代码中从未
>    赋值（唯一赋值点 L4287 在 return 后不可达块内）→ 真源 14:00 强平实际永不触发。本版保真保留
>    该行为；编排层若要激活，须在 12:50 条件单成交后显式设置 deadline（建议与用户确认后启用）
> 5. **编排层执行职责（SessionPlays 建议的消费方，防装配漏接）**: session_plays 是纯决策层，
>    以下动作必须由编排层（system.run 节点 / execution_pipeline）完成——
>    a. `SessionAction(CLOSE_POSITION)`（盘前跳空主动平仓）: 按 close_direction/volume 调
>       `order_executor.execute_order_safe(offset='CLOSE')`，成交后清空 `pm.position` +
>       `pm.save_position_state()` 持久化 + 发送 ✅/❌ 成交结果通知（真源 L3940–3974 语义）
>    b. `SessionAction(FORCE_CLOSE)`（14:00 强平，当前死路径）: 同上调 close_position
>    c. `post_open_analysis` 返回的 ADJUST_STOP/ADJUST_PROFIT 建议: 写入 `pm.position` 的
>       stop_loss/take_profit + `pm.save_position_state()` 持久化 + TradeLogger 落 ADJUST_* 事件
>    d. `lunch_breakout_check` 返回的条件单 dict: 写入 `pm.conditional_order` +
>       `pm.save_position_state()`（真源 L4238–4250 写全局 conditional_order 的等价物）
> 6. **真源 basis 单位放大 quirk（阶段 3 验收 minor1 备忘）**: `_check_overnight_gap_risk`
>    估算期货开盘价公式 `expected_futures_open = index_price + basis / 100 * index_price`
>    （真源 L3919）中，`basis` 来自 `get_basis_info()['basis']` 是**点值**（im_price - index_price，
>    典型 -16 ~ -60 点），却被当作**百分比**代入（除以 100 再乘指数）→ 放大倍数 = index_price/100
>    ≈ 40-50 倍。后果: LONG + 跳空冲突时估算开盘价被大幅压低 → 预期亏损恒 ≥ 3000 元 →
>    平仓阈值触发面被放大（几乎必触发）；SHORT + 跳空冲突时估算开盘价远低于入场价 →
>    预期亏损为负 → 主动平仓永不触发。本版逐行保真该公式（session_plays.check_overnight_gap_risk），
>    修复方案（改为 `index_price + basis` 点值直加）留编排期与用户确认后统一处理

## 骨架期关键决策记录

1. **账密入 .env**（真源 L99–100 硬编码修复），模板见 `.env.example`
2. **状态文件统一 DATA_DIR**（env `QUANTAI_DATA_DIR` 或 `<项目根>/data`），文件名与格式与真源一致，
   接管时按 design.md §5.4 拷贝 pkl/json
3. **notifier 类化**: 原版 monkey-patch 全局 `notifycation.send_dingtalk_message` →
   `DingTalkNotifier` 类，sender 可注入（dry_run/测试注入假 sender），限频/去重/关键消息分级行为逐条对齐
4. **news_manager 依赖注入**: `prev_trading_day_fn` 注入替代对 market_data 的直接依赖（阶段 2 接线）
5. **ConditionalOrder 模型**: AI 路径（L2245–2313）∪ 午盘路径（L4238–4250）字段并集，
   from_dict 容忍旧 pkl 缺键

## 阶段 2 关键决策记录（数据层）

1. **类划分**: 真源 12 个 market_data 方法按职责归四类——`ContractResolver`（主力合约识别）、
   `TradingCalendar`（交易日/时段/临近休市）、`AccountView`（动态权益）、`MarketDataService`
   （指数价/技术面/基差/换算/昨收）；`format_code` 嵌套闭包提为模块级纯函数（design.md 既定）
2. **数据状态归属**: 真源上帝类的 `symbol`/`im_quote`/`index_price`/`tech_data_text` 归
   `MarketDataService` 持有；阶段 4 换月时由 rollover_manager 直接更新该服务字段
   （替代真源 L3470–3503 / L5456–5459 的散点赋值）
3. **可测性注入（生产行为不变）**: `ContractResolver(now_fn)` 控制月份边界（12 月跨年候选）、
   `TradingCalendar(now_fn)` 控制时刻；默认 `datetime.now` 与真源一致
4. **news_manager 接线就绪**: `TradingCalendar.get_previous_trading_day_15` 签名与
   `prev_trading_day_fn` 注入契约一致（单测已验证回补起点正确），实际装配在阶段 5 system.py
5. **LunchContext 默认键集**: 真源 L438–447 的 7 个数据键落在
   `jp_indices.create_default_lunch_context()`；`update_time` 由模型字段承载（阶段 1 已验收的结构差异）
6. **ATR/OI/动态位阶不在本阶段**: 按 design.md §4.2 映射属 strategies/market_context.py（阶段 3），
   阶段 2 严格不越界
7. **自检扩展**: main.py --check 从 6 步扩到 7 步，新增数据层组件可构造性验证（假 api/fetcher，不连网）

## 阶段 3 关键决策记录（策略层·market_context + indicators）

1. **类划分**: 真源 3 个方法归 `MarketContextService`——`calculate_fut_atr`（L459–513）、
   `compute_oi_state`（L516–552）、`compute_dynamic_levels`（L1521–1606）；真源上帝类的
   5 个指标状态字段（atr_5/atr_15/atr_60/stress_level/oi_state_text，__init__ L399/L421–424）
   归该服务持有（与阶段 2 MarketDataService 持有数据状态同模式）
2. **calc_atr 提纯**: 真源 L473 嵌套闭包提为 `strategies/indicators.calc_atr` 模块级纯函数
   （design.md §4.2 既定）；indicators 其余纯函数（rsi/ema/vwap 等）随阶段 4
   left_side/filters/exemptions 迁移落位，本阶段不预建
3. **symbol 双服务同步备忘（阶段 4）**: 真源单一 `self.symbol` 换月时赋值（L5456–5459）→
   rollover_manager 须同时更新 `MarketDataService.symbol` 与 `MarketContextService.symbol`
4. **direction 参数保真**: `_compute_dynamic_levels` 签名含 `direction` 但真源函数体未使用
   （L1521），签名原样保留，不"顺手清理"
5. **异常路径不重置状态**: `calculate_fut_atr` 外层异常仅记日志（"使用默认值"文案），
   5 个状态字段保持上次值（真源 L512–513 行为；单测锁定）
6. **n<20 布林 quirk 保真**: `bb_mid = sum(closes[-20:]) / 20` 在 n<20 时仍除以 20
   （真源 L1542），生产 200 根 K 线不触发；单测锁定防漂移
7. **自检扩展**: main.py --check 从 7 步扩到 8 步，新增 strategies 组件可构造性 +
   compute_dynamic_levels 纯函数冒烟（n<5 兜底路径）

## 阶段 3 第二批关键决策记录（left_side / entry_filters / exemptions / session_plays）

1. **left_side 三段拆分**（design.md 设计要点 1）: `_compute_left_side_signals`（L1608–2049，
   442 行）拆为 `compute_signals()`（计算 → dict + LeftSideSignal 结构化信号）/
   `render_regime`+`render_signals`（渲染 → prompt 文本逐字保真，阶段 4 可整体迁往
   ai_decision.PromptBuilder）/ `dispatch_alerts()`（告警 → 注入 notifier，5min 节流 +
   SL/TP 载荷手算路径保真）；`compute_left_side_signals()` 组合入口返回值与真源一致
2. **FilterResult 替代二元组**: 过滤器/豁免 10 个方法真源返回 `Tuple[bool, str]` →
   `models.FilterResult(allowed, reason, filter_name)`（design.md 设计要点 1 既定结构化输出）；
   异常语义保真——豁免类异常 → allowed=False（"不豁免"），vwap_alignment 异常 → allowed=True
3. **session_plays 纯决策化**（design.md §5.4）: 真源直接下单/清仓/写全局的部分改为返回值——
   盘前跳空平仓 → `SessionAction(CLOSE_POSITION)`、14:00 强平 → bool、12:50 条件单 →
   返回 dict（键集 = 真源 L4238–4250）；`current_position` 全局 → 方法参数 position；
   `AI_CLIENT` 全局 → `ai_chat_fn` 注入；`save_position_state` → 编排层持久化
4. **真源 quirk 保真（14:00 强平死路径）**: `force_close_deadline` 唯一赋值点 L4287 位于
   `return` 后不可达块 → 真源强平永不触发；本版保真并单测锁定，激活方案记入阶段 5 备忘
5. **真源 quirk 保真（vwap tail(48)）**: `current_date_str` 全文件仅 L4901 一处 hasattr 读、
   从未赋值 → 恒走 `df_5.tail(48)` 分支；本版保留 hasattr 原样，单测锁定无 datetime 列可用
6. **真源死代码/死变量处置**: lunch_breakout_check 末尾不可达块（L4274–4296，引用未定义
   avg_price）未迁移；`is_yang/body_pct`（L1702–1709）、`long_complete`（L1755）、
   `pos = api.get_position(...)`（L3584）计算后未使用 → 保真保留并注明
7. **可测性注入（生产行为不变）**: `now_fn`（left_side 节流/时效检查、session_plays 全部
   时点判断）、`warn_fn`（真源 _warn_once_per_session L1276–1284 同款默认实现，ai_decision
   迁移后可替换注入）、`atr5_fn`/`index_price_fn`/`yesterday_close_fn`/`dynamic_levels_fn`/
   `ai_chat_fn`/`news_items_fn`（阶段 5 装配接线，见阶段 5 备忘）
8. **_warn_once_per_session 归属**: 真源属 ai_decision（L1276，阶段 4 迁移）；left_side 的
   stale_5min 告警暂用同款语义默认实现（按 key 按天去重），阶段 4 后统一注入
9. **自检扩展**: main.py --check 从 8 步扩到 9 步，新增过滤器/豁免/左侧/时段策略可构造性 +
   纯决策冒烟（数据不足路径 + 尾盘边界 + force_close_deadline quirk 断言）

## 阶段 4 关键决策记录（业务层: risk / position / order / conditional_orders / rollover / execution_pipeline）

1. **类划分**: 真源 10 个风控方法按职责归五类——`StopOutCooldown`（止损冷却状态 +
   记录/检查，真源散在 check_stop_profit L2946–2953 与 execute_decision L2125/L5119 的
   同款逻辑收拢）、`DailyTradeLimiter`（日次数 check/bump + pkl 恢复 restore）、
   `PositionSizer`（get_max_lots/max_lots_by_risk/get_risk_scale/apply_risk_scale）、
   `CircuitBreaker`（record_trade_result/load_state/save_state/check）、`EmergencyState`
   （emergency_mode 容器；自动重置 EMERGENCY_AUTO_RESET_SEC 属 run 主循环，阶段 5）
2. **CircuitBreaker 懒初始化保真**: `_daily_loss` 等状态字段保持真源 hasattr 懒初始化模式
   （未记录前 `check()` 返回 "无交易历史"、`get_risk_scale` 返回 1.0 的语义依赖"未初始化"）；
   PositionSizer 经 `daily_loss_fn` 注入读取（None ↔ 真源未初始化等价）
3. **全局状态消除落点**: `current_position`/`conditional_order`（真源 L147/L157 全局）→
   `PositionManager` 带锁（RLock）持有，pkl 格式保持 plain dict；`last_entry_time`
   （真源 L428）归 PositionManager（平仓绩效快照回退 + 开仓路径写入）
4. **pkl plain-dict 守护**（design.md §5.2 阶段 4 备忘落实）: load 时校验 pkl 顶层 /
   position / conditional_order 均为 plain dict（`_is_plain_value` 白名单: 标量 +
   datetime/date + 浅嵌套容器），违规拒绝加载保持空仓并记 error——防新版把 dataclass
   写进 pkl 后旧版 autotrade_fix.py 读新 pkl 出错；旧版两种格式（包裹/裸 dict）兼容有测试锁定
5. **check_stop_profit 纯决策化**: 返回 trigger_reason 由编排层执行
   `close_position` + 失败转 `emergency_close`；`_closing` 守卫改为参数
   （编排层读 `OrderExecutor.is_closing`），保持"平仓进行中不触发应急平仓"的真源语义；
   止损记录经 `on_stopout` 回调接线 StopOutCooldown.record
6. **业务层横向依赖一律构造注入**（不 import 兄弟模块）: order_executor 收
   pm/cb/metrics/emergency + quote_fn/atr5_fn/symbol_fn；conditional_orders 收
   pm/mds/mcs/calendar/filters/exemptions/sizer/limiter/cb/stopout/oe/emergency/tail_fn；
   rollover_manager 收 mds/mcs/api/pm/oe/emergency；execution_pipeline 收全量依赖。
   装配在阶段 5 system.py 完成
7. **execute_decision 状态读写归属**（design.md §5.4 状态矩阵的落点）:
   pm.position 读写（调整/加仓/开仓/清仓）、pm.conditional_order 读写（设置/清除）、
   pm.save_position_state 持久化、pm.last_entry_time 写入；pipeline 自持
   last_stop_adjust_time（L427）与 _failed_order_window（L2872 懒初始化 → 构造初始化）
8. **嵌套 conv 保真**: `conv(p)`（真源 L2112 嵌套闭包）随 execute_decision 保留为嵌套
   函数，内部调 `mds.index_to_future_price`（design.md §4.2 既定不提级）
9. **execute_ai_cycle 依赖注入**: prompt 构建/AI 调用/决策落盘 → `prompt_fn`/
   `ai_chat_fn`/`save_decision_fn` 注入——ai_decision 模块（9 方法，§4.2）按 §5.2
   不属阶段 4，阶段 5 落位后接线；prompt_fn 未注入时跳过 AI 调用返回默认间隔（防御性）
10. **symbol 双服务同步落实**: rollover 成功/失败路径均同时更新 `mds.symbol` 与
    `mcs.symbol` + `mds.im_quote` 切换（阶段 3 决策 3 的兑现）
11. **真源死键/死分支保真**: `notify_order_filled` 读 `pos.get('last_pnl', 0)`
    （真源 L3227 只读从不写入 → CLOSE 通知恒无盈亏行）；`get_max_lots` 的
    `else: balance = 0` 死分支（L792-793，account None 已提前 return）原样保留
12. **dry_run 硬约束落点**: order_executor 不内嵌模式分支（保持真源行为），
    阶段 5 装配以 mock api 注入实现"不得发出任何真实下单/撤单"（design.md §5.2 验收期）
13. **自检扩展**: main.py --check 从 9 步扩到 10 步，新增业务层组件可构造性 +
    手算冒烟（get_max_lots=4 / max_lots_by_risk 边界 / 熔断无历史语义 /
    get_next_dominant_im / WAIT 决策无动作 / execute_ai_cycle 默认间隔）
