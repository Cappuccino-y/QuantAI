"""命令行入口."""
from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="quantai",
        description="多源数据驱动的 IM 股指期货 T+0 LLM 量化交易系统",
    )
    parser.add_argument(
        "--mode",
        choices=("live", "paper", "verify", "dry-run"),
        default="paper",
        help="运行模式：live=实盘 / paper=模拟 / verify=仅校验依赖 / dry-run=不调 LLM 不下单",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="覆盖默认日志等级（DEBUG/INFO/WARNING/ERROR）",
    )
    args = parser.parse_args(argv)

    from .config import ensure_credentials, runtime
    from .logger import setup_logging

    if args.log_level:
        import os
        os.environ["LOG_LEVEL"] = args.log_level

    setup_logging()
    log = logging.getLogger("quantai.cli")
    log.info("QuantAI starting in mode=%s env=%s", args.mode, runtime.env)

    if args.mode == "verify":
        try:
            ensure_credentials(require_llm=True)
            log.info("✅ 所有凭证均已配置且就绪。")
            return 0
        except RuntimeError as exc:
            log.error("❌ %s", exc)
            return 1

    try:
        from .system import IMTradingSystem
    except Exception as exc:
        log.error("Failed to import IMTradingSystem: %s", exc)
        return 2

    system = IMTradingSystem(dry_run=(args.mode == "dry-run"))
    try:
        system.run()
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    except Exception as exc:
        log.error("System crashed: %s", exc, exc_info=True)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
