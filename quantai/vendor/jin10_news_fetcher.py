import os
import re
import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import requests


# 共享缓存：与 finanCalc/report_news_data.py 共用同一文件
_JIN10_Z3C_CACHE_FILE = r"D:\PythonProject\MainToy\.jin10_z3c_cache.json"


def _load_cached_z3c_url() -> Optional[str]:
    """读取本地缓存的 z3c 域名。文件不存在或解析失败返回 None。"""
    try:
        if os.path.exists(_JIN10_Z3C_CACHE_FILE):
            with open(_JIN10_Z3C_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                url = data.get("url")
                if isinstance(url, str) and url.startswith("https://") and ".z3c.jin10.com" in url:
                    return url
    except Exception as e:
        logging.debug(f"读取 z3c 缓存失败: {e}")
    return None


def _save_cached_z3c_url(url: str) -> None:
    """把探测成功的 z3c 域名写入本地缓存。"""
    try:
        os.makedirs(os.path.dirname(_JIN10_Z3C_CACHE_FILE), exist_ok=True)
        with open(_JIN10_Z3C_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"url": url, "ts": time.time()}, f, ensure_ascii=False)
        logging.info(f"已缓存 z3c 域名: {url}")
    except Exception as e:
        logging.warning(f"写入 z3c 缓存失败: {e}")


def _invalidate_z3c_cache() -> None:
    """使用缓存的 URL 请求失败时调用，清除缓存以便下次重新探测。"""
    try:
        if os.path.exists(_JIN10_Z3C_CACHE_FILE):
            os.remove(_JIN10_Z3C_CACHE_FILE)
            logging.info("已清除 z3c 缓存（下次将重新探测）")
    except Exception as e:
        logging.debug(f"清除 z3c 缓存失败: {e}")


