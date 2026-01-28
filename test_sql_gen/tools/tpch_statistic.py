import argparse
import ast

import pandas as pd

# 统计慢SQL的根因
# 使用方法
# python tpch_statistic.py --csv_path /path/to/file.csv --analyze_query --analyze_root_cause --analyze_duration
# 示例
# python tpch_statistic.py --csv_path /root/DREAM/data/tpc_h.csv --analyze_query --analyze_root_cause
# python tpch_statistic.py --csv_path /root/DREAM/data/tpc_c.csv --analyze_query --analyze_root_cause
# python tpch_statistic.py --csv_path /root/DREAM/data/tpc_ds.csv --analyze_query --analyze_root_cause
# python tpch_statistic.py --csv_path ./slow_sql_root_cause.csv --analyze_root_cause --analyze_duration
# python tpch_statistic.py --csv_path /root/DREAM/data/tpc_h.csv --analyze_duration


def analyze_multilabels(csv_path, analyze_query=False):
    # 读取csv，只读取multilabel和query列
    if analyze_query:
        df = pd.read_csv(csv_path, usecols=["multilabel", "query"])
    else:
        df = pd.read_csv(csv_path, usecols=["multilabel"])

    # 解析multilabel列为list
    def parse_label(x):
        # 兼容有空格的情况
        return ast.literal_eval(x.strip())

    df["multilabel_list"] = df["multilabel"].apply(parse_label)

    # print(df['multilabel_list'])

    # # 找位置
    # positions = df.index[df['multilabel_list'].apply(lambda x: sum(x) == 1)]
    # print(positions)

    # 统计单根因和多根因
    single_count = df["multilabel_list"].apply(lambda x: sum(x) == 1).sum()
    multi_count = df["multilabel_list"].apply(lambda x: sum(x) > 1).sum()

    # 统计每个根因的慢SQL数量
    multilabel_matrix = df["multilabel_list"].tolist()
    import numpy as np

    multilabel_array = np.array(multilabel_matrix)
    root_cause_counter = multilabel_array.sum(axis=0)

    print(f"单根因慢SQL数量: {single_count}")
    print(f"多根因慢SQL数量: {multi_count}")
    print("每个根因对应的慢SQL数量:")
    for idx, count in enumerate(root_cause_counter):
        print(f"根因{idx}: {count}")

    # 检查query列中不包含SELECT的行
    if analyze_query:
        not_select = df[~df["query"].str.contains("SELECT", case=False, na=False)]
        if not not_select.empty:
            print("以下query列不包含SELECT的行:")
        for i, row in not_select.iterrows():
            print(f"行号: {i}, query内容: {row['query']}")
        else:
            print("所有行的query都包含SELECT")


def analyze_duration_distribution(csv_path):
    """
    统计不同根因下SQL的执行时间分布。
    """
    import numpy as np

    # 读取multilabel和duration列
    df = pd.read_csv(csv_path, usecols=["multilabel", "duration"])

    # 解析multilabel列为list
    def parse_label(x):
        return ast.literal_eval(x.strip())

    df["multilabel_list"] = df["multilabel"].apply(parse_label)
    # 转换duration为float
    df["duration"] = pd.to_numeric(df["duration"], errors="coerce")
    # 统计根因数量
    multilabel_matrix = df["multilabel_list"].tolist()
    multilabel_array = np.array(multilabel_matrix)
    num_root_causes = multilabel_array.shape[1]
    print("\n不同根因下SQL执行时间分布:")
    for idx in range(num_root_causes):
        mask = multilabel_array[:, idx] == 1
        durations = df["duration"][mask]
        if len(durations) == 0:
            print(f"根因{idx}: 无数据")
            continue
        print(f"根因{idx}:")
        print(f"  样本数: {len(durations)}")
        print(f"  平均值: {durations.mean():.2f}")
        print(f"  中位数: {durations.median():.2f}")
        print(f"  25分位: {durations.quantile(0.25):.2f}")
        print(f"  75分位: {durations.quantile(0.75):.2f}")
        print(f"  最大值: {durations.max():.2f}")
        print(f"  最小值: {durations.min():.2f}")


def main():
    parser = argparse.ArgumentParser(description="慢SQL根因与执行时间统计工具")
    parser.add_argument("--csv_path", type=str, required=True, help="CSV文件路径")
    parser.add_argument("--analyze_query", action="store_true", help="是否分析query列中是否包含SELECT")
    parser.add_argument("--analyze_root_cause", action="store_true", help="统计根因分布")
    parser.add_argument("--analyze_duration", action="store_true", help="统计根因下SQL执行时间分布")
    args = parser.parse_args()

    if args.analyze_root_cause or args.analyze_query:
        print("\n根因分布统计:")
        analyze_multilabels(args.csv_path, analyze_query=args.analyze_query)
    if args.analyze_duration:
        print("\n执行时间分布统计:")
        analyze_duration_distribution(args.csv_path)


if __name__ == "__main__":
    main()
