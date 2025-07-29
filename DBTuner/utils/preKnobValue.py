import os
import json

# 文件夹路径
file_path = '/root/AI4DB/hzt/db_configurations/task/mysql_all_197_32G.json'

new_write_path = '/root/AI4DB/hzt/db_configurations/task/init_knob.json'
        
# 读取 JSON 文件
with open(file_path, 'r') as f:
    data = json.load(f)

# 遍历文件中的每个键，计算并添加 startValue
for key, value in data.items():
    print(f"Processing key '{key}'...")
    
    if value.get('type') == 'integer':
        min_value = value.get('min')
        max_value = value.get('max')

        if min_value is not None and max_value is not None:
            # 计算 startValue 并确保不用科学计数法表示
            start_value = (min_value + max_value) // 2  # 使用整数除法避免科学计数法
            value['start_value'] = start_value
        else:
            # 检查缺少 min_value 或 max_value，并输出相关信息
            if min_value is None and max_value is None:
                print(f"Key '{key}' is missing min_value and max_value.")
            elif min_value is None:
                print(f"Key '{key}' is missing min_value.")
            elif max_value is None:
                print(f"Key '{key}' is missing max_value.")
    
    elif value.get('type') == 'enum':
        value['start_value'] = value.get('default')
        
    else :
        print(f"Key '{key}' has unsupported type '{value.get('type')}'.")

# 将修改后的数据写回文件，禁用科学计数法
with open(new_write_path, 'w') as f:
    json.dump(data, f, indent=4)
    
    print(f"Updated {new_write_path}")
