"""
VeighNa Trader GUI Launcher
启动 vnpy 图形界面
"""
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp


def main():
    # 1. Qt 应用
    qapp = create_qapp()

    # 2. 事件引擎
    event_engine = EventEngine()

    # 3. 主引擎
    main_engine = MainEngine(event_engine)

    # 4. 加载已安装的模块
    # CTA 策略
    try:
        from vnpy_ctastrategy import CtaStrategyApp
        main_engine.add_app(CtaStrategyApp)
        print("[OK] CTA Strategy App loaded")
    except ImportError:
        print("[SKIP] CTA Strategy App not installed")

    # CTA 回测
    try:
        from vnpy_ctabacktester import CtaBacktesterApp
        main_engine.add_app(CtaBacktesterApp)
        print("[OK] CTA Backtester App loaded")
    except ImportError:
        print("[SKIP] CTA Backtester App not installed")

    # 5. 主窗口
    main_window = MainWindow(main_engine, event_engine)
    main_window.showMaximized()

    # 6. 事件循环
    qapp.exec()


if __name__ == "__main__":
    main()
