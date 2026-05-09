from __future__ import annotations
import pandas as pd
import json


class DifferentialProfiling:
    """差分剖析器（MySQL + OLTP 专用版本）"""

    def _compare_functions(self, abnormal_file: str, baseline_file: str):
        """函数级差分分析"""
        file_data = pd.read_csv(abnormal_file, sep='\t')
        normal_data = pd.read_csv(baseline_file)

        file_data['Sampling Rate (%)'] = file_data['Sampling Rate (%)'].str.replace('%', '').astype(float)
        normal_data['Min Sampling Rate (%)'] = normal_data['Min Sampling Rate (%)'].astype(float)
        normal_data['Max Sampling Rate (%)'] = normal_data['Max Sampling Rate (%)'].astype(float)
        normal_data['Average Sampling Rate (%)'] = normal_data['Average Sampling Rate (%)'].astype(float)

        outliers = []

        for _, row in file_data.iterrows():
            func = row['Function']
            sample_rate = row['Sampling Rate (%)']

            ref = normal_data[normal_data['Function'] == func]
            if ref.empty:
                continue

            min_v = ref['Min Sampling Rate (%)'].values[0]
            max_v = ref['Max Sampling Rate (%)'].values[0]
            mean_v = ref['Average Sampling Rate (%)'].values[0]
            
            epsilon = 0.05  # 5% 阈值
            if sample_rate < min_v*(1-epsilon) or sample_rate > max_v*(1+epsilon):
            # if sample_rate < min_v or sample_rate > max_v:
                diff = abs(sample_rate - mean_v)
                change = 1 if sample_rate > mean_v else 0

                outliers.append({
                    "function_name": func,
                    "sample_rate": sample_rate,
                    "diff_from_mean": diff,
                    "change": change
                })

        outliers.sort(key=lambda x: x["diff_from_mean"], reverse=True)
        return outliers


    def compare_profiles(
        self,
        baseline_profile: dict[str, object],
        abnormal_profile: dict[str, object],
    ) -> dict[str, object]:
        """
        baseline_profile:
        {
            "function_file": "baseline.csv"
        }

        abnormal_profile:
        {
            "function_file": "abnormal.txt"
        }
        """

        # 函数差分
        function_diff = self._compare_functions(
            abnormal_profile["function_file"],
            baseline_profile["function_file"]
        )

        return {
            "function_diff": function_diff
        }

    def rank_changed_features(
        self, differential_profile: dict[str, object]
    ) -> list[dict[str, object]]:
        """对变化显著的函数排序"""

        function_diff = differential_profile["function_diff"]

        return [
            {
                "function_name": item["function_name"],
                "diff_from_mean": item["diff_from_mean"],
                "change": item["change"] # 1表示增加，0表示减少
            }
            for item in function_diff
        ]
        


if __name__ == "__main__":
    from config import NORMAL_SYSBENCH, PERF_OUTPUT_DIR
    import os

    profiler = DifferentialProfiling()

    baseline_profile = {
        "function_file": NORMAL_SYSBENCH,
    }

    abnormal_profile = {
        "function_file": os.path.join(PERF_OUTPUT_DIR, "perf_1776398000_counts_sysbench.txt"),
    }

    diff = profiler.compare_profiles(baseline_profile, abnormal_profile)

    top_features = profiler.rank_changed_features(diff)

    print(top_features)        
