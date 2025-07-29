import logging
import openai
from typing import List, Dict
import re
import os
import datetime
import json
from .function_find import find_function_location, extract_function_from_file

openai.api_key = "sk-t01IzeeXG4BKiQUtAbA954A63eE942E38eEe7fF08fF3Af6c"

openai.base_url = "https://api.gpt.ge/v1/"
openai.default_headers = {"x-foo": "true"}

class SimpleParameterAnalyzer:
    def __init__(self):
        self.max_retries = 5
        self._setup_logger()
        self._init_prompt()
        self._setup_cache_dir()

    def _setup_logger(self):
        # 创建日志目录
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        os.makedirs(log_dir, exist_ok=True)
        
        # 创建带有时间戳的日志文件名
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = os.path.join(log_dir, f"parameter_analyzer_{timestamp}.log")
        
        # 配置日志记录器
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.DEBUG)
        
        # 文件处理器
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        
        # 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        
        # 设置格式
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        # 添加处理器
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    def _setup_cache_dir(self):
        self.cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "analysis_cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.logger.info(f"缓存目录: {self.cache_dir}")

    def _get_cache_filename(self, param_name, function_name=None):
        if function_name:
            return os.path.join(self.cache_dir, f"{param_name.lower()}_{function_name.lower()}_analysis.json")
        else:
            return os.path.join(self.cache_dir, f"{param_name.lower()}_analysis.json")


    def _check_cache(self, param_name, function_name=None):
        cache_file = self._get_cache_filename(param_name, function_name)
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                    self.logger.info(f"从缓存加载参数 {param_name} 和函数 {function_name} 的分析结果")
                    return cache_data.get("analysis")
            except Exception as e:
                self.logger.error(f"读取缓存失败: {str(e)}")
        return None

    def _save_to_cache(self, param_name, analysis_result, code_snippets, function_name=None):
        cache_file = self._get_cache_filename(param_name, function_name)
        try:
            cache_data = {
                "param_name": param_name,
                "function_name": function_name,
                "analysis": analysis_result,
                "code_snippets": code_snippets,
                "timestamp": datetime.datetime.now().isoformat()
            }
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            self.logger.info(f"参数 {param_name} 和函数 {function_name} 的分析结果已保存到缓存")
        except Exception as e:
            self.logger.error(f"保存缓存失败: {str(e)}")

    def extract_instructions_by_param(self, param_name, function_name=None, base_path="/root/sysinsight-main/controlpath"):  # hzttodo
        #print("name is" , param_name, function_name)
        # 首先检查缓存
        cached_result = self._check_cache(param_name, function_name)
        if cached_result is not None:
            return cached_result
        
        # 查找以指定参数名开头的文件
        target_file = None
        for filename in os.listdir(base_path):
            # 检查文件名是否以参数名开头
            if filename.lower().startswith(param_name.lower()):
                file_path = os.path.join(base_path, filename)
                if os.path.isfile(file_path):
                    target_file = file_path
                    self.logger.debug(f"找到匹配的文件: {filename}")
                    break

        if target_file is None and param_name.lower().startswith('srv'):
            fallback_param_name = param_name.lower().replace('srv', 'innodb', 1)
            self.logger.debug(f"未找到以 '{param_name}' 开头的文件，尝试使用 '{fallback_param_name}' 查找")
            
            for filename in os.listdir(base_path):
                if filename.lower().startswith(fallback_param_name):
                    file_path = os.path.join(base_path, filename)
                    if os.path.isfile(file_path):
                        target_file = file_path
                        self.logger.debug(f"使用备用名称找到匹配的文件: {filename}")
                        break


        check_data_flow = True
        file_path = target_file

        if function_name is not None and target_file is not None:
            with open(file_path, 'r', encoding='utf-8') as file:
                content = file.read()
            
            # 查找位置并判断
            data_flow_pos = content.find("Explicit Data Flow")
            control_flow_pos = content.find("Explicit Control Flow")
            function_pos = content.find(function_name)

            # 返回布尔值：True表示在Data Flow后面，False表示在Control Flow后面
            check_data_flow = data_flow_pos > control_flow_pos if (data_flow_pos < function_pos and control_flow_pos < function_pos) else data_flow_pos < function_pos

        function_code = ""

        if not check_data_flow:
            
            def find_function_caller(file_path, target_function):
             
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                except FileNotFoundError:
                    print(f"错误: 找不到文件 {file_path}")
                    return None
                except Exception as e:
                    print(f"错误: 读取文件时出错 - {e}")
                    return None
                
                lines = content.strip().split('\n')
                
                # 找到所有目标函数的位置
                target_positions = []
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped == target_function:
                        target_positions.append(i)
                
                if not target_positions:
                    print(f"未找到函数: {target_function}")
                    return None

                # 存储所有找到的调用者
                callers = []
                
                 # 对每个目标函数位置都查找调用者
                for target_pos in target_positions:
                    target_indent = len(lines[target_pos]) - len(lines[target_pos].lstrip())
                    
                    # 向上查找缩进更少的函数（调用者）
                    for i in range(target_pos - 1, -1, -1):
                        line = lines[i]
                        current_indent = len(line) - len(line.lstrip())
                        stripped = line.strip()
                        
                        # 如果找到缩进更少且非空的行，这就是调用者
                        if stripped and current_indent < target_indent:
                            if stripped not in callers:  # 避免重复
                                callers.append(stripped)
                            break
                
                return callers if callers else None

            up_functions = find_function_caller("/root/deatails-records.dat", function_name) # hzttodo
            
            if up_functions:
                for up_function in up_functions:
                    file_path, line_number = find_function_location(up_function, "/root/postgresql-analysis/xml") # hzttodo
                    function_code += extract_function_from_file(file_path, line_number)
 
        file_path, line_number = find_function_location(function_name, "/root/postgresql-analysis/xml") # hzttodo
        function_code += extract_function_from_file(file_path, line_number)
        
        # 分析结果
        result = self.analyze(function_name, function_code, [param_name])
        self._save_to_cache(param_name, result, function_code, function_name)
        
        return result

    def _init_prompt(self):
        self.analysis_prompt = """你是一个专业的数据库性能优化专家。请分析以下MySQL参数是如何直接影响代码执行的：


需要分析的关键参数：
{parameters}

需要重点分析影响的关键函数：
{key_func}

关键参数涉及的函数片段：
{code_snippets}



请按照以下格式进行分析，每个部分输出前必须输出我用<>号指定的标记：

<需要提供函数>
除了现在给出的函数片段，是否还需要给出其他函数的具体实现，如果有，则输出函数名

<思考过程>
1. 参数通过控制关键函数对数据库性能的影响：
    - {parameters} 通过 [机制] 影响 {key_func}，进而产生[数据库性能影响]
    - 机制：简要描述 {parameters} 值的变化如何改变函数的行为
    - 数据库性能影响：描述这种改变对数据库性能的具体影响

<火焰图采样分析与调优方向>
2. 基于 {key_func}执行状态和相关函数片段给出{parameters} 优化建议：
    - 如果涉及到[其他函数]，请指出除了监控{key_func}之外，是否还需要监控其他函数。
    - 如何根据 {key_func}和[其他函数]的火焰图采样率，推荐{parameters}调整的方向（升高或降低）和依据


要求只能根据给定的代码和参数分析，描述具体、精确，避免笼统的说法，帮助另外的agent精确理解如何根据{key_func}的执行表现，
通过调节{parameters} 的设置，优化数据库的性能表现，请注意格式，不要有多余输出。不要用markdown格式"""

    def _validate_response_format(self, response: str) -> Dict[str, bool]:
        """验证响应是否包含必需的格式部分"""
        required_sections = {
            "需要提供函数": False,
            "思考过程": False,
            "火焰图采样分析与调优方向": False
        }
        
        for section in required_sections.keys():
            if section in response:
                required_sections[section] = True
        
        return required_sections

    def _extract_monitor_functions(self, response: str) -> List[str]:
        """从响应中提取建议监控的函数名"""
        functions = []
        
        # 查找建议监控函数部分
        pattern = r"需要提供函数\s*(.*?)\s*思考过程"
        match = re.search(pattern, response, re.DOTALL)
        if match:
            content = match.group(1).strip()
            # 使用正则表达式提取连续的非汉字串（函数名通常是英文、数字、下划线）
            func_names = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', content)
            
            # 去重并添加到函数列表
            for func_name in func_names:
                if func_name and func_name not in functions:
                    functions.append(func_name)

        return functions

    def _get_function_codes(self, function_names: List[str], old_func) -> str:
        """获取监控函数的代码片段"""
        function_codes = ""
        
        for func_name in function_names:
            try:
                file_path, line_number = find_function_location(func_name, "/root/postgresql-analysis/xml") # hzttodo
                if file_path and line_number:
                    code = extract_function_from_file(file_path, line_number)

                    if old_func and (code in old_func):
                        continue  # 跳过已存在的代码

                    function_codes += f"\n\n=== {func_name} ===\n{code}\n"
                    self.logger.info(f"成功获取函数 {func_name} 的代码")
                else:
                    self.logger.warning(f"未找到函数 {func_name} 的位置")
            except Exception as e:
                self.logger.error(f"获取函数 {func_name} 代码时出错: {str(e)}")
        
        return function_codes

    def analyze(self, key_func, code_snippets, parameters: List[str]) -> str:

        formatted_params = "\n".join([f"{param}" for param in parameters])
        
        prompt = self.analysis_prompt.format(
            code_snippets=code_snippets,
            parameters=formatted_params,
            key_func=str(key_func)
        )

        self.logger.debug(f"prompt: {prompt}")

        result = ""
        
        # 重试机制
        for attempt in range(self.max_retries):
            try:
                self.logger.info(f"第 {attempt + 1} 次尝试分析...")
                
                response = openai.ChatCompletion.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": "你是一个专业的数据库性能优化专家，请严格按照要求的格式进行分析。"},
                        {"role": "user", "content": prompt}
                    ]
                )
                
                result = response.choices[0].message.content
            
                # 验证响应格式
                validation_result = self._validate_response_format(result)
                missing_sections = [section for section, present in validation_result.items() if not present]
                
                if missing_sections:
                    continue
                
                self.logger.info("响应格式验证通过")
                monitor_functions = self._extract_monitor_functions(result)
                if monitor_functions:
                    self.logger.info(f"提取到监控函数: {monitor_functions}")
                    
                    # 获取监控函数的代码
                    function_codes = self._get_function_codes(monitor_functions, code_snippets)
                    
                    if function_codes:
                        code_snippets += function_codes
                        prompt = self.analysis_prompt.format(
                            code_snippets=code_snippets,
                            parameters=formatted_params,
                            key_func=str(key_func)
                        )

                    else:
                        return result
                        
                    continue
                    
                return result
                    
            except Exception as e:
                self.logger.error(f"第 {attempt + 1} 次分析失败: {str(e)}")
                if attempt == self.max_retries - 1:
                    raise

        return result

    def extract_function_from_location(self, location_str: str) -> str:
        """
        从给定的字符串中提取文件路径和行列信息，并返回对应位置的整个函数代码。
        
        Args:
            location_str: 包含文件路径和行列信息的字符串
        
        Returns:
            str: 提取出的函数代码
        """
        import re

        # 使用正则表达式提取文件路径、行和列
        match = re.search(r'Location: (.+):(\d+):(\d+)', location_str)
        if not match:
            self.logger.error("无法从字符串中提取位置")
            return ""

        file_path, line, column = match.groups()
        line, column = int(line), int(column)

        self.logger.debug(f"提取到的文件路径: {file_path}, 行: {line}, 列: {column}")

        try:
            # 修改文件路径
            modified_file_path = file_path.replace("/root/LLVM/mysql-8.0.36", "/root/mysql-8.0.36") # hzttodo
            with open(modified_file_path, 'r') as file:
                file_content = file.read()
                # 删除C风格的块注释 /* ... */
                import re
                file_content = re.sub(r'/\*.*?\*/', '', file_content, flags=re.DOTALL)
                # 删除C++风格的行注释 //
                file_content = re.sub(r'//.*?$', '', file_content, flags=re.MULTILINE)
                lines = file_content.splitlines(True)

            # 从文件第一行开始，利用匹配的{}组成的栈获得文件所有的函数的开始和结束的行数
            stack = []
            function_ranges = []

            for i, line_content in enumerate(lines):
                # 处理一行中包含多个 '{' 或 '}' 的情况
                open_braces = line_content.count('{')
                close_braces = line_content.count('}')
                
                # 将所有的 '{' 的位置入栈
                for _ in range(open_braces):
                    stack.append(i)
                
                # 处理所有的 '}'，并在栈为空时记录函数范围
                for _ in range(close_braces):
                    if stack:
                        start_position = stack.pop()
                
                        if not stack:
                            function_ranges.append((start_position, i))

            # 查找包含指定行的函数范围
            self.logger.debug(f"函数范围: {function_ranges}")
            start_line, end_line = None, None
            for start, end in function_ranges:
                if start <= line - 1 <= end:
                    start_line, end_line = start, end
                    break

            if start_line is not None and end_line is not None:
                extracted_code = ''.join(lines[start_line - 1:end_line + 1])
                self.logger.debug(f"提取到的函数代码长度: {len(extracted_code)}")
                return extracted_code
            else:
                self.logger.error("未能找到包含指定行的函数", start_line, end_line, function_ranges)
                return ""

        except Exception as e:
            self.logger.error(f"无法打开文件或读取内容: {str(e)}")
            return ""

def main():
    # 测试代码
    analyzer = SimpleParameterAnalyzer()
    
    result = analyzer.extract_instructions_by_param("srv_spin_wait_delay", 'ut_delay')
    print("\n分析结果:")
    print(result)

if __name__ == "__main__":
    main() 
