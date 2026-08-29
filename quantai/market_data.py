"""market_data — 数据层（真源: autotrade_fix.py 12 个方法，design.md §4.2 market_data 表）。

方法映射（真源行号）:
- format_code            ← L693 嵌套闭包（随 _get_dominant_im 迁移提为模块级函数，design.md 既定）
- ContractResolver.get_dominant_im        ← _get_dominant_im L687–745
- TradingCalendar.is_trading_day          ← _is_trading_day L955–967
- TradingCalendar.get_previous_trading_day_15 ← _get_previous_trading_day_15 L949–953
- TradingCalendar.is_trading_time         ← _is_trading_time L5340–5351
- TradingCalendar.is_near_close           ← _is_near_close L3648–3657
- AccountView.get_equity                  ← _get_equity L805–811
- MarketDataService.update_index_price    ← _update_index_price L675–684
- MarketDataService.refresh_tech_data     ← _refresh_tech_data L748–756
- MarketDataService.get_basis_info        ← get_basis_info L759–784
- MarketDataService.index_to_future_price ← _index_to_future_price L969–976
- MarketDataService.get_yesterday_index_close ← _get_yesterday_index_close L3669–3680

行为保持: 全部阈值/日志文案/异常兜底路径逐行对齐真源。
结构差异（已在 ARCHITECTURE.md 决策记录）:
- 真源挂在上帝类上的数据状态（symbol/im_quote/index_price/tech_data_text）归
  MarketDataService 持有；换月（阶段 4 rollover_manager）直接更新该服务字段
- ContractResolver 注入 now_fn（仅测试用，生产默认 datetime.now，行为不变）
- ATR/OI/动态位阶按 design.md §4.2 属 strategies/market_context.py（阶段 3），不在本模块
- get_basis_info 异常捕获口径（阶段 2 验收 minor1 声明）: 真源 L769 为裸 `except:`，
  本版收窄为 `except Exception:`（不吞 KeyboardInterrupt/SystemExit，行为更安全；
  正常路径与到期日兜底路径的输出不变，故保留新写法）
"""
import logging
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import Any, Callable, Dict, Optional

import pandas as pd


# ---------- 合约代码格式化（真源 L693 嵌套闭包，提为模块级纯函数） ----------

def format_code(y: int, m: int) -> str:
    """合约代码格式化（真源 L693–694 逐行保真）。"""
    return f"CFFEX.IM{y:02d}{m:02d}"


# ---------- 主力合约识别（真源 L687–745） ----------

class ContractResolver:
    """IM 股指期货主力合约识别（基于最新静态持仓量）。

    now_fn 注入仅用于测试控制月份边界（如 12 月跨年候选推算），
    生产环境使用默认 datetime.now，与真源行为一致。
    """

    def __init__(self, api, now_fn: Callable[[], datetime] = datetime.now):
        self.api = api
        self._now_fn = now_fn

    def get_dominant_im(self) -> str:
        """获取IM股指期货的主力合约（基于最新静态持仓量）（真源 L688–745 逐行保真）。"""
        date = self._now_fn()
        year = date.year % 100
        month = date.month

        current = format_code(year, month)

        if month == 12:
            next_month = 1
            next_year = (year + 1) % 100
        else:
            next_month = month + 1
            next_year = year
        next_month_contract = format_code(next_year, next_month)

        quarter_months = [3, 6, 9, 12]
        found = False
        for qm in quarter_months:
            if qm > month:
                next_quarter_month = qm
                next_quarter_year = year
                found = True
                break
        if not found:
            next_quarter_month = 3
            next_quarter_year = (year + 1) % 100
        next_quarter = format_code(next_quarter_year, next_quarter_month)

        q_index = quarter_months.index(next_quarter_month)
        next_quarter2_month = quarter_months[(q_index + 1) % 4]
        next_quarter2_year = next_quarter_year
        if next_quarter2_month <= next_quarter_month:
            next_quarter2_year = (next_quarter_year + 1) % 100
        next_quarter2 = format_code(next_quarter2_year, next_quarter2_month)

        candidates = [current, next_month_contract, next_quarter, next_quarter2]
        logging.info(f"候选合约: {candidates}")

        # 直接读取静态持仓量，不等待更新
        max_oi = -1
        dominant = current
        for sym in candidates:
            q = self.api.get_quote(sym)
            oi = q.open_interest
            if oi is not None and oi > 0:
                logging.info(f"{sym} 持仓量: {oi}")
                if oi > max_oi:
                    max_oi = oi
                    dominant = sym

        if max_oi == -1:
            logging.warning("无法获取有效持仓量，默认使用当月合约")
        else:
            logging.info(f"识别主力合约: {dominant} (持仓量 {max_oi})")
        return dominant


