# 获取语料中的参数的结构化知识
import os
import json

def extract_knob_info(base_path):
    """
    提取参数的核心信息
    
    Args:
        base_path: structured_knowledge目录的路径
        
    Returns:
        包含核心参数信息的列表
    """
    normal_dir = os.path.join(base_path, "normal")
    special_dir = os.path.join(base_path, "special")
    
    knob_info_list = []
    
    if not os.path.exists(normal_dir):
        return knob_info_list
    
    for filename in os.listdir(normal_dir):
        if filename.endswith(".json"):
            knob_name = filename.replace(".json", "")
            normal_file = os.path.join(normal_dir, filename)
            
            try:
                with open(normal_file, "r") as f:
                    normal_data = json.load(f)
                
                # 初始化核心信息
                knob_info = {
                    "parameter": knob_name,
                    "min_value": normal_data.get("min_value", ""),
                    "max_value": normal_data.get("max_value", ""),
                    "suggested_values": normal_data.get("suggested_values", []),
                    "special_value": None
                }
                
                # 检查special文件
                special_file = os.path.join(special_dir, filename)
                if os.path.exists(special_file):
                    with open(special_file, "r") as f:
                        special_data = json.load(f)
                    
                    if special_data.get("special_knob", False):
                        knob_info["special_value"] = special_data.get("special_value")
                
                knob_info_list.append(knob_info)
                
            except Exception as e:
                print(f"Error processing {filename}: {e}")
                continue
    
    return knob_info_list

def read_knob_info_from_file(file_path):
    """
    从文件中读取核心信息列表
    
    Args:
        file_path: 文件路径
        
    Returns:
        核心信息列表
    """
    try:
        with open(file_path, "r") as f:
            knob_info_list = json.load(f)
        return knob_info_list
    except Exception as e:
        print(f"Error reading file {file_path}: {e}")
        return []

def match_knob_info(knob_name):
    """
    根据参数名匹配核心信息
    
    Args:
        knob_name: 参数名
        knob_info_list: 核心信息列表
        
    Returns:
        匹配到的核心信息字典或None
    """
    # 从文件中获取
    knob_info_list = read_knob_info_from_file("/home/sysinsight/DBTuner/utils/knob_info_manual.json")
    for knob_info in knob_info_list:
        if knob_info["parameter"] == knob_name:
            return knob_info
    return None

# 使用示例
# if __name__ == "__main__":
    # base_path = "/home/sysinsight/library/knowledge_collection/mysql/structured_knowledge"
    
    # # 提取核心信息
    # knob_info = extract_knob_info(base_path)
    
    # if knob_info:
    #     # 输出JSON
    #     print(json.dumps(knob_info, indent=2, ensure_ascii=False))
        
    #     # 也可以保存到文件
    #     with open("knob_info_manual.json", "w") as f:
    #         json.dump(knob_info, f, indent=2)
    # else:
    #     print("No data found.")
    
    # 测试匹配功能
    # test_knob = "innodb_buffer_pool_size"
    # matched_info = match_knob_info(test_knob, [])
    # if matched_info:
    #     print(f"Matched info for {test_knob}:")
    #     print("Matched info for {test_knob}:",matched_info['min_value'])
    #     print(json.dumps(matched_info, indent=2, ensure_ascii=False))
    # else:
    #     print(f"No matching info found for {test_knob}.")