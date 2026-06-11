"""QuantAI 项目根入口.

用法：
    python main.py                       # 默认 paper 模式
    python main.py --mode verify         # 仅校验 .env 凭证
    python main.py --mode live           # 实盘模式（生产环境）
    python main.py --mode dry-run        # 不下单不调 LLM，用于本地结构验证
"""
from __future__ import annotations

import sys

from quantai.cli import main

if __name__ == "__main__":
    sys.exit(main())
