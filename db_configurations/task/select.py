# # 提取min+max/2
# import json

# # 读取 JSON 文件
# with open("/root/sysinsight-main/DBTuner/knobspace/gptuner_target_knobs.json", "r") as f:
#     data = json.load(f)

# # 更新 start_value
# for key, value in data.items():
#     value["start_value"] = (value["min"] + value["max"]) // 2
#     print(value["start_value"])


# # 写回 JSON 文件
# with open("/root/sysinsight-main/DBTuner/knobspace/mysql_knobs.json", "w") as f:
#     json.dump(data, f, indent=4)

# print("Updated JSON saved as config_updated.json")

# 记录初始值
import json

# 读取 selected_knobs.json 文件
with open("/root/sysinsight-main/DBTuner/knobspace/mysql_knobs.json", "r") as file:
    selected_knobs = json.load(file)

# 提取 start_value 的值并转换为所需格式
start_values = [{}]
for knob, details in selected_knobs.items():
    start_value = details.get("default")
    if start_value is not None:
        start_values[0][knob] = start_value

# 保存到新的 JSON 文件
with open("gptuner_default_values.json", "w") as outfile:
    json.dump(start_values, outfile, indent=4)

print("已成功提取 start_value 并保存到 'init_start_values.json' 文件中。")

# import json

# # 读取 selected_knobs.json 文件
# with open("/root/sysinsight-main/DBTuner/knobspace/mysql_knobs.json", "r") as file:
#     selected_knobs = json.load(file)

# # 创建目标格式字典
# formatted_knobs = {"MySQL_Parameters": {}}

# # 遍历参数并根据类型转换格式
# for knob, details in selected_knobs.items():
#     knob_type = details.get("type")
#     if knob_type == "integer":
#         # 连续型参数
#         min_val = details.get("min", 0)
#         max_val = details.get("max", 0)
#         formatted_knobs["MySQL_Parameters"][knob] = ["int", "linear", [min_val, max_val]]
#     elif knob_type == "enum":
#         # 离散型参数
#         enum_values = details.get("enum_values", [])
#         formatted_knobs["MySQL_Parameters"][knob] = ["enum", "linear", enum_values]

# # 保存到新的 JSON 文件
# with open("formatted_gptuner_knobs.json", "w") as outfile:
#     json.dump(formatted_knobs, outfile, indent=4)

# print("已成功转换参数并保存到 'formatted_selected_knobs.json' 文件中。")