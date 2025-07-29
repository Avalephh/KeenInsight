import os
import json

# 文件夹路径
code_new_folder = '/root/AI4DB/hzt/library/knobs_library/code_new'  # 目标文件夹
extract_code_folder = '/root/AI4DB/hzt/library/extractCode'  # 另一个包含文件的文件夹

# 步骤1：修改文件名
for filename in os.listdir(code_new_folder):
    new_filename = filename
    if filename.startswith("srv_"):
        new_filename = new_filename.replace("srv", "innodb")
    if "buf" in filename and "buff" not in filename and "buffer" not in filename:
        new_filename = new_filename.replace("buf", "buffer")
    elif "buff" in filename and "buffer" not in filename:
        new_filename = new_filename.replace("buff", "buffer")
    
    if new_filename != filename:
        os.rename(os.path.join(code_new_folder, filename), os.path.join(code_new_folder, new_filename))
        print(f"重命名 {filename} 为 {new_filename}")

# 步骤2：检查并删除不在 JSON 文件中的文件
json_path = '/root/AI4DB/hzt/library/knobs_library/test/func_match/cleaned_knob_names.json'
with open(json_path, 'r') as f:
    # 加载 JSON 列表并提取 knob_name
    knob_data = json.load(f)
    valid_knobs = {item["knob_name"] for item in knob_data}

# 初始化未匹配的参数列表
unmatched_knobs = []

# 遍历 code_new 文件夹并删除无效的文件
for filename in os.listdir(code_new_folder):
    knob_name = filename.split('_code')[0]
    if knob_name not in valid_knobs:
        # 记录未匹配项
        unmatched_knobs.append(knob_name)
        os.remove(os.path.join(code_new_folder, filename))
        print(f"删除无效文件: {filename}")

# 步骤3：检查 extractCode 和 code_new 文件夹中的文件是否有重复
code_new_files = {f for f in os.listdir(code_new_folder) if os.path.isfile(os.path.join(code_new_folder, f))}
extract_code_files = {f for f in os.listdir(extract_code_folder) if os.path.isfile(os.path.join(extract_code_folder, f))}

# 查找唯一文件
unique_files = code_new_files | extract_code_files

# 检查唯一文件数量是否为 118
if len(unique_files) == 118:
    print("检查通过：code_new 中的唯一文件共有118个")
else:
    print(f"检查不通过：code_new 中的唯一文件共有{len(unique_files)}个")

# 输出未匹配的参数
# if unmatched_knobs:
#     print("以下参数没有匹配上并被删除：")
#     for knob in unmatched_knobs:
#         print(knob)
# else:
#     print("所有文件均匹配，无未匹配的参数。")


# 检查 unique_files 中的参数是否出现在 JSON 文件中的 knob_names 列表
missing_in_json = [filename.split('_code')[0] for filename in unique_files if filename.split('_code')[0] not in valid_knobs]

# 输出不在 JSON 中的参数
if missing_in_json:
    print("以下 unique_files 中的参数没有出现在 JSON 中：")
    for knob in missing_in_json:
        if knob != "extractCode.py":
            print(knob)
else:
    print("unique_files 中的所有参数均在 JSON 中出现。")

# # 输出未匹配的参数
# if unmatched_knobs:
#     print("以下参数没有匹配上并被删除：")
#     for knob in unmatched_knobs:
#         print(knob)
# else:
#     print("所有文件均匹配，无未匹配的参数。")