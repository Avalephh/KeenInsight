# 统计正常情况下各函数的占比
import os
import pandas as pd

# 输入和输出路径
input_folder = "/root/sysinsight-main/perf_data"  # 替换为你的txt文件所在目录
output_file = "function_normal.csv"

# 存储所有文件的函数信息
functions_data = {}

# 处理每个文件
for filename in os.listdir(input_folder):
    if filename.endswith(".txt"):
        filepath = os.path.join(input_folder, filename)
        with open(filepath, "r") as file:
            for line in file.readlines():
                # 跳过文件头
                if "Cycles" in line or not line.strip():
                    continue
                
                # 分割字段
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                
                function = parts[1].strip()  # 函数名
                sampling_rate = float(parts[2].strip().replace("%", ""))  # 采样率
                
                # 将采样率添加到对应函数的列表
                if function not in functions_data:
                    functions_data[function] = []
                functions_data[function].append(sampling_rate)

# 统计范围和平均值
results = []
for function, rates in functions_data.items():
    min_rate = min(rates)
    max_rate = max(rates)
    avg_rate = sum(rates) / len(rates)
    results.append({"Function": function, "Min Sampling Rate (%)": min_rate, 
                    "Max Sampling Rate (%)": max_rate, "Average Sampling Rate (%)": avg_rate})

# 转换为 DataFrame 并保存为 CSV
df = pd.DataFrame(results)
print(df)
df.sort_values(by="Average Sampling Rate (%)", ascending=False, inplace=True)  # 按平均值排序
df.to_csv(output_file, index=False)

print(f"the result save to {output_file}")
