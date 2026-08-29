"""notifier 限频/去重/关键消息分级行为测试（对齐真源 _safe_send L71–94）。"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from quantai import config
from quantai.notifier import DingTalkNotifier


def _make_notifier():
    sent = []
    n = DingTalkNotifier(sender=sent.append)
    return n, sent


def test_rate_limit_drops_non_critical():
    """非关键消息: 60s 窗口内超过 10 条被丢弃。"""
    n, sent = _make_notifier()
    for i in range(config.NOTIFY_RATE_LIMIT):
        n.send(f"普通消息 {i} {time.time()}")
    assert len(sent) == config.NOTIFY_RATE_LIMIT
    n.send("第 11 条普通消息")
    assert len(sent) == config.NOTIFY_RATE_LIMIT  # 被限频丢弃


def test_critical_bypasses_rate_limit():
    """关键消息（如 平仓成功）绕过限频（8/27 增强）。"""
    n, sent = _make_notifier()
    for i in range(config.NOTIFY_RATE_LIMIT + 3):
        n.send(f"平仓成功 #{i}")
    assert len(sent) == config.NOTIFY_RATE_LIMIT + 3


def test_dedup_within_window():
    """相同内容 300s 内只发一次（关键消息也去重）。"""
    n, sent = _make_notifier()
    n.send("止损触发 IM4000")
    n.send("止损触发 IM4000")
    assert len(sent) == 1
    # 不同内容不去重
    n.send("止损触发 IM4010")
    assert len(sent) == 2


def test_sender_exception_does_not_raise():
    """发送失败只记日志，不中断主流程（真源 L91–94）。"""
    def boom(msg):
        raise RuntimeError("network down")
    n = DingTalkNotifier(sender=boom)
    n.send("测试消息")  # 不应抛异常


def test_dedup_table_capped():
    """去重表内存保护: 只保留最近 200 条（真源 L88–90）。"""
    n, sent = _make_notifier()
    for i in range(config.NOTIFY_DEDUP_TABLE_MAX + 50):
        n.send(f"去重表压测 {i}")
    assert len(n._last_sent) <= config.NOTIFY_DEDUP_TABLE_MAX
