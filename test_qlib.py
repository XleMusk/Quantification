"""
Qlib Quick Test — 验证数据和基础功能
数据范围: 2005-01-04 ~ 2021-06-11
"""
import qlib
from qlib.constant import REG_CN
from pathlib import Path


def main():
    QLIB_DATA_DIR = Path(r"D:/Quantification/qlib_data/cn_data")

    if not QLIB_DATA_DIR.exists():
        print(f"数据未找到: {QLIB_DATA_DIR}")
        print("请先运行: python get_qlib_data.py")
        return

    provider_uri = str(QLIB_DATA_DIR.resolve())

    # Windows 下用单进程模式避免 multiprocessing spawn 问题
    qlib.init(
        provider_uri=provider_uri,
        region=REG_CN,
        expression_cache=None,
        dataset_cache=None,
        joblib_backend="threading",  # threading 替代 multiprocessing
    )

    from qlib.data import D

    # 1. 基本信息
    inst = D.instruments(market="all")
    all_stocks = D.list_instruments(inst, as_list=True)
    print(f"总股票数: {len(all_stocks)}")
    print(f"数据范围: 2005-01-04 ~ 2021-06-11")

    # 2. 单只股票查询
    print("\n--- 浦发银行 SH600000 (2020年1月) ---")
    df = D.features(
        ["SH600000"],
        fields=["$close", "$volume", "$high", "$low", "$open"],
        start_time="2020-01-01",
        end_time="2020-01-31",
    )
    print(df.head(10))

    # 3. 多只股票
    print("\n--- 银行股对比 ---")
    bank_stocks = ["SH600000", "SH600015", "SH600016", "SH600036"]
    df_banks = D.features(
        bank_stocks,
        fields=["$close"],
        start_time="2020-06-01",
        end_time="2020-06-30",
    )
    print(df_banks.head(10))

    # 4. 沪深300成分股数
    csi300 = D.instruments(market="csi300")
    print(f"\n沪深300成分股数: {len(D.list_instruments(csi300))}")

    print("\nQlib 就绪！")


if __name__ == "__main__":
    main()