# ---------- 交易日历（真源 L949–967 / L3648–3657 / L5340–5351） ----------

class TradingCalendar:
    """交易日/交易时段判断。

    is_trading_day 依赖 akshare 交易日历，失败时回退"周一至周五"简易判断
    （真源 L965–967 同款兜底，日志文案一致）。
    now_fn 注入仅用于测试控制时刻（生产默认 datetime.now，行为不变）。
    """

    def __init__(self, now_fn: Callable[[], datetime] = datetime.now):
        self._now_fn = now_fn

    def is_trading_day(self, date: Optional[datetime] = None) -> bool:
        """交易日判断（真源 _is_trading_day L955–967 逐行保真）。"""
        if date is None:
            date = datetime.now()
        try:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            # 将 trade_date 列转为 datetime 类型
            df['trade_date'] = pd.to_datetime(df['trade_date'])
            target_date = pd.to_datetime(date.date())
            return (df['trade_date'] == target_date).any()
        except Exception as e:
            logging.warning(f"获取交易日历失败: {e}，使用简易判断（周一至周五）")
            return date.weekday() < 5

    def get_previous_trading_day_15(self, dt: datetime) -> datetime:
        """上一交易日 15:00（真源 _get_previous_trading_day_15 L949–953 逐行保真）。"""
        date = dt - timedelta(days=1)
        while not self.is_trading_day(date):
            date -= timedelta(days=1)
        return date.replace(hour=15, minute=0, second=0, microsecond=0)

    def is_trading_time(self, now: Optional[datetime] = None) -> bool:
        """判断当前是否在IM期货交易时段内（日盘+夜盘）（真源 L5340–5351 逐行保真）。"""
        if now is None:
            now = datetime.now()
        if now.weekday() >= 5:
            return False
        t = now.time()
        return (
            (dt_time(9, 30) <= t <= dt_time(11, 30)) or
            (dt_time(13, 0) <= t <= dt_time(15, 0)) or
            (dt_time(21, 0) <= t <= dt_time(23, 0))
        )

    def is_near_close(self) -> bool:
        """判断当前是否临近休市/收盘（最后5分钟）（真源 L3648–3657 逐行保真）。"""
        now = self._now_fn().time()
        # 上午收盘前：11:25-11:30
        if dt_time(11, 25) <= now <= dt_time(11, 30):
            return True
        # 下午收盘前：14:55-15:00
        if dt_time(14, 55) <= now <= dt_time(15, 0):
            return True
        return False


# ---------- 账户权益（真源 L805–811） ----------

class AccountView:
    """账户动态权益读取（真源 _get_equity L805–811 类化）。"""

    def __init__(self, api):
        self.api = api

    def get_equity(self) -> float:
        """获取动态权益（真源逐行保真）。"""
        try:
            account = self.api.get_account()
            return account.balance + account.position_profit if account else 0.0
        except Exception:
            return 0.0


# ---------- 行情数据服务（真源 L675–684 / L748–784 / L969–976 / L3669–3680） ----------