def _detect_z3c_base_url() -> str:
    """
    实际探测最新的 z3c 域名（无缓存时调用）。
    策略：从 index.js 抓出所有 32-hex 候选域名，逐个实测 /flash 接口，
          第一个返回 200/400 的就是当前有效的。
    探测成功会写入缓存；探测失败返回硬编码 fallback（不写缓存）。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
        "Referer": "https://www.jin10.com/",
    }
    session = requests.Session()

    try:
        # 1. 抓首页，定位 index.js
        resp_home = session.get("https://www.jin10.com/", headers=headers, timeout=10)
        resp_home.raise_for_status()
        match_js = re.search(r'/new/js/index\.([a-f0-9]+)\.js', resp_home.text)
        if not match_js:
            raise ValueError("首页中未找到 index.js 路径")
        js_url = f"https://www.jin10.com/new/js/index.{match_js.group(1)}.js"
        logging.info(f"获取到 JS 文件: {js_url}")

        # 2. 抓 JS 内容
        resp_js = session.get(js_url, headers=headers, timeout=10)
        resp_js.raise_for_status()
        js_text = resp_js.text

        # 3. 提取所有 32-hex 候选 z3c 域名（去重保序）
        candidates = re.findall(r'([a-f0-9]{32})\.z3c\.jin10\.com', js_text)
        seen, ordered = set(), []
        for h in candidates:
            if h not in seen:
                seen.add(h)
                ordered.append(h)
        if not ordered:
            raise ValueError("JS 中未找到任何 z3c 候选域名")
        logging.info(f"发现 {len(ordered)} 个 z3c 候选，开始探测...")

        # 4. 逐个探测（200=命中，400=后端在但参数错 = 域名有效）
        probe_params = json.dumps(
            {"hot": ["火", "热", "沸", "爆"],
             "max_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "channel": [1, 5]},
            separators=(',', ':'),
        )
        probe_headers = {
            **headers,
            "X-App-Id": "bVBF4FyRTn5NJF5n",
            "X-Version": "1.0",
            "Handleerror": "true",
        }

        for i, h in enumerate(ordered):
            probe_url = f"https://{h}.z3c.jin10.com/flash?params={requests.utils.quote(probe_params)}"
            try:
                r = session.get(probe_url, headers=probe_headers, timeout=6)
                if r.status_code in (200, 400):
                    base_url = f"https://{h}.z3c.jin10.com"
                    logging.info(f"[{i + 1}/{len(ordered)}] 命中: {base_url} (status={r.status_code})")
                    _save_cached_z3c_url(base_url)
                    return base_url
                logging.debug(f"[{i + 1}/{len(ordered)}] {h} 返回 {r.status_code}，跳过")
            except requests.exceptions.RequestException as e:
                logging.debug(f"[{i + 1}/{len(ordered)}] {h} 异常: {e}")
                continue

        raise ValueError(f"所有 {len(ordered)} 个候选 z3c 域名均不可达")

    except Exception as e:
        logging.error(f"动态探测 z3c 域名失败: {e}，使用硬编码 fallback。")
        return Jin10FlashFetcher.FALLBACK_BASE_URL


def get_latest_z3c_base_url(force_refresh: bool = False) -> str:
    """
    获取快讯接口域名。优先使用本地缓存，缓存缺失或 force_refresh=True 时才探测。

    行为：
    - 缓存存在 → 直接返回（零网络开销）
    - 缓存不存在 → 探测 → 成功则写入缓存并返回
    - 探测失败 → 返回硬编码 fallback（不写缓存，下次还会再探测）
    """
    if not force_refresh:
        cached = _load_cached_z3c_url()
        if cached:
            logging.debug(f"使用缓存的 z3c 域名: {cached}")
            return cached
    return _detect_z3c_base_url()


class Jin10FlashFetcher:
    """金十重要快讯抓取器，支持动态获取API域名与翻页抓取"""

    # 默认热度标签
    DEFAULT_HOT_TAGS = ["火", "热", "沸", "爆"]
    # 默认频道
    DEFAULT_CHANNELS = [1, 5]
    # 备用域名（当动态获取失败时使用）
    FALLBACK_BASE_URL = "https://3318fc142ea545eab931e22a61ec6e5c.z3c.jin10.com"

    def __init__(self, base_url: Optional[str] = None):
        """
        初始化抓取器

        Parameters
        ----------
        base_url : str, optional
            API基础域名，若不提供则自动从金十首页动态提取最新z3c子域名
        """
        self.base_url = base_url
        self._session = requests.Session()
        self._setup_headers()

    def _setup_headers(self):
        """配置默认请求头"""
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
            "Referer": "https://www.jin10.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": "https://www.jin10.com",
        })

    def _get_latest_base_url(self) -> str:
        """
        获取快讯接口域名。优先使用本地缓存，缓存缺失时探测。
        委托给模块级函数 get_latest_z3c_base_url()，保证两个脚本共享缓存。
        """
        return get_latest_z3c_base_url()

    @property
    def api_base_url(self) -> str:
        """获取API基础URL，若未初始化则自动获取并缓存"""
        if self.base_url is None:
            self.base_url = self._get_latest_base_url()
        return self.base_url

    def fetch_important_news(
        self,
        start_time: str,
        end_time: str,
        hot_tags: Optional[List[str]] = None,
        channels: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取指定时间段内带有热度标签的重要快讯

        Parameters
        ----------
        start_time : str
            开始时间，格式 "YYYY-MM-DD HH:MM:SS"
        end_time : str
            结束时间，格式 "YYYY-MM-DD HH:MM:SS"
        hot_tags : List[str], optional
            热度标签列表，默认使用 ["火", "热", "沸", "爆"]
        channels : List[int], optional
            频道列表，默认使用 [1, 5]

        Returns
        -------
        List[Dict[str, Any]]
            重要快讯列表（按时间正序排列）
        """
        if hot_tags is None:
            hot_tags = self.DEFAULT_HOT_TAGS
        if channels is None:
            channels = self.DEFAULT_CHANNELS

        base_url = self.api_base_url
        api_path = "/flash"

        # API专用请求头
        api_headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br",
            "X-App-Id": "bVBF4FyRTn5NJF5n",
            "X-Version": "1.0",
            "Handleerror": "true",
            "Referer": "https://www.jin10.com/",
            "User-Agent": self._session.headers["User-Agent"],
        }

        all_important_news = []
        current_max_time = end_time
        target_start_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")

        page = 1
        cache_refreshed_this_session = False  # 本次任务内只重探测一次
        while True:
            # 构造请求参数
            params_obj = {
                "hot": hot_tags,
                "max_time": current_max_time,
                "channel": channels,
            }
            params_str = json.dumps(params_obj, separators=(',', ':'))
            url = f"{base_url}{api_path}?params={requests.utils.quote(params_str)}"

            logging.info(f"第 {page} 页请求，max_time = {current_max_time}")

            data = self._request_with_retry(url, api_headers)

            # 重试耗尽 → 怀疑缓存的 base_url 已失效，强制重新探测一次
            if data is None and not cache_refreshed_this_session:
                logging.warning("连续重试失败，怀疑缓存域名已失效，强制重新探测...")
                _invalidate_z3c_cache()
                new_url = _detect_z3c_base_url()
                if new_url != base_url:
                    base_url = new_url
                    cache_refreshed_this_session = True
                    url = f"{base_url}{api_path}?params={requests.utils.quote(params_str)}"
                    logging.info(f"使用新域名重试本页: {base_url}")
                    data = self._request_with_retry(url, api_headers, max_retries=3)

            if data is None:
                logging.error("连续重试失败，终止抓取。")
                break

            news_list = data.get("data", [])
            if not news_list:
                logging.info("本页无数据，翻页结束。")
                break

            # 过滤时间范围内的新闻
            in_range_news = []
            earliest_time_in_page = None
            for item in news_list:
                time_str = item.get("time")
                if not time_str:
                    continue
                try:
                    news_dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue

                if earliest_time_in_page is None or news_dt < earliest_time_in_page:
                    earliest_time_in_page = news_dt

                if news_dt < target_start_dt:
                    continue

                in_range_news.append(item)

            all_important_news.extend(in_range_news)
            logging.info(
                f"本页获取 {len(news_list)} 条，符合时间范围 {len(in_range_news)} 条，累计 {len(all_important_news)} 条。"
            )

            # 判断翻页终止条件
            if earliest_time_in_page and earliest_time_in_page <= target_start_dt:
                logging.info("已覆盖目标时间范围，停止翻页。")
                break
            if len(news_list) < 30:
                logging.info("返回数据量较少，可能已到末尾。")
                break

            # 准备下一页的 max_time
            if earliest_time_in_page:
                next_max_time = (earliest_time_in_page - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
                current_max_time = next_max_time
            else:
                break

            page += 1
            sleep_sec = random.uniform(1.5, 5.5)
            logging.info(f"等待 {sleep_sec:.2f} 秒后请求下一页...")
            time.sleep(sleep_sec)

        # 按时间正序返回（原始数据是倒序的）
        all_important_news.reverse()
        logging.info(f"总共获取到 {len(all_important_news)} 条重要快讯。")
        return all_important_news

    def _request_with_retry(self, url: str, headers: Dict[str, str], max_retries: int = 5) -> Optional[Dict]:
        """
        带指数退避重试的请求方法

        Parameters
        ----------
        url : str
            请求URL
        headers : Dict[str, str]
            请求头
        max_retries : int
            最大重试次数

        Returns
        -------
        Optional[Dict]
            解析后的JSON数据，失败返回None
        """
        for attempt in range(max_retries):
            try:
                resp = requests.get(url, headers=headers, timeout=20)
                if resp.status_code == 502:
                    logging.warning(f"502 Bad Gateway (尝试 {attempt + 1}/{max_retries})，等待后重试...")
                    time.sleep(2 ** (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException as e:
                logging.warning(f"请求异常 (尝试 {attempt + 1}/{max_retries}): {e}")
                time.sleep(2 ** attempt)
            except json.JSONDecodeError as e:
                logging.warning(f"JSON解析失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                time.sleep(2)
        return None

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    fetcher = Jin10FlashFetcher()
    # 注意：日期必须 YYYY-MM-DD 格式（月/日必须补零），API 会拒绝 2026-6-10 这种
    now = datetime.now()
    end = now.strftime("%Y-%m-%d %H:%M:%S")
    start = (now - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    news_list = fetcher.fetch_important_news(start, end)
    print(news_list)
