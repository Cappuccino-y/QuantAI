"""quantai — IM 股指期货量化交易系统（QuantAI 式模块化重写）。

逻辑真源: D:/PythonProject/MainToy/trade/autotrade_fix.py (5659 行, 96 def)
架构参考: D:/PythonProject/QuantAI
设计文档: 工作区 design.md

依赖方向（自上而下，禁止反向/横向）:
    config → models → logger/notifier/performance/news_manager
           → market_data → risk_manager/position_manager/order_executor
           → strategies → ai_decision/conditional_orders
           → execution_pipeline → system
"""
__version__ = "0.1.0"