class MarketDataService:
    """指数行情 + 基差 + 技术面数据。

    持有真源上帝类的数据层状态字段（__init__ L391–398）:
    - symbol / im_quote: 当前主力合约与行情引用（阶段 4 换月时由 rollover_manager 更新）
    - index_price: 最新指数点位（update_index_price 刷新）
    - tech_data_text: 技术面数据文本（refresh_tech_data 刷新）

    构造时若未显式传 symbol，则按真源 L391–392 顺序: 先识别主力合约，再订阅行情。
    """

    def __init__(self, api, index_fetcher, index_name: str = "中证1000",
                 symbol: Optional[str] = None, im_quote: Any = None):
        self.api = api
        self.index_fetcher = index_fetcher
        self.index_name = index_name
        self.index_price = 0.0  # 最新指数点位（真源 L397）
        self.tech_data_text = ""  # 真源 L398
        if symbol is None:
            symbol = ContractResolver(api).get_dominant_im()
        self.symbol = symbol
        self.im_quote = im_quote if im_quote is not None else api.get_quote(symbol)

    def update_index_price(self) -> None:
        """通过 IndexDataFetcher 获取中证1000指数最新价（真源 L675–684 逐行保真）。"""
        try:
            df = self.index_fetcher.get_kline_data(self.index_name, "5min")
            if df is not None and not df.empty:
                self.index_price = df.iloc[-1]['close']
            else:
                logging.warning("无法获取中证1000指数价格，基差计算可能异常")
        except Exception as e:
            logging.error(f"更新指数价格失败: {e}")

    def refresh_tech_data(self) -> None:
        """指数技术数据刷新（真源 L748–756 逐行保真）。"""
        try:
            self.tech_data_text = self.index_fetcher.generate_ai_prompt(
                index_name=self.index_name,
                periods=["5min", "15min", "30min", "60min", "日线", "周线"]
            )
            logging.info("指数技术数据刷新成功")
        except Exception as e:
            logging.error(f"刷新指数技术数据失败: {e}")

    def get_basis_info(self) -> Dict:
        """基差信息（真源 L759–784 逐行保真，含到期日获取失败时的简化计算兜底）。"""
        index_price = self.index_price
        im_price = self.im_quote.last_price
        basis = im_price - index_price
        basis_pct = (basis / index_price) * 100 if index_price else 0

        # 到期日精确获取（使用 tqsdk 合约信息）
        try:
            contract_info = self.api.get_contract_info(self.symbol)
            expiry = datetime.fromtimestamp(contract_info['expire_datetime'])
        except Exception:
            # 备用简化计算
            code = self.symbol.split('.')[-1]
            year = 2000 + int(code[2:4])
            month = int(code[4:6])
            expiry = datetime(year, month, 15)
        days_to_expiry = (expiry - datetime.now()).days

        return {
            "index_price": index_price,
            "im_price": im_price,
            "basis": basis,
            "basis_pct": basis_pct,
            "days_to_expiry": days_to_expiry,
            "symbol": self.symbol
        }

    def index_to_future_price(self, idx_price: float) -> float:
        """将指数价格换算为对应期货价格，圆整到最小变动价位0.2（真源 L969–976 逐行保真）。"""
        fut_price = self.im_quote.last_price
        idx_current = self.index_price
        if idx_current <= 0 or fut_price <= 0:
            return round(idx_price / 0.2) * 0.2  # fallback
        basis_rate = fut_price / idx_current
        return round(idx_price * basis_rate / 0.2) * 0.2

    def get_yesterday_index_close(self) -> Optional[float]:
        """获取昨日中证1000指数收盘价（真源 L3669–3680 逐行保真）。"""
        try:
            df = self.index_fetcher.get_kline_data(self.index_name, "日线")
            if df is not None and len(df) >= 2:
                return float(df.iloc[-2]['close'])
            else:
                logging.warning("无法获取昨日指数收盘价")
                return None
        except Exception as e:
            logging.error(f"获取昨日指数收盘价失败: {e}")
            return None
