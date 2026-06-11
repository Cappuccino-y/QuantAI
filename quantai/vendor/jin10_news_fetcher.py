import re
import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import requests


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
        从金十首页JS中动态提取最新的z3c子域名

        Returns
        -------
        str
            完整的API基础URL，例如 https://{32位hex}.z3c.jin10.com
        """
        headers = {
            "User-Agent": self._session.headers["User-Agent"],
            "Referer": self._session.headers["Referer"],
        }

        try:
            # 1. 获取首页，提取最新 index.js 路径
            resp_home = self._session.get("https://www.jin10.com/", headers=headers, timeout=10)
            resp_home.raise_for_status()

            match_js = re.search(r'/new/js/index\.([a-f0-9]+)\.js', resp_home.text)
            if not match_js:
                raise ValueError("未找到 index.js 路径")

            js_url = f"https://www.jin10.com/new/js/index.{match_js.group(1)}.js"
            logging.info(f"获取到 JS 文件: {js_url}")

            # 2. 获取 JS 内容
            resp_js = self._session.get(js_url, headers=headers, timeout=10)
            resp_js.raise_for_status()
            js_text = resp_js.text

            # 3. 匹配快讯接口域名模式
            pattern = r',\s*b\s*=\s*Object\(s\.a\)\s*\(\s*\{?\s*baseURL:\s*"//([a-f0-9]{32})\.z3c\.jin10\.com"'
            match = re.search(pattern, js_text)
            if match:
                base_url = f"https://{match.group(1)}.z3c.jin10.com"
                logging.info(f"匹配到 T 对应的域名: {base_url}")
                return base_url

            # 备用模式
            match_z3c = re.search(r'([a-f0-9]{32})\.z3c\.jin10\.com', js_text)
            if match_z3c:
                return f"https://{match_z3c.group(1)}.z3c.jin10.com"

            raise ValueError("未找到任何 z3c 域名")

        except Exception as e:
            logging.error(f"获取最新域名失败: {e}，使用备用硬编码域名。")
            return self.FALLBACK_BASE_URL

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
    news_list = fetcher.fetch_important_news("2026-6-10 00:00:00", "2026-6-10 12:59:59")
    print(news_list)
