"""notifier — 钉钉通知（真源 L52–96 的 _safe_send 限频/去重逻辑类化迁移）。

行为保持（与原版 _safe_send 逐条对齐）:
1. 全局速率限制: 滑动窗口 60s 内最多 NOTIFY_RATE_LIMIT(10) 条（非关键消息）
2. 同类消息去重: 相同内容 NOTIFY_DEDUP_WINDOW(300s) 内只发一次（关键消息也去重）
3. 关键消息分级: 命中 NOTIFY_CRITICAL_KEYWORDS 的消息绕过限频（8/27 增强）
4. 去重表内存保护: 只保留最近 NOTIFY_DEDUP_TABLE_MAX(200) 条
5. 发送失败只记日志，不中断主流程

与原版差异: 原版通过 monkey-patch notifycation.send_dingtalk_message 全局生效；
本版封装为 DingTalkNotifier 类，sender 可注入（dry_run/测试时注入假 sender）。
"""
import logging
import threading
import time
from typing import Callable, List, Dict

from . import config


class DingTalkNotifier:
    def __init__(self, sender: Callable[[str], None] = None):
        if sender is None:
            # 默认使用 vendor 钉钉传输层（原版 notifycation.send_dingtalk_message）
            from .vendor import notifycation
            sender = notifycation.send_dingtalk_message
        self._sender = sender
        self._lock = threading.Lock()
        self._timestamps: List[float] = []       # (timestamp) 滑动窗口
        self._last_sent: Dict[str, float] = {}   # msg -> last_sent_time

    def send(self, msg: str) -> None:
        """限频+去重后的安全发送（真源 _safe_send L71–94 逐行对齐）。"""
        now = time.time()
        is_critical = any(k in msg for k in config.NOTIFY_CRITICAL_KEYWORDS)
        with self._lock:
            # 1. 全局速率限制：滑动窗口 60s 内最多 NOTIFY_RATE_LIMIT 条
            self._timestamps[:] = [t for t in self._timestamps if now - t < 60]
            if not is_critical and len(self._timestamps) >= config.NOTIFY_RATE_LIMIT:
                logging.warning(f"钉钉限频: 60秒内超过{config.NOTIFY_RATE_LIMIT}条，丢弃消息: {msg[:60]}")
                return
            # 2. 同类消息去重：相同内容在窗口内只发一次
            last = self._last_sent.get(msg)
            if last is not None and now - last < config.NOTIFY_DEDUP_WINDOW:
                logging.info(f"钉钉去重: {config.NOTIFY_DEDUP_WINDOW}秒内已发送过同类消息，跳过: {msg[:60]}")
                return
            self._timestamps.append(now)
            self._last_sent[msg] = now
            # 内存保护: 去重表只保留最近 200 条
            if len(self._last_sent) > config.NOTIFY_DEDUP_TABLE_MAX:
                for k in list(self._last_sent.keys())[:-config.NOTIFY_DEDUP_TABLE_MAX]:
                    self._last_sent.pop(k, None)
        try:
            self._sender(msg)
        except Exception as e:
            logging.error(f"钉钉发送失败（不影响交易）: {e}")
