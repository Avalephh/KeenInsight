import json

def get_functions_for_knobs(json_file_path, knobs):
    # 打开并加载 JSON 文件
    with open(json_file_path, 'r', encoding='utf-8') as file:
        data = json.load(file)
    
    # 存储结果
    results = []

    # 遍历提供的 knob 列表
    for knob_name in knobs.keys():
        # 在 JSON 数据中找到匹配的 knob 并获取对应的函数
        match_found = False
        for item in data:
            if item.get("knob_name") == knob_name:
                # 提取数据流和控制流函数列表
                data_flow_functions = item.get("data_flow_functions_in_csv", [])
                control_flow_functions = item.get("control_flow_functions_in_csv", [])
                
                # 构造输出格式
                result = {
                    "knob": knob_name,
                    "data_flow_functions": data_flow_functions,
                    "control_flow_functions": control_flow_functions
                }
                results.append(result)
                match_found = True
                break

        # 如果没有找到匹配的 knob，返回空的函数列表
        if not match_found:
            results.append({
                "knob": knob_name,
                "data_flow_functions": [],
                "control_flow_functions": []
            })

    return results

# # 使用 JSON 文件路径和 knob 名称调用函数
# json_file_path = '/root/AI4DB/Experiment/test/148_default_matched_functions.json'
# knobs = {'tmp_table_size': 9223372036854776319, 'max_heap_table_size': 9223372036854783488, 'big_tables': 0}
# results = get_functions_for_knobs(json_file_path, knobs)

# # 输出结果
# for result in results:
#     print(f"knob: {result['knob']}")
#     print(f"data_flow_functions: {result['data_flow_functions']}")
#     print(f"control_flow_functions: {result['control_flow_functions']}\n")