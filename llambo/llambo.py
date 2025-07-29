import json
import os
import traceback
import numpy as np
from llambo.discriminative_sm import LLM_DIS_SM
from llambo.generative_sm import LLM_GEN_SM
from llambo.acquisition_function import LLM_ACQ
from llambo.rate_limiter import RateLimiter
from llambo.warping import NumericalTransformer
import pandas as pd
import time
import pprint
from llambo.task_logger import TaskLogger
from llambo.extract_knob import ParameterLibrary
from DBTuner.utils.matchFunctions import read_function_names, get_knob_in_keyFunctions
from library.rule_library.rule_extract import extract_rule
from DBTuner.utils.matchRule import knob_normalize,updateMetric_useful,updateMetric_useless,update_rule_file,process_rule_catagory

class LLAMBO:
    def __init__(self, 
                 task_context: dict, # dictionary describing task (see above)
                 sm_mode, # either 'generative' or 'discriminative'
                 n_candidates, # number of candidate points to sample at each iteration
                 n_templates, # number of templates for LLM queries
                 n_gens,    # number of generations for LLM, set at 5
                 alpha,    # alpha for LLM, recommended to be -0.2
                 n_initial_samples, # number of initial samples to evaluate
                 n_trials,   # number of trials to run,
                 init_f,        # function to generate initial configurations
                 bbox_eval_f,       # bbox function to evaluate a point
                 chat_engine,       # LLM chat engine
                 top_pct=None,      # only used for generative SM, top percentage of points to consider for generative SM
                 use_input_warping=False,       # whether to use input warping
                 prompt_setting=None,    # ablation on prompt design, either 'full_context' or 'partial_context' or 'no_context'
                 shuffle_features=False     # whether to shuffle features in prompt generation
                 ):
        self.task_context = task_context
        assert sm_mode in ['generative', 'discriminative']
        assert top_pct is None if sm_mode == 'discriminative' else top_pct is not None
        self.model_name = task_context['model']
        self.lower_is_better = task_context['lower_is_better']
        lower_is_better = self.lower_is_better
        self.n_candidates = n_candidates
        self.n_template = n_templates
        self.n_gens = n_gens
        self.alpha = alpha
        self.n_initial_samples = n_initial_samples
        self.n_trials = n_trials
        self.llm_query_cost = []    # list of cost for LLM calls in EACH TRIAL
        self.llm_query_time = []    # list of time taken for LLM calls in EACH TRIAL
        self.task_logger = TaskLogger(task_context, "./old_output")
        self.promptlib = ParameterLibrary(task_context)
        # self.recommendation_times = [] # list of cost time in each trial
        

        assert type(shuffle_features) == bool, 'shuffle_features should be a boolean'
        assert type(use_input_warping) == bool, 'use_input_warping should be a boolean'

        self.init_f = init_f
        self.bbox_eval_f = bbox_eval_f

        if use_input_warping:
            warping_transformer = NumericalTransformer(task_context['hyperparameter_constraints'])
        else:
            warping_transformer = None

        rate_limiter = None #RateLimiter(max_tokens=100000, time_frame=60, max_requests=720)
        
        print('='*150)
        print(f'[Search settings]: ' + '\n\t'
              f'n_candidates: {n_candidates}, n_templates: {n_templates}, n_gens: {n_gens}, ' + '\n\t'
              f'alpha: {alpha}, n_initial_samples: {n_initial_samples}, n_trials: {n_trials}, ' + '\n\t'
              f'using warping: {use_input_warping}, ablation: {prompt_setting}, '
              f'shuffle_features: {shuffle_features}')
        print(f'[Task]: ' + '\n\t'
              f'task type: {task_context["task"]}, sm: {sm_mode}, lower is better: {lower_is_better}')
        print(f'Hyperparameter search space: ')
        pprint.pprint(task_context['hyperparameter_constraints'])
        print('='*150)

        # initialize surrogate model and acquisition function
        if sm_mode == 'generative':
            self.surrogate_model = LLM_GEN_SM(task_context, n_gens, lower_is_better, top_pct,
                                              n_templates=n_templates, rate_limiter=None)
        else:
            self.surrogate_model = LLM_DIS_SM(task_context, n_gens, lower_is_better, 
                                              n_templates=n_templates, rate_limiter=rate_limiter, 
                                              warping_transformer=warping_transformer,
                                              chat_engine=chat_engine, prompt_setting=prompt_setting, 
                                              shuffle_features=shuffle_features)
            
        self.acq_func = LLM_ACQ(task_context, n_candidates, n_templates, lower_is_better, 
                                rate_limiter=rate_limiter, warping_transformer=warping_transformer, 
                                chat_engine=chat_engine, prompt_setting=prompt_setting, 
                                shuffle_features=shuffle_features)


    def _initialize(self):
        '''Initialize the optimization loop.'''
        start_time = time.time()
        # generate initial configurations
        init_configs = self.init_f(self.n_initial_samples)

        assert isinstance(init_configs, list), 'init_f() should return a list of configs (dictionaries)'
        for item in init_configs:
            assert isinstance(item, dict), 'init_f() should return a list of configs (dictionaries)'

        init_configs = pd.DataFrame(init_configs)
        assert init_configs.shape[0] == self.n_initial_samples, 'init_f() should return n_initial_samples number of configs'

        # create empty pandas dataframe for observed function values
        self.observed_fvals = pd.DataFrame()
        self.observed_configs = pd.DataFrame()

        for index, _ in init_configs.iterrows():
            one_config = init_configs.iloc[[index]]
            """hzt todo 需要补充"""
            one_config, one_result, keyFunction_file, config_, resource = self._evaluate_config(one_config)

            self.promptlib.config = config_
            self.promptlib.keyFunction_file = keyFunction_file
            self.promptlib.resource = resource
            self.promptlib.update()

            # save prompt to file
            pr_data = self.promptlib.get_prompt()
            prompt_file_path = 'prompt.txt'
            if not os.path.exists(prompt_file_path):
                with open(prompt_file_path, 'w') as f:
                    f.write("")

            formatted_pr_data = str(pr_data).replace("\\n", "\n")
            with open(prompt_file_path, 'a') as f:
                f.write("***************************************************************\n")
                f.write(formatted_pr_data + "\n")


            if self.observed_fvals.empty:
                self.observed_fvals = one_result
            else:
                self.observed_fvals = pd.concat([self.observed_fvals, one_result], axis=0, ignore_index=True)

            if self.observed_configs.empty:
                self.observed_configs = one_config
            else:
                self.observed_configs = pd.concat([self.observed_configs, one_config], axis=0, ignore_index=True)

        print(f'[Initialization] COMPLETED: {self.observed_fvals.shape[0]} points evaluated...')
        end_time = time.time()

        time_taken = end_time - start_time
        return 0, time_taken

    def _evaluate_config(self, config):
        # can support batch mode in the future
        # 这个函数得修改!!!!!!
        assert config.shape[0] == 1, 'batch mode not supported yet'
        config = config.to_dict('records')[0]

        eval_config, eval_results,resource, output = self.bbox_eval_f(config)

        assert isinstance(eval_config, dict), 'bbox_eval_f() should return the evaluated config as a dictinoary'
        assert isinstance(eval_results, dict), 'bbox_eval_f() should return bbox evaluation results as a dictionary'
        assert 'score' in eval_results.keys(), 'score must be a key in results returned'

        self.task_logger.insert_log_entry({"config": config, "score": eval_results})

        return pd.DataFrame([eval_config]), pd.DataFrame([eval_results]), output, config, resource

    def _update_observations(self, new_config, new_fval):
        '''Update the observed configurations and function values.'''
        # append new observations
        self.observed_configs = pd.concat([self.observed_configs, new_config], axis=0, ignore_index=True)
        self.observed_fvals = pd.concat([self.observed_fvals, new_fval], axis=0, ignore_index=True)

    def optimize(self, test_metric='generalization_score'):
        '''Run the optimization loop.'''
        # initialize
        cost, query_time = self._initialize()
        self.llm_query_cost.append(cost)
        self.llm_query_time.append(query_time)
        
        self.recommendation_times = []  # 存储每次推荐流程的时间
        self.average_recommendation_time = 0.0  # 推荐流程的平均时间

        if self.lower_is_better:
            self.best_fval = self.observed_fvals['score'].min()
            best_gen_fval = self.observed_fvals[test_metric].min()
        else:
            self.best_fval = self.observed_fvals['score'].max()
            best_gen_fval = self.observed_fvals[test_metric].max()

        print(f'[Initialization] COMPLETED: best fval: {self.best_fval:.4f}, best generalization fval: {best_gen_fval:.4f}')
        print('='*150)

        # optimization loop
        for trial_id in range(self.n_trials):
            try:
                trial_cost = 0
                trial_query_time = 0

                start_time = time.time()
                # 记录推荐流程开始时间（生成候选点前）
                recommend_start = time.time()
                # get candidate point
                # 重点函数！！！！！！！！！！！！！
                #self.promptlib.auto_load_file()
                # print(self.promptlib.config)
                # print(len(self.promptlib.config.keys()))
                # print(1237899999)
                
                # hzy modify
                # candidate_points, cost, time_taken, rule_config_change = self.acq_func.get_candidate_points(self.observed_configs, self.observed_fvals[['score']], alpha=self.alpha, config = self.promptlib)
                candidate_points, cost, time_taken = self.acq_func.get_candidate_points(self.observed_configs, self.observed_fvals[['score']], alpha=self.alpha, config = self.promptlib)
                
                trial_cost += cost
                trial_query_time += time_taken

                print('='*150)
                print('EXAMPLE POINTS PROPOSED')
                print(candidate_points)
                print('='*150)
                
                # select candidate point
                sel_candidate_point, cost, time_taken = self.surrogate_model.select_query_point(self.observed_configs, 
                                                                            self.observed_fvals[['score']], 
                                                                            candidate_points, self.promptlib)
                trial_cost += cost
                trial_query_time += time_taken

                self.llm_query_cost.append(trial_cost)
                self.llm_query_time.append(trial_query_time)
                
                # 记录推荐流程结束时间（选完最佳候选点即完成推荐）
                # TODO 计算推荐的时间
                recommend_end = time.time()
                # 计算本次推荐配置的耗时（仅包含生成和选择候选点）
                current_recommend_time = recommend_end - recommend_start
                self.recommendation_times.append(current_recommend_time)
                
                # 更新平均推荐时间
                self.average_recommendation_time = sum(self.recommendation_times) / len(self.recommendation_times)

                print('='*150)
                print('SELECTED CANDIDATE POINT')
                print(sel_candidate_point)
                print('='*150)
                # 打印推荐时间统计（不包含评估环节）
                print(f'-------[推荐配置时间] 本次耗时: {current_recommend_time:.4f}秒, 平均耗时: {self.average_recommendation_time:.4f}秒-------------')
                
                
                sel_candidate_point, sel_candidate_fval, keyFunction_file, config_, resource = self._evaluate_config(sel_candidate_point)

                # hzy add
                # keyFunctions_list = read_function_names(keyFunction_file)
                
                json_file = "/root/sysinsight-main/DBTuner/utils/paramater_association_library.json"
                function_to_knob = get_knob_in_keyFunctions(keyFunction_file, json_file)
                print("function_to_knob: ", function_to_knob)
                
                change_fval = {
                    "old_fval": self.observed_fvals.iloc[-1]["score"],
                    "new_fval": sel_candidate_fval["score"].values[0],
                    "change_fval": sel_candidate_fval["score"].values[0] - self.observed_fvals.iloc[-1]["score"]
                }
                print("change_fval: ", change_fval)
                
                print("sel_candidate_point: ", sel_candidate_point)

                rule_config_change = []
                for key in sel_candidate_point.keys():

                    # 提取值
                    new_value = sel_candidate_point.at[0, key]

                    # 根据类型动态转换
                    if isinstance(new_value, (np.integer, int)):
                        new_value = int(new_value)
                    elif isinstance(new_value, (np.floating, float)):
                        new_value = float(new_value)
                    elif isinstance(new_value, str):
                        new_value = new_value.strip()  # 如果是字符串，做简单清理

                    rule_config_change.append({
                        "knob": key,
                        "old_value": self.promptlib.config[key],
                        "new_value": new_value
                    })

                # extract_rule(keyFunctions_list, rule_config_change, change_fval)
                print("rule_config_change: ", rule_config_change)
                
                
                # extract_rule(function_to_knob, rule_config_change, change_fval)

                self.promptlib.config = config_
                self.promptlib.keyFunction_file = keyFunction_file
                self.promptlib.update()
                #self.task_logger.insert_log_entry(sel_candidate_point)
                
                ###################TODO：ctt 规则维护：需要比较两次的参数值和tps变化值  ##############################################################################################
                # 初始化调用日志文件路径
                call_log_file = "matched_rules_calls_tpch.json"

                # 初始化或加载调用记录
                if os.path.exists(call_log_file):
                    with open(call_log_file, "r") as f:
                        rule_call_log = json.load(f)
                else:
                    rule_call_log = {}

                # 当前是第几次调用
                current_call_id = f"call_{len(rule_call_log) + 1}"
                
                selected_rules, globalRules,defaultFile,rule_file = self.promptlib.transfer_rule()
                if selected_rules:
                    # 查看现在的参数改变情况是否符合规则中的参数配置，并且tps变化是否符合规则
                    matched_rules = []
                    processed_matched_rules = []
                    matched_rule_set = set() 
                    old_performance = self.observed_fvals.iloc[-1]["score"]
                    new_performance = sel_candidate_fval["score"].values[0]
                    performance_change = (new_performance - old_performance) / old_performance * 100
                    print("performance change: ", performance_change)
                    for change in rule_config_change:
                        knob_name = change['knob']
                        old_value = change['old_value']
                        new_value = change['new_value']
                        for rule1 in selected_rules:
                            rule = process_rule_catagory(rule1)
                            for knob_info in rule['knob']:
                                if knob_info['name'] == knob_name:
                                    # old_value = self.promptlib.config[key]
                                    old_value_norm = knob_normalize(defaultFile,knob_name,old_value)
                                    new_value_norm = knob_normalize(defaultFile,knob_name,new_value)
                                    change_amount = new_value_norm - old_value_norm
                                    lower_bound = knob_info['lower_bound']
                                    upper_bound = knob_info['upper_bound']
                                    # print("change_amount : ",change_amount)
                                    # print("key:{} old_value:{} new_value:{}, change_amount: {}",knob_name, old_value, new_value, change_amount)
                                    if (lower_bound <= change_amount <= upper_bound) or (lower_bound == -float("inf") and change_amount <= upper_bound) or (upper_bound == -float("inf") and change_amount > lower_bound):      
                                        if rule['performance']['lower_bound'] <= performance_change <= rule['performance']['upper_bound']:
                                            #  匹配到规则
                                            # TODO 记录下每次匹配到的规则，以及更改的值 -- 4950 -> 4951查看一下
                                            rule = updateMetric_useful(rule)
                                            print("rule matched.")
                                        else:
                                            # 没有匹配到规则
                                            rule = updateMetric_useless(rule)
                                            print("rule not matched.")
                                        if rule1 not in matched_rule_set:
                                            matched_rule_set.add(rule1)
                                            matched_rules.append(rule1)
                                            processed_matched_rules.append(rule)
                                            
                    update_rule_file(globalRules, processed_matched_rules, rule_file)
                    # 记录当前调用匹配到的规则（使用字符串列表）
                    rule_call_log[current_call_id] = processed_matched_rules

                    # 保存到文件
                    with open(call_log_file, "w") as f:
                        json.dump(rule_call_log, f, indent=4)
                                        
                ##############################################################################################
                     
                # save prompt to file
                pr_data = self.promptlib.get_prompt()
                prompt_file_path = 'prompt.txt'
                if not os.path.exists(prompt_file_path):
                    with open(prompt_file_path, 'w') as f:
                        f.write("")

                formatted_pr_data = str(pr_data).replace("\\n", "\n")
                with open(prompt_file_path, 'a') as f:
                    f.write("***************************************************************\n")
                    f.write(formatted_pr_data + "\n")

                # update observations
                self._update_observations(sel_candidate_point, sel_candidate_fval)

                print('='*150)
                print('UPDATED OBSERVATIONS')
                print(self.observed_configs)
                print(self.observed_fvals)
                print('='*150)

                end_time = time.time()
                time_taken = end_time - start_time

                current_fval_cv = sel_candidate_fval['score'].values[0]
                current_fval_gen = sel_candidate_fval[test_metric].values[0]

                if self.lower_is_better:
                    if current_fval_cv < self.best_fval:
                        self.best_fval = current_fval_cv
                        best_found = True
                    else:
                        best_found = False
                else:
                    if current_fval_cv > self.best_fval:
                        self.best_fval = current_fval_cv
                        best_found = True
                    else:
                        best_found = False

                if best_found:
                    print(f'[Trial {trial_id} completed, time taken: {time_taken:.2f}s] best fval (cv): {self.best_fval:.4f}, current fval (cv): {current_fval_cv:.4f}. Generalization fval: {current_fval_gen:.4f} NEW BEST FVAL FOUND!!')
                else: 
                    print(f'[Trial {trial_id} completed, time taken: {time_taken:.2f}s] best fval (cv): {self.best_fval:.4f}, current fval (cv): {current_fval_cv:.4f}. Generalization fval: {current_fval_gen:.4f}.')
                print('='*150)

            except Exception as e:
                print(f"Error occurred during trial {trial_id}: {str(e)}")
                print(traceback.format_exc())


        # returns history of observed configurations and function values
        return self.observed_configs, self.observed_fvals