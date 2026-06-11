"""钉钉机器人通知封装.

- Webhook 与 secret 从 :mod:`quantai.config` 读取
- 失败静默吞掉，不影响主交易循环
- 支持 ENABLE_NOTIFY=False 完全关闭（本地调试）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
from typing import Optional

import requests

from .config import dingtalk

logger = logging.getLogger(__name__)


class DingTalkNotifier:
    """钉钉自定义机器人 Markdown 通知客户端."""

    def __init__(
        self,
        webhook: Optional[str] = None,
        secret: Optional[str] = None,
        enabled: Optional[bool] = None,
        timeout: int = 10,
    ) -> None:
        self.webhook = webhook if webhook is not None else dingtalk.webhook
        self.secret = secret if secret is not None else dingtalk.secret
        self.enabled = enabled if enabled is not None else dingtalk.enabled
        self.timeout = timeout

    def _signed_url(self) -> str:
        if not self.secret:
            return self.webhook
        ts = str(round(time.time() * 1000))
        sign_str = f"{ts}\n{self.secret}"
        mac = hmac.new(self.secret.encode(), sign_str.encode(), digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(mac))
        return f"{self.webhook}&timestamp={ts}&sign={sign}"

    def send(self, content: str, title: str = "QuantAI 提醒", at_all: bool = True) -> bool:
        """发送 Markdown 消息；任何异常被吞掉并记日志."""
        if not self.enabled:
            logger.debug("DingTalk disabled, skip: %s", content[:80])
            return False
        if not self.webhook:
            logger.warning("DingTalk webhook empty, skip notification.")
            return False
        try:
            payload = {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": content},
                "at": {"isAtAll": at_all},
            }
            resp = requests.post(
                self._signed_url(),
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                logger.info("DingTalk message sent.")
                return True
            logger.error("DingTalk send failed: %s", resp.text)
            return False
        except Exception as exc:
            logger.error("DingTalk network error: %s", exc)
            return False


_default_notifier: Optional[DingTalkNotifier] = None


def get_notifier() -> DingTalkNotifier:
    global _default_notifier
    if _default_notifier is None:
        _default_notifier = DingTalkNotifier()
    return _default_notifier


def notify(content: str, title: str = "QuantAI 提醒", at_all: bool = True) -> bool:
    """模块级便捷函数：调用默认 notifier."""
    return get_notifier().send(content, title=title, at_all=at_all)


__all__ = ["DingTalkNotifier", "get_notifier", "notify"]
