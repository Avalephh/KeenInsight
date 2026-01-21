import json

# 读取JSON文件
def read_json(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)

# 遍历JSON并修改knob_name
def modify_knob_names(data):
    if isinstance(data, dict):
        for key, value in data.items():
            if key == 'knob_name':
                # 修改含有 'srv' 或 'buf' 的值
                if 'srv' in value:
                    data[key] = value.replace('srv', 'innodb')
                if 'buf' in value and 'buffer' not in value:
                    data[key] = value.replace('buf', 'buffer')
            else:
                modify_knob_names(value)
    elif isinstance(data, list):
        for item in data:
            modify_knob_names(item)

# 写入修改后的JSON文件
def write_json(data, file_path):
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# 主函数
def main():
    #这个改完还得手动改
    file_path = '/root/LLVM/ConfTainter/src/test/func_match/function_all.json'  # 替换为你的JSON文件路径
    json_data = read_json(file_path)
    
    # 修改knob_name
    modify_knob_names(json_data)
    
    # 输出修改后的JSON到新文件
    output_file = 'modified_knob_names.json'
    write_json(json_data, output_file)
    print(f"修改后的JSON文件已保存为: {output_file}")

if __name__ == '__main__':
    main()