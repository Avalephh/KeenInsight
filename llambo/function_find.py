import xml.etree.ElementTree as ET
import os
from typing import Optional, Tuple

def find_function_location(function_name: str, xml_path: str) -> Optional[Tuple[str, int]]:
    """
    在XML文件中查找函数的实现位置
    
    Args:
        function_name (str): 要查找的函数名
        xml_path (str): XML文件路径（文件或目录）
    
    Returns:
        Tuple[str, int] or None: (文件名, 行号) 如果找到，否则返回None
    """
    
    def get_xml_files(path: str) -> list:
        """获取所有XML文件"""
        if os.path.isfile(path) and path.endswith('.xml'):
            return [path]
        elif os.path.isdir(path):
            xml_files = []
            for root, dirs, files in os.walk(path):
                for file in files:
                    if file.endswith('.xml'):
                        xml_files.append(os.path.join(root, file))
            return xml_files
        return []

    def file_contains_function_name(xml_file: str, function_name: str) -> bool:
        """快速检查文件是否包含函数名字符串"""
        try:
            with open(xml_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                if function_name is not None:
                    return function_name in content
                return False
        except (FileNotFoundError, IOError):
            return False
    
    def parse_xml_file(xml_file: str) -> Optional[Tuple[str, int]]:
        """解析单个XML文件查找函数"""
        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()
            
            # 查找所有函数定义
            for memberdef in root.findall(".//memberdef[@kind='function']"):
                name_elem = memberdef.find("name")
                if name_elem is not None and name_elem.text == function_name:
                    # 找到函数，获取位置信息
                    location_elem = memberdef.find("location")
                    if location_elem is not None:
                        file_path = location_elem.get('bodyfile') or location_elem.get('file')
                        line_num = location_elem.get('bodystart') or location_elem.get('line')
                        
                        if file_path and line_num:
                            try:
                                return (file_path, int(line_num))
                            except ValueError:
                                return None, None
            
        except (ET.ParseError, FileNotFoundError):
            return None, None
        
        return None, None
    
    # 获取所有XML文件并逐个搜索
    xml_files = get_xml_files(xml_path)
    
    for xml_file in xml_files:
        if not file_contains_function_name(xml_file, function_name):
            continue
        result1, result2 = parse_xml_file(xml_file)
        if result1 is not None:
            return result1, result2
    
    return None, None

def extract_function_from_file(file_path: str, line_number: int) -> Optional[str]:
    if file_path is None:
        return ""
    try:
        # 读取文件
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        
        start_line = line_number - 1  # 转换为0基索引
        if start_line >= len(lines):
            return None
        
        # 从指定行开始查找函数
        function_lines = []
        brace_count = 0
        found_opening_brace = False
        
        for i in range(start_line, len(lines)):
            line = lines[i]
            function_lines.append(line)
            
            # 计算大括号
            for char in line:
                if char == '{':
                    brace_count += 1
                    found_opening_brace = True
                elif char == '}':
                    brace_count -= 1
            
            # 如果找到了开始的大括号，并且括号匹配完成，函数结束
            if found_opening_brace and brace_count == 0:
                break
        
        # 返回完整函数代码
        return ''.join(function_lines).rstrip()
    
    except (FileNotFoundError, IOError):
        return None

# 使用示例
if __name__ == "__main__":
    # 查找ut_delay函数
    result = find_function_location("log_buffer_s_lock_wait", "/root/postgresql-analysis/xml")
    if result:
        file_path, line_number = result
        print(f"{file_path}:{line_number}")
        function_code = extract_function_from_file(file_path, line_number)
        print(function_code)
    else:
        print("Function not found")
