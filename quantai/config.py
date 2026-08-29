"""config — 全部常量迁移（真源: autotrade_fix.py L27–157）。

迁移原则:
1. 常量名与值与原版逐项一致（含全部注释里的案例依据），作为行为等价验收 checklist
2. 原版硬编码账密（L99–100）改为 .env 读取（QUANTAI_ACCOUNT / QUANTAI_PASSWORD）
3. 原版 8 个路径常量基于脚本目录（修复 M6），本版改为 DATA_DIR（env QUANTAI_DATA_DIR
   或 <项目根>/data）统一管理，pkl/json/csv 状态文件格式不变，接管时按 §5.4 拷贝迁移
"""
import os

# ---------- .env 加载（无第三方依赖的极简实现；vendor 内模块自带 load_dotenv 不冲突） ----------

def _load_dotenv(path: str) -> None:
    """极简 .env 解析: KEY=VALUE 行，# 注释行跳过，不覆盖已存在的环境变量。"""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        # 配置加载失败不中断（与原版"通知失败不中断主流程"同哲学）
        pass


# 项目根 = quantai/ 的上一级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# ---------- 路径（真源 L27–35；原版基于脚本目录，本版统一到 DATA_DIR） ----------
DATA_DIR = os.environ.get("QUANTAI_DATA_DIR") or os.path.join(PROJECT_ROOT, "data")
POSITION_FILE = os.path.join(DATA_DIR, "position_state.pkl")
LOG_FILE = os.path.join(DATA_DIR, "trading.log")
TRADE_LOG_FILE = os.path.join(DATA_DIR, "trade_log.csv")
METRICS_FILE = os.path.join(DATA_DIR, "performance_metrics.csv")
AI_DECISIONS_FILE = os.path.join(DATA_DIR, "ai_decisions.jsonl")
TRADES_HISTORY_FILE = os.path.join(DATA_DIR, "trades_history.jsonl")
CIRCUIT_BREAKER_FILE = os.path.join(DATA_DIR, "circuit_breaker_state.json")
PERF_STATE_FILE = os.path.join(DATA_DIR, "performance_state.json")


def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


# ---------- 新闻缓存上限（真源 L38，修复 M3：防止长时间运行内存/prompt 无限膨胀） ----------
NEWS_CACHE_MAX = 200

# ---------- 钉钉通知（真源 L58–69） ----------
NOTIFY_RATE_LIMIT = 10          # 全局: 60 秒窗口内最多 10 条
NOTIFY_DEDUP_WINDOW = 300       # 去重窗口: 相同消息 5 分钟内只发一次
# 8/27: 关键消息分级——成交/平仓/熔断/异常类消息绕过全局限频（去重仍保留），
# 防止重要通知被刷屏限频误杀（如高频拦截期间丢失平仓确认）
NOTIFY_CRITICAL_KEYWORDS = (
    "平仓成功", "开仓成功", "条件单入场", "成交:", "熔断", "失败",
    "紧急", "请手动", "手动处理", "止损触发", "过期条件单", "重连"
)
# 去重表内存保护上限（真源 L88）
NOTIFY_DEDUP_TABLE_MAX = 200

# ---------- 账户与配置（真源 L99–103；账密改 .env，禁止硬编码） ----------
ACCOUNT = os.environ.get("QUANTAI_ACCOUNT", "")
PASSWORD = os.environ.get("QUANTAI_PASSWORD", "")
MIN_CONFIDENCE = 0.55

# ---------- 动态频率配置（真源 L107–115） ----------
BASE_DECISION_INTERVAL = 900   # 波段基础频率 15分钟
SHORT_TERM_INTERVAL = 300      # 短线基础频率 5分钟
MIN_DECISION_INTERVAL = 300    # 最小决策间隔 5分钟（单次AI循环需5~25s，留足时间看完整K线）
MAX_DECISION_INTERVAL = 1200   # 最大决策间隔 20分钟

# 市场状态阈值
SCALPING_ATR_RATIO = 1.3       # 5minATR/15minATR > 此值触发短线模式
BREAKOUT_THRESHOLD = 0.3       # 突破幅度阈值（%）
STOP_ADJUST_COOLDOWN = 300     # 止损调整冷却时间 5分钟

# ---------- P0/P1 风险控制常量（真源 L118–135） ----------
# 止损 ratchet：止损只能朝"保护利润"方向移动，反向放宽需更高门槛
STOP_RELAX_REQUIRED_CONFIDENCE = 0.75  # 想放宽止损需要信心 >= 此值
# 硬性最低止损距离（单位：5minATR 倍数）—— 防止 AI 设过紧被秒扫
# 注意：现在用 5minATR（不是 15minATR）。5minATR ≈ 15minATR 的 1/2
# 6/11 案例：5minATR=23, 1.0×5minATR=23点（合理）; 之前 1.0×15minATR=47点（过宽）
MIN_STOP_DISTANCE_ATR_MULT = 0.8       # 立即单止损至少 0.8×5minATR (约 18-24 点)
MIN_STOP_DISTANCE_ATR_MULT_COND = 0.6  # 条件单止损至少 0.6×5minATR (约 14-18 点)
# 同向加仓控制（允许加仓，但门槛较高）
ADD_REQUIRED_CONFIDENCE = 0.70         # 加仓所需最低信心（6/15-6/16 统计：AI 0.7+ 信心约 5% 决策）
ADD_MIN_PRICE_GAP_ATR = 1.0            # 加仓价与首仓价至少相差 1.0×15minATR
ADD_MAX_DRAWDOWN_PCT = 1.5             # 加仓时浮亏不能超过 1.5%（防止追跌/追涨被套）
MAX_POSITION_LOTS = 3                  # 最大持仓手数（不论 max_lots 多大）
# 止损后冷却
# 业界实践: 人工交易建议 30-60 分钟(防报复性交易/情绪), 但自动化系统无情绪,
# 冷却目的仅是避免同一方向连续被打止损(whipsaw), 等 3 根 5min K 线重新确认结构即可
STOPOUT_COOLDOWN_SEC = 900             # 止损平仓后 15 分钟内禁止开同向仓位
# emergency_mode 自动重置
EMERGENCY_AUTO_RESET_SEC = 1800        # 30 分钟后自动复位 emergency_mode

# ---------- 8/14 新增：代码级风险兜底（LLM proposes, risk layer disposes）（真源 L138–143） ----------
# 仓位规则落地到代码，不信任 AI 自报手数：
MAX_RISK_PCT = 0.01                    # 单笔最大风险 = 1% 动态权益（代码强制）
                                       # 手数 = (权益 × MAX_RISK_PCT) / (止损距离 × 200元/点)
MAX_STOP_DISTANCE_ATR_MULT = 3.0       # 单笔止损距离硬上限 = 3×15minATR（防 AI 设超宽止损）
MAX_ROUND_TRIPS_PER_DAY = 6            # 单日开仓次数上限（止损→报复性再进循环截断）
DAILY_LOSS_WARN_RATIO = 0.6            # 日亏达到熔断阈值(1.5%)的 60% → 仓位减半（降档预警）

# ---------- 运行模式 ----------
DRY_RUN = os.environ.get("QUANTAI_DRY_RUN", "0") == "1"
# dry_run 硬约束（design.md §5.2 验收期）: 该模式下 order_executor 不得发出任何真实
# 下单/撤单，且使用独立 sim 账户或只读连接，防止干扰实盘账户状态。
