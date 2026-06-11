"""AI 决策日志：JSONL append-only.

每条决策一行 JSON，便于离线回溯、A/B 测试不同 Prompt 版本。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import paths

logger = logging.getLogger(__name__)


class AIDecisionLogger:
    """LLM 决策 JSONL 记录器."""

    def __init__(self, log_file: Optional[Path] = None) -> None:
        self.log_file: Path = Path(log_file) if log_file else paths["ai_decisions"]
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def save(self, decision: dict, *, mode: str = "", extra: Optional[dict] = None) -> None:
        record: dict[str, Any] = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
            "decision": decision,
        }
        if extra:
            record["extra"] = extra
        try:
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.error("AI decision log write failed: %s", exc)


__all__ = ["AIDecisionLogger"]
