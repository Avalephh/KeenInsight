from datetime import datetime
import os
from DBTuner.utils.matchFunctions import match_knob_functions,read_function_names,read_function_names_with_change,getTopKnob,get_knob_in_keyFunctions,find_top_and_matched_functions
from DBTuner.utils.matchFunctions_shap import getShapFuncKnobs
from DBTuner.utils.extractCode import extract_code_for_knob_from_json
from DBTuner.utils.getRule import get_rules,group_rules_by_knob
from DBTuner.utils.matchRule_revision import searchRule,read_rules_from_file
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

8. Optimization Goals:
    - Minimize Query Latency

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
    
    
    # 创建 prompt
    # def fill_placeholders(self, template, replacements):
    #     # 替换简单信息
    #     knobs = replacements["variable"]
    #     # keyFunctions = replacements["keyFunction"]
    #     knobs_str = ", ".join(knobs)
    #     # keyFunctions_str = ", ".join(keyFunctions)
    #     template = template.replace("{variable}", knobs_str)
    #     # template = template.replace("{keyFunction}", keyFunctions_str)


    #     dataflow = replacements["dataflow"]
    #     uKnobs = replacements["uKnobs"]

    #     # Build the new sections
    #     dataflow_section = ""
    #     code_section = ""
    

    #     for entry in uKnobs:
    #         knob_name = entry["knob_name"]
    #         data_flows = ", ".join(entry["data_flow_functions"])
    #         control_flows = ", ".join(entry["control_flow_functions"])
    #         if data_flows == "":
    #             dataflow_section += f"Parameters {knob_name} affect control flow function {control_flows};\n"
    #         elif control_flows == "":
    #             dataflow_section += f"Parameters {knob_name} affect data flow function {data_flows};\n"        
    #         else:
    #             dataflow_section += f"Parameters {knob_name} affect the function for data flow {data_flows}, control flow function {control_flows};\n"

    #     for entry in dataflow:
    #       knob_name = entry["knob_name"]
    #       for func_detail in entry["data_flow_functions_code"]:
    #           function_name = func_detail["function"]
    #           code_snippet = func_detail["code"]
    #           code_section += f"Parameters {knob_name} affect the data flow function {function_name}, the corresponding code is:\n{code_snippet}\n"
        
    #     # Replace the sections in the template
    #     template = template.replace("{dataflow_section}", dataflow_section.strip())
    #     template = template.replace("{code_section}", code_section.strip())

    #     # TODO:CTT: the keyFunction section
    #     # keyFunction_with_change = replacements["keyFunctionWithChange"]
    #     # keyFunction_section = ""
    #     # for entry in keyFunction_with_change:
    #     #     function_name = entry[0]
    #     #     change = entry[1]
    #     #     if(change == 0):
    #     #         keyFunction_section += f"The bottleneck function {function_name} is called more times than perf with default parameter values;\n"
    #     #     else:
    #     #         keyFunction_section += f"The bottleneck function {function_name} iscalled less frequently than perf with default parameter values\n"
        
    #     # 2024.11.30
    #     keyFunction_section = ""
    #     bottleneck_functions = replacements["keyFunction"]
    #     for entry in bottleneck_functions:
    #         function_name = entry[0]
    #         function_rate = entry[1]
    #         change = entry[3]
    #         if(change == 0):
    #             keyFunction_section += f"The sampling rate of the bottleneck function {function_name} is {function_rate}, which is higher than the sampling rate of the default function;\n"
    #         else:
    #             keyFunction_section += f"The sampling rate of the bottleneck function {function_name} is {function_rate}, which is lower than the sampling rate of the default function;\n"
            
    #     template = template.replace("{keyFunction_section}", keyFunction_section.strip())
        
        
    #     # TODO: 历史数据规则
    #     rule_section = ""
    #     searchRule = replacements.get("searchRule", [])
    #     rulebase_list = replacements.get("rulebase", [])
    #     if searchRule and rulebase_list:
    #         # 开始构建规则段
    #         rule_section += f"The rules retrieved for the historical data are {searchRule}, which means \n "
            
    #         for ruleBase in rulebase_list:
    #             ajustKnobs = ruleBase.get('ajustKnobs', {})
    #             processed_rule = ruleBase.get('processed_rule', {})
    #             if not ajustKnobs and not processed_rule:
    #                 print("No knobs to adjust or processed rule is empty, skipping further processing.")
    #                 continue  # 如果都为空，跳过当前循环
                
    #             # 处理function采样率
    #             function_conditions = []
    #             if 'function' in processed_rule and processed_rule['function']:
    #                 for fun_info in processed_rule['function']:
    #                     function_name = fun_info['name']  # 假设每个function有'name'字段
    #                     function_conditions.append(f"when the sampling rate of function {function_name} is in the range of [{fun_info['lower_bound']}, {fun_info['upper_bound']}] (according to max-min normalization)")
                
    #             # 处理knob的配置变化
    #             knob_conditions = []
    #             if 'knob' in processed_rule and processed_rule['knob']:
    #                 for knob_info in processed_rule['knob']:
    #                     knob_name = knob_info['name']  # 假设每个knob有'name'字段
    #                     knob_conditions.append(f"the configuration value of parameter {knob_name} should be within the variation range of [{knob_info['lower_bound']}, {knob_info['upper_bound']}] (according to max-min normalization)")
                
    #             # 组合function和knob的条件
    #             if function_conditions:
    #                 rule_section += ", and ".join(function_conditions) + ", "
                
    #             if knob_conditions:
    #                 rule_section += ", and ".join(knob_conditions) + ", "
                
    #             # 处理TPS的提高效果
    #             if 'tps' in processed_rule:
    #                 tps_increase = processed_rule['tps']  # 假设有'tps'字段，表示TPS提高幅度
    #                 tps_lower_bound = tps_increase['lower_bound']
    #                 tps_upper_bound = tps_increase['upper_bound']
    #                 rule_section += f"mysql performance tps will increase {tps_lower_bound}% ~ {tps_upper_bound}%. According to this rule, parameters in the rule are modified, "
                
    #             # 输出调整的knob信息
    #             for config, value in ajustKnobs.items():
    #                 rule_section += f"the parameter {config} and corresponding value are {value}. \n "
    #     else:
    #         rule_section += f"No rules matched to historical data."       
    #             # 打印或记录每条规则的构建结果
    #             # print(rule_section)

            
    #     template = template.replace("{rule_section}", rule_section.strip())
        
    #     print("********************************************************************************\n")
    #     print(template)
    #     print("********************************************************************************\n")
    #     return template
    def find_key_for_knob(self, knob):
        for key, params in self.store_csv_func_to_knob.items():
            if knob in params:
                return key
        return None
    
    def fill_placeholders(self, template, replacements):
        
        # add resource
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
        
        
        # 替换简单信息
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

        # for entry in dataflow:
        #   knob_name = entry["knob_name"]
        #   for func_detail in entry["data_flow_functions_code"]:
        #       function_name = func_detail["function"]
        #       code_snippet = func_detail["code"]
        #       code_section += f"Parameters {knob_name} affect the data flow function {function_name}, the corresponding code is:\n{code_snippet}\n"
        
        # Replace the sections in the template
        # TODO no code
        template = template.replace("{dataflow_section}", dataflow_section.strip())
        # template = template.replace("{code_section}", code_section.strip())
        
        # 2024.11.30
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
        print("rulebase_list: ", rulebase_list)
        if searchRule and rulebase_list:
            # # 开始构建规则段
            # rule_section += f"The rules retrieved for the historical data are {searchRule}, which means \n "
            # rule_section += f"The rules retrieved for the historical data are as follows: \n "
            rule_section += f"Based on the rules obtained from the historical data, you are advised to adjust the following: \n "
            
            for ajustKnobs in rulebase_list:
                for config, value in ajustKnobs.items():
                    rule_section += f"Adjust parameter {config} to {value}\n"
        else:
            rule_section += f"No rules matched to historical data."
                

        # # TODO no rule
        template = template.replace("{rule_section}", rule_section.strip())
        
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
        staticFile = "/root/sysinsight-main/DBTuner/utils/paramater_association_library.json"
        codeFoder = '/root/sysinsight-main/library/extractCode'
        
        print("ddddd: ",self.resource)
        

        # keyFunctions_list = read_function_names(self.keyFunction_file)
        # uKnobs = []
        # uKnobs = match_knob_functions(self.keyFunction_file, staticFile)

        # knob_names = [item['knob_name'] for item in uKnobs]
        # dataflow_values = [str(value) for item in uKnobs for value in item.values()]
        # dataflow = ' '.join(dataflow_values)

        # dataflow_code = []
        # dataflow_code = extract_code_for_knob_from_json(uKnobs, codeFoder, change_config)

        # self.hyperparameters = knob_names

        # TODO:CTT: Update the keyfunction with change
        # keyFunctions_list_with_change = read_function_names_with_change(self.keyFunction_file)

        # top 5 knobs
        # function_to_knob = get_knob_in_keyFunctions(self.keyFunction_file, staticFile)
        # top_knobs = getTopKnob(function_to_knob, 5)
        # top_param_names = [param for param, count in top_knobs]

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
        #print("hzt666777", self.store_csv_func_to_knob)
        
        # TODO: 接入规则 2025.2.14
        # 检索要调整参数的规则列表
        # 规则库
        # rule_file = '/root/sysinsight-main/HisRule/gptuner_update_rule_tpch_runtime_144_0.1.txt'
        rule_file = '/root/sysinsight-main/HisRule/rule_mysql_tpcc_update_rule_revision.txt'
        defaultKnob_file = '/root/sysinsight-main/DBTuner/knobspace/gptuner_target_knobs.json'
        # ruleFunctionRaneg_file = '/root/RUC/DBTune/scripts/rule/function_range_tpch_gptuner.csv'
        
        # function_names_list = [item[0] for item in bkFunctions_list]
        # print("219219function_names_list: ",function_names_list)
        # matched_rules = get_rules(rule_file, function_names_list)
        
        # rulebase_list=[]
        # selected_rules=[]
        # if matched_rules:
        #     selected_rules = group_rules_by_knob(matched_rules)
        #     for rule in selected_rules:
        #         # rule = ",".join(matched_rules[0]) # 仅返回一条规则
        #         ajustKnobs, processed_rule = searchRule(defaultKnob_file,rule,self.config,self.keyFunction_file,ruleFunctionRaneg_file)
        #         print("214214 ajustKnobs: ",ajustKnobs)
        #         print("214214 processed_rule: ",processed_rule)
        #         if ajustKnobs and processed_rule: 
        #             rulebase={}
        #             rulebase['ajustKnobs'] = ajustKnobs
        #             rulebase['processed_rule'] = processed_rule
        #             # 汇总需要调整的参数
        #             for key in ajustKnobs:
        #                 if key not in updateKnobs_names:
        #                     updateKnobs_names.append(key)
        #             rulebase_list.append(rulebase)
                
        # print('rulebase_list: ', rulebase_list)
        # print("updateKnobs_names: ",updateKnobs_names)
        
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
            # print("214214 ajustKnobs: ",ajustKnobs)
            # print("214214 processed_rule: ",processed_rule)
            if ajustKnobs and processed_rule: 
                # rulebase={}
                # rulebase['ajustKnobs'] = ajustKnobs
                # rulebase['processed_rule'] = processed_rule
                # rulebase_list.append(rulebase)
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
            # "variable": knob_names,
            "variable": updateKnobs_names,
            # "keyFunction": keyFunctions_list,
            "keyFunction": bkFunctions_list,
            # "uKnobs": uKnobs,
            "uKnobs": updateKnobs,
            "dataflow": dataflow_code_1,
            # "code": dataflow_code
            # "keyFunctionWithChange": keyFunctions_list_with_change
            "searchRule": selected_rules,
            "rulebase": rulebase_list,
            "resource": self.resource
        }

    def transfer_rule(self):
        # rule_file = '/root/sysinsight-main/HisRule/gptuner_update_rule_tpch_runtime_144_0.1.txt'
        rule_file = '/root/sysinsight-main/HisRule/rule_mysql_tpcc_update_rule_revision.txt'
        defaultKnob_file = '/root/sysinsight-main/DBTuner/knobspace/gptuner_target_knobs.json'
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
        
        # 分组
        # selected_rules = self.replacements.get("searchRule", [])
        # processed_selected_rules = self.replacements.get("processed_rule", [])
        # return selected_rules, processed_selected_rules, defaultKnob_file
        
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
    