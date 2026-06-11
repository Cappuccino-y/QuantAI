"""启动前自检脚本.

用法：
    python scripts/verify_setup.py

检查项：
1. Python 版本 >= 3.10
2. 必要依赖已安装（tqsdk / openai / pandas / akshare 等）
3. .env 已配置且关键凭证非空
4. 关键路径可写（data/ 目录）
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def check_python_version() -> bool:
    if sys.version_info < (3, 10):
        print(f"❌ Python {sys.version_info.major}.{sys.version_info.minor} 太老，需要 3.10+")
        return False
    print(f"✅ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    return True


def check_dependencies() -> bool:
    required = [
        "tqsdk", "openai", "pandas", "numpy", "requests",
        "dotenv", "akshare", "efinance",
    ]
    missing = []
    for mod in required:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        print(f"❌ 缺少依赖: {missing}")
        print("   运行: pip install -r requirements.txt")
        return False
    print("✅ 所有必要依赖已安装")
    return True


def check_credentials() -> bool:
    try:
        from quantai.config import LOADED_ENV_PATH, ensure_credentials
    except Exception as exc:
        print(f"❌ 无法加载 quantai.config: {exc}")
        return False
    if LOADED_ENV_PATH is None:
        print("❌ 未找到 .env 文件")
        print("   运行: cp .env.example .env 并填入凭证")
        return False
    print(f"✅ 已加载 .env: {LOADED_ENV_PATH}")
    try:
        ensure_credentials(require_llm=True)
        print("✅ 所有凭证已就绪")
        return True
    except RuntimeError as exc:
        print(f"❌ {exc}")
        return False


def check_data_dir() -> bool:
    try:
        from quantai.config import paths
        for key, path in paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.parent.is_dir():
                print(f"❌ 无法创建 {key} 父目录: {path.parent}")
                return False
        print(f"✅ 运行时目录可写: {next(iter(paths.values())).parent}")
        return True
    except Exception as exc:
        print(f"❌ 检查 data 目录失败: {exc}")
        return False


def main() -> int:
    print("=" * 60)
    print("  QuantAI 启动自检")
    print("=" * 60)
    results = [
        check_python_version(),
        check_dependencies(),
        check_credentials(),
        check_data_dir(),
    ]
    print("=" * 60)
    if all(results):
        print("🎉 所有检查通过，可以启动 python main.py --mode paper")
        return 0
    print("⚠️  存在问题，请按上方提示修复后重试")
    return 1


if __name__ == "__main__":
    sys.exit(main())
