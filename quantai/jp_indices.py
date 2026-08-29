"""jp_indices — 日韩联动数据层（真源: autotrade_fix.py 4 个方法，design.md §4.2 jp_indices 表）。

方法映射（真源行号）:
- JPIndicesService.fetch_jp_indices            ← fetch_jp_indices L3684–3723（60s 缓存）
- JPIndicesService.calc_nk225_max_move_in_window ← calc_nk225_max_move_in_window L3725–3754
- JPIndicesService.calc_kospi_amp_delta_in_window ← calc_kospi_amp_delta_in_window L3756–3801
- refresh_lunch_context                        ← _refresh_lunch_context L3803–3807

行为保持: 窗口过滤（字符串 HH:MM 比较、双端闭区间）、round(..., 2)、缓存 60s、
异常返回 None 的兜底路径逐行对齐真源。窗口计算是纯逻辑，单测直接对拍旧实现
（design.md §5.2 阶段 2 验收点）。

LunchContext 默认键集来自真源 __init__ L438–447（update_time 由模型字段承载，
不再作为 values 内的键，见 models.LunchContext docstring——阶段 1 已验收的结构差异）。
"""
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

from .models import LunchContext

# 真源 __init__ L438–447 的 lunch_context 初始键（不含 update_time——模型字段承载）
DEFAULT_LUNCH_CONTEXT_KEYS = (
    'nk225_9am_pct',        # 日经 9:00 涨跌幅
    'nk225_1130_pct',       # 日经 11:30 涨跌幅（A 股午休起点）
    'nk225_1230_pct',       # 日经 12:30 涨跌幅（12:50 顺势单依据）
    'topix_1230_pct',       # 东证 12:30 涨跌幅
    'nk225_1230_max_move',  # 日经 11:30-12:30 最大变动（点）
    'index_call_auction',   # 9:25 集合竞价撮合指数
    'index_last_close',     # 昨日收盘指数
)


def create_default_lunch_context() -> LunchContext:
    """构造带真源初始键集的 LunchContext（对应真源 L438–447 初始化）。"""
    return LunchContext(values={k: None for k in DEFAULT_LUNCH_CONTEXT_KEYS},
                        update_time="")


def refresh_lunch_context(ctx: LunchContext, key: str, value) -> None:
    """写入 lunch_context 并打日志（真源 _refresh_lunch_context L3803–3807 逐行保真）。"""
    ctx.set(key, value)
    logging.info(f"[日韩联动] {key} = {value} @ {ctx.update_time}")


class JPIndicesService:
    """日经 N225 + KOSPI 行情拉取与窗口计算（早盘前 + 12:50 顺势单的数据底座）。"""

    def __init__(self, index_fetcher):
        self.index_fetcher = index_fetcher
        # 日韩数据缓存（60s）（真源 L456）
        self._jp_cache: Dict[str, Any] = {'data': None, 'time': 0}

    def fetch_jp_indices(self) -> Optional[Dict]:
        """
        拉取日经 N225 + KOSPI 实时数据（通过 IndexDataFetcher.get_asian_indices_5min_bars）。
        返回当日 5min K 线 + 关键时点涨跌幅。
        带 60s 缓存。
        返回: {
            'nk225_now': float, 'nk225_pct': float,
            'kospi_now': float, 'kospi_pct': float,
            'nk225_5min': list[dict], 'kospi_5min': list[dict],
            'ts': str
        }
        失败返回 None。
        （真源 L3684–3723 逐行保真）
        """
        # 60s 缓存
        now_ts = time.time()
        if self._jp_cache['data'] and now_ts - self._jp_cache['time'] < 60:
            return self._jp_cache['data']
        try:
            raw = self.index_fetcher.get_asian_indices_5min_bars()
            indices = raw.get('indices', {})
            nk = indices.get('nikkei225', {})
            kospi = indices.get('kospi', {})
            nk_bars = nk.get('5min_bars', [])
            kospi_bars = kospi.get('5min_bars', [])
            data = {
                'nk225_now': nk_bars[-1]['close'] if nk_bars else None,
                'nk225_pct': nk_bars[-1]['change_pct_from_prev_close'] if nk_bars else None,
                'nk225_prev_close': nk.get('prev_close'),
                'kospi_now': kospi_bars[-1]['close'] if kospi_bars else None,
                'kospi_pct': kospi_bars[-1]['change_pct_from_prev_close'] if kospi_bars else None,
                'kospi_prev_close': kospi.get('prev_close'),
                'nk225_5min': nk_bars,
                'kospi_5min': kospi_bars,
                'ts': raw.get('timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
            }
            self._jp_cache = {'data': data, 'time': now_ts}
            return data
        except Exception as e:
            logging.error(f"拉取日经/KOSPI 失败: {e}")
            return None

    def calc_nk225_max_move_in_window(self, start_hm: str, end_hm: str) -> Optional[float]:
        """
        计算日经 5min K 线在 [start_hm, end_hm] 窗口内的最大变动幅度（%）。
        变动 = (max_high - min_low) / prev_close * 100。
        例：start='11:30' end='12:30' → 11:30-12:30 日经最大振幅。
        （真源 L3725–3754 逐行保真）
        """
        jp = self.fetch_jp_indices()
        if not jp or not jp.get('nk225_5min') or not jp.get('nk225_prev_close'):
            return None
        prev_close = jp['nk225_prev_close']
        max_high = None
        min_low = None
        for bar in jp['nk225_5min']:
            t = bar['time']  # 'YYYY-MM-DD HH:MM:SS'
            if not t:
                continue
            hm = t[11:16]  # 'HH:MM'
            if hm < start_hm or hm > end_hm:
                continue
            h = bar.get('high')
            l = bar.get('low')
            if h is None or l is None:
                continue
            if max_high is None or h > max_high:
                max_high = h
            if min_low is None or l < min_low:
                min_low = l
        if max_high is None or min_low is None:
            return None
        return round((max_high - min_low) / prev_close * 100, 2)

    def calc_kospi_amp_delta_in_window(self, start_hm: str, end_hm: str) -> Optional[Dict[str, float]]:
        """
        计算 KOSPI 5min K 线在 [start_hm, end_hm] 窗口内的振幅+变动。
        返回: {'amp': 振幅%, 'delta': 变动% (末-初)}
        例：start='11:30' end='12:50' → 11:30-12:50 KOSPI 振幅和变动。
        （真源 L3756–3801 逐行保真）
        """
        jp = self.fetch_jp_indices()
        if not jp or not jp.get('kospi_5min') or not jp.get('kospi_prev_close'):
            return None
        max_high = None
        min_low = None
        first_open = None
        last_close = None
        for bar in jp['kospi_5min']:
            t = bar.get('time', '')
            if not t or len(t) < 16:
                continue
            hm = t[11:16]
            if hm < start_hm or hm > end_hm:
                continue
            h = bar.get('high')
            l = bar.get('low')
            o = bar.get('open')
            c = bar.get('close')
            if h is None or l is None:
                continue
            if max_high is None or h > max_high:
                max_high = h
            if min_low is None or l < min_low:
                min_low = l
            if first_open is None and o is not None:
                first_open = o
            if c is not None:
                last_close = c
        if max_high is None or min_low is None or first_open is None or last_close is None:
            return None
        amp = (max_high - min_low) / first_open * 100
        delta = (last_close - first_open) / first_open * 100
        return {
            'amp': round(amp, 2),
            'delta': round(delta, 2),
            'max_high': max_high,
            'min_low': min_low,
            'first_open': first_open,
            'last_close': last_close,
        }
