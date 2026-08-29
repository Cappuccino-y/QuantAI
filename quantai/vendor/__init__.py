"""vendor 适配层 — 以 MainToy 版为准的逐字节保真拷贝。

文件清单与来源:
    trade_data_fetcher.py   ← D:/PythonProject/MainToy/trade/  (1052 行, 较 QuantAI 旧版 591 行新)
    jin10_news_fetcher.py   ← D:/PythonProject/MainToy/trade/  (371 行, 较旧版 265 行新)
    eastmoney_patch.py      ← D:/PythonProject/MainToy/trade/  (两版相同)
    backtest_core.py        ← D:/PythonProject/MainToy/trade/  (两版相同)
    akshare_multi_period.py ← D:/PythonProject/MainToy/trade/  (trade_data_fetcher 硬依赖)
    llm_client.py           ← D:/PythonProject/MainToy/tools/
    notifycation.py         ← D:/PythonProject/MainToy/tools/  (钉钉传输层, notifier 依赖)

这些文件保持与 MainToy 原版逐字节一致（哈希校验），禁止在本目录内修改——
升级方式是重新从 MainToy 拷贝覆盖。vendor 内部使用平铺 import
（如 `from eastmoney_patch import ...`），因此本 __init__ 把 vendor 目录
加入 sys.path 以保证 import 链不断。
"""
import os
import sys

_VENDOR_DIR = os.path.dirname(os.path.abspath(__file__))
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)
