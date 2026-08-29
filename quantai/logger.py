"""logger — 日志设置 + TradeLogger（真源 L41–50, L159–187 逐行迁移）。"""
import csv
import logging
import os
import threading
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

from . import config


def setup_logging(log_file: str = None) -> None:
    """同时输出到控制台和文件（保留 7 天日志，便于复盘）——真源 L40–50。"""
    config.ensure_data_dir()
    log_file = log_file or config.LOG_FILE
    file_handler = TimedRotatingFileHandler(
        log_file, when='midnight', backupCount=7, encoding='utf-8'
    )
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[file_handler, logging.StreamHandler()]
    )


class TradeLogger:
    """交易日志记录器 — 直接 append 模式，无需读取整体（真源 L159–185 逐行迁移）。"""

    def __init__(self, log_file=None):
        # 修复 M6: 默认路径基于配置（原版基于脚本目录，本版统一 DATA_DIR），避免依赖启动 cwd
        self.log_file = log_file or config.TRADE_LOG_FILE
        self._lock = threading.Lock()  # 防止多线程写入冲突
        self._init_csv()

    def _init_csv(self):
        if not os.path.exists(self.log_file):
            with open(self.log_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "event_type", "symbol", "direction", "volume",
                    "price", "pnl", "balance_after", "ai_reason"
                ])

    def log(self, event_type, symbol, direction, volume, price, pnl=0.0, balance_after=0.0, ai_reason=""):
        # 直接 append 单行，O(1) 时间复杂度，不读取整体文件
        with self._lock:
            with open(self.log_file, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    event_type, symbol, direction or "", volume,
                    f"{price:.2f}", f"{pnl:.2f}", f"{balance_after:.2f}", ai_reason
                ])
