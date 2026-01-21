import json
import numpy as np
import matplotlib.pyplot as plt

# 读取 sys-eye 数据
def load_json_sys_eye(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data

# 提取 sys-eye TPS 数据
def extract_lat_sys_eye(data):
    return [entry["external_metrics"]["lat"] for entry in data if "external_metrics" in entry and "lat" in entry["external_metrics"]]

# 文件路径
file_sys_eye_1 = "/root/sysinsight-main/rule_collect_results_tpch_notbetter.json"
file_sys_eye_2 = "/root/sysinsight-main/rule_collect_results_tpch-better.json"
# file_sys_eye_3 = "/root/sysinsight-main/rule_collect_results_sysbench_mysql_diff.json"

# 解析 JSON 数据
data_sys_eye_1 = load_json_sys_eye(file_sys_eye_1)
data_sys_eye_2 = load_json_sys_eye(file_sys_eye_2)
# data_sys_eye_3 = load_json_sys_eye(file_sys_eye_3)

# 提取 sys-eye TPS 数据
lat_sys_eye_1 = extract_lat_sys_eye(data_sys_eye_1)
lat_sys_eye_2 = extract_lat_sys_eye(data_sys_eye_2)
# lat_sys_eye_3 = extract_lat_sys_eye(data_sys_eye_3)

sys_eye_indices_1 = list(range(1, len(lat_sys_eye_1) + 1))
sys_eye_indices_2 = list(range(1, len(lat_sys_eye_2) + 1))
# sys_eye_indices_3 = list(range(1, len(lat_sys_eye_3) + 1))

# **计算累积最小值**
min_lat_sys_eye_1 = np.minimum.accumulate(lat_sys_eye_1)
min_lat_sys_eye_2 = np.minimum.accumulate(lat_sys_eye_2)
# min_lat_sys_eye_3 = np.maximum.accumulate(lat_sys_eye_3)

# **绘制图像**
plt.figure(figsize=(12, 6))

# sys-eye 1
plt.scatter(sys_eye_indices_1, lat_sys_eye_1, alpha=0.3, color="C0", label="Before optimization(samples)")
plt.plot(sys_eye_indices_1, min_lat_sys_eye_1, color="C0", label="Before optimization(Convergence)")

# sys-eye 2
plt.scatter(sys_eye_indices_2, lat_sys_eye_2, alpha=0.3, color="C1", label="After optimization (samples)")
plt.plot(sys_eye_indices_2, min_lat_sys_eye_2, color="C1", label="After optimization (Convergence)")

# sys-eye 3
# plt.scatter(sys_eye_indices_3, lat_sys_eye_3, alpha=0.3, color="C2", label="Diff + Rule (samples)")
# plt.plot(sys_eye_indices_3, min_lat_sys_eye_3, color="C2", label="Diff + Rule (Convergence)")

# 设置图例、标题、轴标签
plt.xlabel("Number of iterations", fontsize=14)
plt.ylabel("Total RunTime", fontsize=14)
plt.title("Total RunTime Convergence Comparison (sys-eye)", fontsize=16, fontweight="bold")
plt.legend(fontsize=10, loc="upper right")
plt.grid(True, linestyle="--", alpha=0.6)

# **显示图像**
plt.savefig("sys_eye_prompt_tpch_comparison.png", dpi=300, bbox_inches="tight")
plt.show()

#  #########################################柱状图####################################

# import json
# import numpy as np
# import matplotlib.pyplot as plt

# # 读取 sys-eye 数据
# def load_json_sys_eye(file_path):
#     with open(file_path, "r", encoding="utf-8") as f:
#         data = json.load(f)
#     return data

# # 提取 sys-eye TPS 数据
# def extract_lat_sys_eye(data):
#     return [entry["external_metrics"]["lat"] for entry in data if "external_metrics" in entry and "lat" in entry["external_metrics"]]

# # 文件路径
# file_sys_eye_1 = "/root/sysinsight-main/rule_collect_results_tpch_runtime_random_2.json"
# file_sys_eye_2 = "/root/sysinsight-main/rule_collect_results_tpch_runtime_gpt4o.json"

# # 解析 JSON 数据
# data_sys_eye_1 = load_json_sys_eye(file_sys_eye_1)
# data_sys_eye_2 = load_json_sys_eye(file_sys_eye_2)

# # 提取 TPS 数据
# lat_sys_eye_1 = extract_lat_sys_eye(data_sys_eye_1)
# lat_sys_eye_2 = extract_lat_sys_eye(data_sys_eye_2)

# # 计算累积最小值
# min_lat_sys_eye_1 = np.minimum.accumulate(lat_sys_eye_1)
# min_lat_sys_eye_2 = np.minimum.accumulate(lat_sys_eye_2)

# # **第一组、第二组、最小值**
# performance_first = [lat_sys_eye_1[0], lat_sys_eye_2[0]]
# performance_second = [lat_sys_eye_1[1], lat_sys_eye_2[1]]
# performance_best = [np.min(min_lat_sys_eye_1), np.min(min_lat_sys_eye_2)]

# # **绘制柱状图**
# labels = ["GPT-4o-mini", "GPT-4o"]
# x = np.arange(len(labels))
# width = 0.25  

# fig, ax = plt.subplots(figsize=(8, 6))

# bars1 = ax.bar(x - width, performance_first, width, label="Default Data", color="C0", alpha=0.8)
# bars2 = ax.bar(x, performance_second, width, label="First Tune Data", color="C1", alpha=0.8)
# bars3 = ax.bar(x + width, performance_best, width, label="Best Min Latency", color="C2", alpha=0.8)

# # **标签和标题**
# ax.set_xlabel("Experiments", fontsize=14)
# ax.set_ylabel("Total RunTime", fontsize=14)
# ax.set_title("Total RunTime Comparison of sys-eye Experiments", fontsize=16, fontweight="bold")
# ax.set_xticks(x)
# ax.set_xticklabels(labels, fontsize=12)
# ax.legend(loc="lower right")

# # **数值标注**
# for bars in [bars1, bars2, bars3]:
#     for bar in bars:
#         height = bar.get_height()
#         ax.annotate(f"{height:.2f}", xy=(bar.get_x() + bar.get_width() / 2, height),
#                     xytext=(0, 5), textcoords="offset points", ha='center', fontsize=10)

# ax.yaxis.grid(True, linestyle="--", alpha=0.6)

# plt.savefig("sys_eye_lat_api_bar.png", dpi=300, bbox_inches="tight")
# plt.show()