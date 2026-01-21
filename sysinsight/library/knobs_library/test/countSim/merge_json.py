import json
import os

# 读取 JSON 文件并合并它们
def merge_json_files(file_paths):
    os.chdir('/root/LLVM/ConfTainter/src/test/countSim/result_job')
    merged_data = []
    for file_path in file_paths:
        if file_path.endswith('.json'):
            with open(file_path, 'r') as file:
                data = json.load(file)
                merged_data.extend(data)
    return merged_data

# JSON 文件路径
file_paths = os.listdir('/root/LLVM/ConfTainter/src/test/countSim/result_job')

# 合并 JSON 数据
merged_data = merge_json_files(file_paths)

# 将合并后的数据写入新的 JSON 文件
with open('merged_job.json', 'w') as outfile:
    json.dump(merged_data, outfile, indent=4)