from datetime import datetime
import os
from DBTuner.utils.matchFunctions import match_knob_functions,read_function_names,read_function_names_with_change,getTopKnob,get_knob_in_keyFunctions,find_top_and_matched_functions
from DBTuner.utils.matchFunctions_shap import getShapFuncKnobs
from DBTuner.utils.extractCode import extract_code_for_knob_from_json
from DBTuner.utils.getRule import get_rules,group_rules_by_knob
from DBTuner.utils.matchRule_revision import searchRule,read_rules_from_file
from DBTuner.utils.getManualKnow import match_knob_info
import time
from .simple_parameter_analyzer import SimpleParameterAnalyzer



class ParameterLibrary:
    def __init__(self,task):

        # 上一轮训练参数
        self.task = task
        self.replacements = None
        self.config = None

        self.keyFunction_file = None
        self.hyperparameters = None
        self.resource = None
        self.store_bkFunctions_list = None
        self.store_updateKnobs = None
        self.store_csv_func_to_knob = None
        self.question_template =  """
As a database parameter tuning expert, you should provide optimization recommendations for the parameter {variable} in MySQL based on the following information:

1. Database Environment:
    - Database kernel: mysql Ver 8.0.36 for Linux on x86_64 (MySQL Community Server - GPL)
    - Hardware configuration: 4 vCPUs and 15 GiB RAM

2. Workload Characteristics:
    {benchmark}
    For the TPC-H workload, there are several key parameters that may need adjustment to optimize performance: set max_parallel_workers = 64, set max_parallel_workers_per_gather = 64.

3. Target Parameter: {variable}

4. The bottleneck functions that are affected by parameters in perf:
    {keyFunction_section}  

5. Relevant Dataflow and Control Dependencies:
    {dataflow_section} 
    
6. The rules extracted from historical data are association rules about parameter changes, function ranges and performance changes:
    {rule_section}
    
7. In the previous round of parameter configuration, the system resource usage was as follows: 
    {resource_usage}
    
8. The recommended values, recommended ranges and recommended mechanisms of the parameters in the manual are as follows:
    {recommendation_section}

9. Optimization Goals:
    {optimization_goal}

Please don't recommend values that appear repeatedly.
"""

     # 在初始化时处理模板中的优化目标
        self.update_optimization_goal()
    
    def update_optimization_goal(self):
        """根据task['anh']更新优化目标"""
        if 'workload' in self.task:
            if self.task['workload'] == 'tpcc':
                optimization_goal = 'Enhance the throughput of the system.'
                benchmark_section = "- Benchmark tool: tpcc;\n - Data scale: 100 w;\n - Concurrent threads: 16 threads;"
            elif self.task['workload'] == 'tpch':
                optimization_goal = 'Improve the query response speed of the system.'
                benchmark_section = "- Benchmark tool: tpch;\n - Data scale: 2G;\n - Concurrent threads: 16 threads;"
            elif self.task['workload'] == 'sysbench':
                optimization_goal = 'Enhance the throughput of the system.Optimize the overall system performance, including CPU and memory utilization.'
                benchmark_section =  "- Benchmark tool: sysbench oltp-read-write;\n - Data scale: 100 tables, 6,000,000 rows each;\n - Concurrent threads: 50 threads;"
            else:
                optimization_goal = 'Enhance the throughput of the system.'  # 默认值
        elif 'workload_type' in self.task:
            if self.task['workload_type'] == 'oltp':
                optimization_goal = 'Enhance the throughput of the system.'
                benchmark_section = "- Benchmark tool: sysbench oltp-read-write;\n - Data scale: 100 tables, 6,000,000 rows each;\n - Concurrent threads: 50 threads;"
            elif self.task['workload_type'] == 'olap':
                optimization_goal = 'Improve the query response speed of the system.'
                benchmark_section = "- Benchmark tool: tpch;\n - Data scale: 2G;\n - Concurrent threads: 16 threads;"
        else:
            optimization_goal = 'Enhance the throughput of the system.'  
        
        if self.task['dbms'] == 'mysql':
            dbms_info = 'mysql  Ver 8.0.36 for Linux on x86_64 (MySQL Community Server - GPL)\n'
        elif self.task['dbms'] == 'postgresql':
            dbms_info = 'psql (PostgreSQL) 14.17\n'
        
        # 更新模板中的优化目标
        self.question_template = self.question_template.replace('{dbms_info}', dbms_info)
        self.question_template = self.question_template.replace('{optimization_goal}', optimization_goal)
        self.question_template = self.question_template.replace('{benchmark}', benchmark_section)
    
    

    
    def change_value(self, config):
        change_config = {}
        # for key, value in config.items():
        #     if key=='big_tables' and value == 1:
        #         change_config[key] = 'ON'
        #     elif key=='big_tables' and value == 0:
        #         change_config[key] = 'OFF'
        #     else:
        #         change_config[key] = int(value)
                
        return config
    
    def make_file(self, str):
        current_time = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        file_name = f"clean_reason_{current_time}.txt"
        output_dir = "./reason_output"  
        os.makedirs(output_dir, exist_ok=True)
        file_path = os.path.join(output_dir, file_name)
        with open(file_path, 'a', encoding='utf-8') as file:
            file.write(str + '\n')
        return file_path
    
    def find_key_for_knob(self, knob):
        for key, params in self.store_csv_func_to_knob.items():
            if knob in params:
                return key
        return None
    
    def fill_placeholders(self, template, replacements):
        
        # 资源信息
        resource_usage=""
        resource = replacements["resource"]
        resource_usage = (
            f"cpu: {resource['cpu']:.2f}, "
            f"avg_read_io: {resource['readIO']:.4f}, "
            f"avg_write_io: {resource['writeIO']:.4f}, "
            f"avg_virtual_memory: {resource['virtualMem']:.2f}, "
            f"avg_physical_memory: {resource['physical']:.2f}, "
            f"buffer_hit_rate: {resource['hit']:.2f}"
        )

        template = template.replace("{resource_usage}", resource_usage)
        
        # 参数信息
        knobs = replacements["variable"]
        # 初始化参数分析器
        analyzer = SimpleParameterAnalyzer()
        # 为每个参数获取对应的总结
        knob_summaries = {}
        for knob in knobs:
            key = self.find_key_for_knob(knob)
            summary = analyzer.extract_instructions_by_param(knob, key)
            knob_summaries[knob] = summary

       
        # 将参数分析结果添加到模板中
        dataflow_analysis = ""
        for knob in knobs:
            if knob in knob_summaries and knob_summaries[knob]:
                dataflow_analysis += f"{knob_summaries[knob]}\n\n"
        
        # TODO no code
        template = template.replace("{dataflow_section}", dataflow_analysis)

        # 将总结添加到模板中
        # keyFunctions = replacements["keyFunction"]
        knobs_str = ", ".join(knobs)
        # TODO no knob
        template = template.replace("{variable}", knobs_str)
        # template = template.replace("{keyFunction}", keyFunctions_str)


        dataflow = replacements["dataflow"]
        uKnobs = replacements["uKnobs"]

        # Build the new sections
        dataflow_section = ""
        code_section = ""
        for entry in uKnobs:
            knob_name = entry["knob_name"]
            data_flows = ", ".join(entry["data_flow_functions"])
            control_flows = ", ".join(entry["control_flow_functions"])
            if data_flows == "":
                dataflow_section += f"Parameters {knob_name} affect control flow function {control_flows};\n"
            elif control_flows == "":
                dataflow_section += f"Parameters {knob_name} affect data flow function {data_flows};\n"        
            else:
                dataflow_section += f"Parameters {knob_name} affect the function for data flow {data_flows}, control flow function {control_flows};\n"

        # TODO no code
        template = template.replace("{dataflow_section}", dataflow_section.strip())
        # template = template.replace("{code_section}", code_section.strip())
        
        # 函数信息
        keyFunction_section = ""
        bottleneck_functions = replacements["keyFunction"]
        for entry in bottleneck_functions:
            function_name = entry[0]
            function_rate = entry[1]
            change = entry[3]
            if(change == 0):
                keyFunction_section += f"The sampling rate of the bottleneck function {function_name} is {function_rate}, which is higher than the sampling rate of the default function;\n"
            else:
                keyFunction_section += f"The sampling rate of the bottleneck function {function_name} is {function_rate}, which is lower than the sampling rate of the default function;\n"
        
        # TODO no function
        template = template.replace("{keyFunction_section}", keyFunction_section.strip())
        
        # TODO: 历史数据规则
        rule_section = ""
        searchRule = replacements.get("searchRule", [])
        rulebase_list = replacements.get("rulebase", [])
        # print("rulebase_list: ", rulebase_list)
        if searchRule and rulebase_list:
            # # 开始构建规则段
            rule_section += f"Based on the rules obtained from the historical data, you are advised to adjust the following: \n "
            
            for ajustKnobs in rulebase_list:
                for config, value in ajustKnobs.items():
                    rule_section += f"Adjust parameter {config} to {value}\n"
        else:
            rule_section += f"No rules matched to historical data."
                

        # # TODO no rule
        template = template.replace("{rule_section}", rule_section.strip())
        
        # TODO 语料信息
        recommendation_section = ""
        # 从文件中找到参数对应的推荐值等信息
        for knob in knobs:
            print(knob)
            knob_info = match_knob_info(knob)
            if knob_info:
                recommendation_section += f"Parameter: {knob}\n"
                recommendation_section += f"  - Recommended Values: {', '.join(map(str, knob_info.get('suggested_values', [])))}\n"
                recommendation_section += f"  - Recommended Range: [{knob_info.get('min_value', '')}, {knob_info.get('max_value', '')}]\n"
                if knob_info.get('special_value') is not None:
                    recommendation_section += f"  - Special Value: {knob_info.get('special_value')}\n"
                recommendation_section += "\n"
        
        template = template.replace("{recommendation_section}", recommendation_section.strip())
        
        
        
        print("88988********************************************************************************\n")
        print(template)
        print("********************************************************************************\n")
        return template
    

    def get_prompt(self):
        if self.config != None:
            question = self.fill_placeholders(self.question_template, self.replacements)
            question_with_brackets = question.replace("{", "<hzt<").replace("}", ">hzt>")
            return question_with_brackets
        else:  
            return self.question_template.replace("{", "<hzt<").replace("}", ">hzt>")


    def update(self):

        change_config = self.change_value(self.config)
        staticFile = "/home/sysinsight/DBTuner/utils/paramater_association_library.json"
        codeFoder = '/home/sysinsight/library/extractCode'
        
        # 2024.11.30
        bkFunctions_list, updateKnobs, csv_func_to_knob = find_top_and_matched_functions(self.keyFunction_file, staticFile)
        updateKnobs_names = [item['knob_name'] for item in updateKnobs]
        updateKnobs_names = list(set(updateKnobs_names))
        
        
        # shap
        # res_file_path = '/root/sysinsight-main/rule_collect_results_tpcc.json'
        # bkFunctions_list_shap, updateKnobs_shap = getShapFuncKnobs(staticFile,self.keyFunction_file, res_file_path)
        # print("ininin: ",updateKnobs_shap)
        # for item in updateKnobs_shap:
        #     knob_name = item.get('knob_name')
        #     if knob_name and knob_name not in updateKnobs_names:
        #         updateKnobs_names.append(knob_name)
                
        # bkFunctions_list += bkFunctions_list_shap
        # bkFunctions_list = list(set(bkFunctions_list))
        # print(bkFunctions_list)
        
        self.store_bkFunctions_list = bkFunctions_list
        self.store_updateKnobs = updateKnobs
        self.store_csv_func_to_knob = csv_func_to_knob
        
        # TODO: 接入规则 2025.2.14
        # 检索要调整参数的规则列表
        # 规则库
        # rule_file = '/root/sysinsight-main/HisRule/gptuner_update_rule_tpch_runtime_144_0.1.txt'
        rule_file = '/home/sysinsight/HisRule/mysql_sysbench_update_rule_revision.txt'
        defaultKnob_file = '/home/sysinsight/DBTuner/knobspace/gptuner_target_knobs.json'
        # ruleFunctionRaneg_file = '/root/RUC/DBTune/scripts/rule/function_range_tpch_gptuner.csv'
        
        # 逐条规则验证
        # 开始时间
        start_time = time.perf_counter()
        rulebase_list=[]
        selected_rules=[]
        processed_selected_rules=[]
        globalRules = read_rules_from_file(rule_file)
        for rule in globalRules:
            # ajustKnobs, processed_rule = searchRule(defaultKnob_file,rule,self.config,self.keyFunction_file,ruleFunctionRaneg_file)
            ajustKnobs, processed_rule = searchRule(defaultKnob_file,rule,self.config,self.keyFunction_file)
            if ajustKnobs and processed_rule: 
                # 汇总需要调整的参数
                for key in ajustKnobs:
                    if key not in updateKnobs_names:
                        updateKnobs_names.append(key)
                
                # 仅返回建议修改的参数        
                if ajustKnobs not in rulebase_list:
                    rulebase_list.append(ajustKnobs)
                selected_rules.append(rule)
                processed_selected_rules.append(processed_rule)
        
         # 结束时间
        end_time = time.perf_counter()
        
        # 计算并输出运行时间
        elapsed_time = end_time - start_time
        print(f"the retrive rule time: {elapsed_time:.4f} 秒")
        self.hyperparameters = updateKnobs_names

        dataflow_code_1 = []
        dataflow_code_1 = extract_code_for_knob_from_json(updateKnobs, codeFoder, change_config)
        # print(dataflow_code_1)
        
        self.replacements = {
            "variable": updateKnobs_names,
            "keyFunction": bkFunctions_list,
            "uKnobs": updateKnobs,
            "dataflow": dataflow_code_1,
            "searchRule": selected_rules,
            "rulebase": rulebase_list,
            "resource": self.resource
        }

    def transfer_rule(self):
        # rule_file = '/root/sysinsight-main/HisRule/gptuner_update_rule_tpch_runtime_144_0.1.txt'
        rule_file = '/home/sysinsight/HisRule/rule_mysql_tpcc_update_rule_revision.txt'
        defaultKnob_file = '/home/sysinsight/DBTuner/knobspace/gptuner_target_knobs.json'
        # ruleFunctionRaneg_file = '/root/RUC/DBTune/scripts/rule/function_range_tpch_gptuner.csv'
        
        print("transfer_rule ing...")
        memory_knobs = [
            'tmp_table_size', 'max_heap_table_size', 'query_prealloc_size',
            'sort_buffer_size', 'innodb_buffer_pool_size', 
            'innodb_online_alter_log_max_size', 'join_buffer_size',
            'table_open_cache', 'thread_cache_size', 
            'range_optimizer_max_mem_size', 'stored_program_definition_cache',
            'tablespace_definition_cache', 'temptable_max_ram',
            'key_cache_block_size', 'max_relay_log_size'
        ]
        globalRules = read_rules_from_file(rule_file)
        selected_rules = self.replacements.get("searchRule", [])
        return selected_rules, globalRules, defaultKnob_file,rule_file,memory_knobs
        
if __name__ == "__main__":
    directory_path = "../perf_data/"

    # 创建 ParameterLibrary 对象
    parameter_library = ParameterLibrary(directory_path)

    # 示例配置字典
    config = {
        'big_tables': 1,  # 示例配置，可以根据实际需要进行调整
    }

    # 调用 get_prompt 方法并获取结果
    prompt = parameter_library.get_prompt()

    # 打印生成的 prompt
    print(prompt)
    # 使用示例
    