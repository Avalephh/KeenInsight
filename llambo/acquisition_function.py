import json
import os
import random
import math
import time
import traceback
import openai
import asyncio
import numpy as np
import pandas as pd
from aiohttp import ClientSession
from langchain import FewShotPromptTemplate
from langchain import PromptTemplate
from llambo.rate_limiter import RateLimiter
from llambo.extract_knob import ParameterLibrary
import re

openai.api_type = os.environ["OPENAI_API_TYPE"]
openai.api_version = os.environ["OPENAI_API_VERSION"]
openai.api_base = os.environ["OPENAI_API_BASE"]
openai.api_key = os.environ["OPENAI_API_KEY"]


class LLM_ACQ:
    def __init__(self, task_context, n_candidates, n_templates, lower_is_better, 
                 jitter=False, rate_limiter=None, warping_transformer=None, chat_engine=None, 
                 prompt_setting=None, shuffle_features=False):
        '''Initialize the LLM Acquisition function.'''
        self.task_context = task_context
        self.n_candidates = n_candidates
        self.n_templates = n_templates
        self.n_gens = int(n_candidates/n_templates)
        self.lower_is_better = lower_is_better
        self.apply_jitter = jitter
        if rate_limiter is None:
            self.rate_limiter = RateLimiter(max_tokens=40000, time_frame=60)
        else:
            self.rate_limiter = rate_limiter
        if warping_transformer is None:
            self.warping_transformer = None
            self.apply_warping = False
        else:
            self.warping_transformer = warping_transformer
            self.apply_warping = True
        self.chat_engine = chat_engine
        self.prompt_setting = prompt_setting
        self.shuffle_features = shuffle_features

        assert type(self.shuffle_features) == bool, 'shuffle_features must be a boolean'


    def _jitter(self, desired_fval):
        '''Add jitter to observed fvals to prevent duplicates.'''

        if not self.apply_jitter:
            return desired_fval

        assert hasattr(self, 'observed_best'), 'observed_best must be set before calling _jitter'
        assert hasattr(self, 'observed_worst'), 'observed_worst must be set before calling _jitter'
        assert hasattr(self, 'alpha'), 'alpha must be set before calling _jitter'

        jittered = np.random.uniform(low=min(desired_fval, self.observed_best), 
                                        high=max(desired_fval, self.observed_best), 
                                        size=1).item()

        return jittered


    def _count_decimal_places(self, n):
        '''Count the number of decimal places in a number.'''
        s = format(n, '.10f')
        if '.' not in s:
            return 0
        n_dp = len(s.split('.')[1].rstrip('0'))
        return n_dp

    def _prepare_configurations_acquisition(
        self,
        observed_configs=None, 
        observed_fvals=None, 
        seed=None,
        use_feature_semantics=True,
        shuffle_features=False
    ):
        '''Prepare and (possibly shuffle) few-shot examples for prompt templates.'''
        examples = []
        
        if seed is not None:
            # if seed is provided, shuffle the observed configurations
            np.random.seed(seed)
            shuffled_indices = np.random.permutation(observed_configs.index)
            observed_configs = observed_configs.loc[shuffled_indices]
            if observed_fvals is not None:
                observed_fvals = observed_fvals.loc[shuffled_indices]
        else:
            # if no seed is provided, sort the observed configurations by fvals
            if type(observed_fvals) == pd.DataFrame:
                if self.lower_is_better:
                    observed_fvals = observed_fvals.sort_values(by=observed_fvals.columns[0], ascending=False)
                else:
                    observed_fvals = observed_fvals.sort_values(by=observed_fvals.columns[0], ascending=True)
                observed_configs = observed_configs.loc[observed_fvals.index]

        if shuffle_features:
            # shuffle the columns of observed configurations
            np.random.seed(0)
            shuffled_columns = np.random.permutation(observed_configs.columns)
            observed_configs = observed_configs[shuffled_columns]
            
        # serialize the k-shot examples
        if observed_configs is not None:
            hyperparameter_names = observed_configs.columns
            for index, row in observed_configs.iterrows():
                row_string = '## '
                for i in range(len(row)):
                    hyp_type = self.task_context['hyperparameter_constraints'][hyperparameter_names[i]][0]
                    hyp_transform = self.task_context['hyperparameter_constraints'][hyperparameter_names[i]][1]

                    if use_feature_semantics:
                        row_string += f'{hyperparameter_names[i]}: '
                    else:
                        row_string += f'X{i+1}: '

                    if hyp_type in ['int', 'float']:
                        lower_bound = self.task_context['hyperparameter_constraints'][hyperparameter_names[i]][2][0]
                    else:
                        lower_bound = self.task_context['hyperparameter_constraints'][hyperparameter_names[i]][2][1]
                    n_dp = None
                    if hyp_type in ['int', 'float']:
                        n_dp = self._count_decimal_places(lower_bound)
                    value = row[i]
                    if self.apply_warping:
                        try:
                            if hyp_type == 'int' and hyp_transform != 'log':
                                row_string += str(int(value))
                            elif hyp_type == 'float' or hyp_transform == 'log':
                                row_string += f'{value:.{n_dp}f}'
                            elif hyp_type == 'ordinal':
                                row_string += f'{value:.{n_dp}f}'
                            else:
                                row_string += value
                        except Exception as e:
                            row_string += str(value)

                    else:
                        try:
                            if hyp_type == 'int':
                                row_string += str(int(value))
                            elif hyp_type in ['float', 'ordinal']:
                                row_string += f'{value:.{n_dp or 4}f}'
                            elif hyp_type == 'enum':
                                row_string += str(value)
                            elif hyp_type == 'real':
                                row_string += f'{value:.{n_dp or 4}f}'
                            else:
                                row_string += value
                        except Exception as e:
                            row_string += str(value)

                    if i != len(row)-1:
                        row_string += ', '
                row_string += ' ##'
                example = {'Q': row_string}
                if observed_fvals is not None:
                    row_index = observed_fvals.index.get_loc(index)
                    perf = f'{observed_fvals.values[row_index][0]:.6f}'
                    example['A'] = perf
                examples.append(example)

        elif observed_fvals is not None:
            examples = [{'A': f'{observed_fvals:.6f}'}]
        else:
            raise Exception
            
        return examples
    

    def _gen_prompt_tempates_acquisitions(
        self,
        observed_configs, 
        observed_fvals, 
        desired_fval,
        n_prompts=1,
        use_context='full_context',
        use_feature_semantics=True,
        shuffle_features=False,
        promptlib = None
    ):
        '''Generate prompt templates for acquisition function.'''
        all_prompt_templates = []
        all_query_templates = []

        for i in range(n_prompts):
            few_shot_examples = self._prepare_configurations_acquisition(observed_configs, observed_fvals,seed=i, use_feature_semantics=use_feature_semantics)           # need to update seed?
            jittered_desired_fval = self._jitter(desired_fval)

            # contextual information about the task
            task_context = self.task_context
            # model = task_context['model']
            model = 'mysql'
            # task = task_context['task']
            # task = 'regression'
            # tot_feats = task_context['tot_feats']
            # cat_feats = task_context['cat_feats']
            # num_feats = task_context['num_feats']
            # n_classes = task_context['n_classes']
            # metric = 'mean squared error' if task_context['metric'] == 'neg_mean_squared_error' else task_context['metric'
            # num_samples = task_context['num_samples']
            metric = 'transaction per second'
            hyperparameter_constraints = task_context['hyperparameter_constraints']
            
            example_template = """
Performance: {A}
Hyperparameter configuration: {Q}"""
            
            example_prompt = PromptTemplate(
                input_variables=["Q", "A"],
                template=example_template
            )
            # hzt todo


            """hzt todo preference需要修改"""
            question = promptlib.get_prompt()
            prefix = question + "\n" 
            prefix += f"The following are examples of performance of a {model} measured in {metric} and the corresponding model hyperparameter configurations."

            prefix += f" The allowable ranges for the hyperparameters are:\n"
            # print(config.hyperparameters)
            for i, (hyperparameter, constraint) in enumerate(hyperparameter_constraints.items()):
                # print(hyperparameter)
                if promptlib.hyperparameters != None and hyperparameter not in promptlib.hyperparameters:
                    continue
                if constraint[0] == 'float':
                    # number of decimal places!!
                    n_dp = self._count_decimal_places(constraint[2][0])
                    if constraint[1] == 'log' and self.apply_warping:
                        lower_bound = np.log10(constraint[2][0])
                        upper_bound = np.log10(constraint[2][1])
                    else:
                        lower_bound = constraint[2][0]
                        upper_bound = constraint[2][1]

                    if use_feature_semantics:
                        prefix += f"- {hyperparameter}: [{lower_bound:.{n_dp}f}, {upper_bound:.{n_dp}f}]"
                    else:
                        prefix += f"- X{i+1}: [{lower_bound:.{n_dp}f}, {upper_bound:.{n_dp}f}]"

                    if constraint[1] == 'log' and self.apply_warping:
                        prefix += f" (log scale, precise to {n_dp} decimals)"
                    else:
                        prefix += f" (float, precise to {n_dp} decimals)"
                elif constraint[0] == 'int':
                    if constraint[1] == 'log' and self.apply_warping:
                        lower_bound = np.log10(constraint[2][0])
                        upper_bound = np.log10(constraint[2][1])
                        n_dp = self._count_decimal_places(lower_bound)
                    else:
                        lower_bound = constraint[2][0]
                        upper_bound = constraint[2][1]
                        n_dp = 0

                    if use_feature_semantics:
                        prefix += f"- {hyperparameter}: [{lower_bound:.{n_dp}f}, {upper_bound:.{n_dp}f}]"
                    else:
                        prefix += f"- X{i+1}: [{lower_bound:.{n_dp}f}, {upper_bound:.{n_dp}f}]"
                    
                    if constraint[1] == 'log' and self.apply_warping:
                        prefix += f" (log scale, precise to {n_dp} decimals)"
                    else:
                        prefix += f" (int)"

                elif constraint[0] == 'ordinal':
                    if use_feature_semantics:
                        prefix += f"- {hyperparameter}: "
                    else:
                        prefix += f"- X{i+1}: "
                    prefix += f" (ordinal, must take value in {constraint[2]})"
                
                elif constraint[0] == 'enum':
                    prefix += f"- {hyperparameter}: enum, must take value in ({constraint[2]})"

                else:
                    raise Exception('Unknown hyperparameter value type') 
                prefix += "\n"
            prefix += f"Recommend a configuration that can achieve the target performance of {jittered_desired_fval:.6f}. "
            if use_context in ['partial_context', 'full_context']:
                prefix += "Do not recommend values at the minimum or maximum of allowable range, do not recommend rounded values. Recommend values with highest possible precision, as requested by the allowed ranges. "
            
            last_config = promptlib.config
            last_config_str = json.dumps(last_config, indent=2)
            last_config_str = last_config_str.replace("{", "[").replace("}", "]")
            prefix += f"previous configuration was as follows:{last_config_str};\n"

            parameter_list = promptlib.hyperparameters
            #prefix += f"Your response must only modify the following parameters: {parameter_list}, and only contain the predicted configuration, in the format ## configuration ##.\n"

            prefix += f"Your response should include:\n"
            prefix += f"1. A detailed reasoning process starting with '## reasoning ##', with each reasoning step on a new line.\n"
            prefix += f"2. The final configuration starting with '## configuration ##', with each parameter-value pair on a new line, separated by ':', only modify these parameters: {parameter_list}.\n"
            prefix += f"Format your response EXACTLY as follows:\n\n"
            prefix += f"## reasoning ##\n"
            prefix += f"1. <first reasoning step>\n"
            prefix += f"2. <second reasoning step>\n"
            prefix += f"...\n\n"
            prefix += f"## configuration ##\n"
            prefix += f"param1:value1,\n"
            prefix += f"param2:value2,\n"
            prefix += f"...\n\n"

            suffix = """
Performance: {A}
Hyperparameter configuration:"""

            few_shot_prompt = FewShotPromptTemplate(
                examples=few_shot_examples,
                example_prompt=example_prompt,
                prefix=prefix,
                suffix=suffix,
                input_variables=["A"],
                example_separator=""
            )

            all_prompt_templates.append(few_shot_prompt)

            query_examples = self._prepare_configurations_acquisition(observed_fvals=jittered_desired_fval, seed=None, shuffle_features=shuffle_features)
            all_query_templates.append(query_examples)

        return all_prompt_templates, all_query_templates
    
    async def _async_generate(self, user_message):
        '''Generate a response from the LLM async.'''
        message = []
        message.append({"role": "system","content": "You are an AI assistant that helps people find information."})
        message.append({"role": "user", "content": user_message})

        MAX_RETRIES = 3

        async with ClientSession(trust_env=True) as session:
            openai.aiosession.set(session)

            resp = None
            for retry in range(MAX_RETRIES):
                try:
                    start_time = time.time()
                    #self.rate_limiter.add_request(request_text=user_message, current_time=start_time)
                    resp = await openai.ChatCompletion.acreate(
                        model=self.chat_engine,
                        messages=message,
                        temperature=0.8,
                        top_p=0.95,
                        n=self.n_gens,
                        request_timeout=300
                    )
                    #self.rate_limiter.add_request(request_token_count=resp['usage']['total_tokens'], current_time=start_time)
                    break
                except Exception as e:
                    print(f'[AF] RETRYING LLM REQUEST {retry+1}/{MAX_RETRIES}...')
                    print(resp)
                    print(e)

        await openai.aiosession.get().close()

        if resp is None:
            return None

        tot_tokens = resp['usage']['total_tokens']
        tot_cost = 0.0015*(resp['usage']['prompt_tokens']/1000) + 0.002*(resp['usage']['completion_tokens']/1000)

        return resp, tot_cost, tot_tokens


    async def _async_generate_concurrently(self, prompt_templates, query_templates):
        '''Perform concurrent generation of responses from the LLM async.'''
        # 用于并发地生成大语言模型 (LLM) 的响应。

        coroutines = []
        for (prompt_template, query_template) in zip(prompt_templates, query_templates):
            print("hzysbpro")
            #并发生成
            word = (prompt_template.format(A=query_template[0]['A'])).replace("<hzt<", "{").replace(">hzt>", "}")
            coroutines.append(self._async_generate(word))

        # coroutines = [self._async_generate(prompt_template.format(A=query_example['A'])) for prompt_template in prompt_templates]
        tasks = [asyncio.create_task(c) for c in coroutines]

        # assert len(tasks) == int(self.n_candidates/self.n_gens)
        assert len(tasks) == int(self.n_templates)

        results = [None]*len(coroutines)

        llm_response = await asyncio.gather(*tasks)

        for idx, response in enumerate(llm_response):
            if response is not None:
                resp, tot_cost, tot_tokens = response
                results[idx] = (resp, tot_cost, tot_tokens)

        return results  # format [(resp, tot_cost, tot_tokens), None, (resp, tot_cost, tot_tokens)]
    
    def _convert_to_json(self, response_str):
        '''Parse LLM response string into JSON.'''
        pairs = response_str.split(',')
        response_json = {}
        for pair in pairs:
            try:
                key, value = [x.strip() for x in pair.split(':')]
            except Exception as e:
                continue

            try:
                response_json[key] = float(value)
            except Exception as e:
                response_json[key] = value
        return response_json
    
    def _filter_candidate_points(self, observed_points, candidate_points, precision=8):
        '''Filter candidate points that already exist in observed points. Also remove duplicates.'''
        # drop points that already exist in observed points
        rounded_observed = [
        {key: round(value, precision) if isinstance(value, (int, float)) else value for key, value in d.items()}
        for d in observed_points
        ]
        rounded_candidate = [
            {key: round(value, precision) if isinstance(value, (int, float)) else value for key, value in d.items()}
            for d in candidate_points
        ]
        filtered_candidates = [x for i, x in enumerate(candidate_points) if rounded_candidate[i] not in rounded_observed]

        def is_within_range(value, allowed_range, default):
            """Check if a value is within an allowed range."""
            value_type, transform, search_range = allowed_range
            if value_type == 'int':
                [min_val, max_val] = search_range
                if transform == 'log' and self.apply_warping:
                    min_val = np.log10(min_val)
                    max_val = np.log10(max_val)
                    if min_val <= value <= max_val:
                        return value
                    else:
                        return default
                else:
                    if not isinstance(value, str):
                        # print(f"Unexpected value type: {type(value)} - {value}")
                        value = str(value)  # 强制转换为字符串
                    match = re.search(r'\d+', value)
                    if match:
                        value = int(match.group())
                        if min_val <= int(value) <= max_val and int(value) == value:
                            return int(value)
                        else:
                            return int(default)
            elif value_type == 'float':                         # THIS MIGHT NEED TO CHANGE, RIGHT NOW IT CAN"T SIT ON THE BOUNDARY
                [min_val, max_val] = search_range
                if transform == 'log' and self.apply_warping:
                    min_val = np.log10(min_val)
                    max_val = np.log10(max_val)
                if min_val <= value <= max_val:
                    return value
                else:
                    return default
            elif value_type == 'ordinal':
                if any(math.isclose(value, x, abs_tol=1e-2) for x in allowed_range[2]):
                    return value
                else:
                    return default
            elif value_type == 'enum':
                if value in search_range:
                    return value
                else:
                    return default
            else:
                raise Exception('Unknown hyperparameter value type')

        def is_dict_within_ranges(d, ranges_dict, default_dict):
            """Check if all values in a dictionary are within their respective allowable ranges."""
            adjusted_dict = {}
            for key in ranges_dict:
                if key in d:
                    adjusted_dict[key] = is_within_range(d[key], ranges_dict[key], default_dict[key])
                else:
                    adjusted_dict[key] = default_dict[key]
            return adjusted_dict

        def filter_dicts_by_ranges(dict_list, ranges_dict, default_dict):
            """Return only those dictionaries where all values are within their respective allowable ranges."""
            return [is_dict_within_ranges(d, ranges_dict, default_dict) for d in dict_list]


        # check that constraints are satisfied
        hyperparameter_constraints = self.task_context['hyperparameter_constraints']

        hyperparameter_default = self.task_context['hyperparameter_default']
        
        filtered_candidates = filter_dicts_by_ranges(filtered_candidates, hyperparameter_constraints, hyperparameter_default)

        filtered_candidates = pd.DataFrame(filtered_candidates)

        # drop duplicates
        filtered_candidates = filtered_candidates.drop_duplicates()
        # reset index
        filtered_candidates = filtered_candidates.reset_index(drop=True)
        return filtered_candidates

    
    def get_candidate_points(self, observed_configs, observed_fvals, 
                             use_feature_semantics=True, use_context='full_context', alpha=-0.2, config = None):
        '''Generate candidate points for acquisition function.'''
        assert alpha >= -1 and alpha <= 1, 'alpha must be between -1 and 1'
        if alpha == 0:
            alpha = -1e-3 # a little bit of randomness never hurt anyone
        self.alpha = alpha

        if self.prompt_setting is not None:
            use_context = self.prompt_setting
        # generate prompt templates
        start_time = time.time()

        # get desired f_val for candidate points
        range = np.abs(np.max(observed_fvals.values) - np.min(observed_fvals.values))

        if range == 0:
            # sometimes there is no variability in y :')
            range = 0.1*np.abs(np.max(observed_fvals.values))
        alpha_range = [0.1, 1e-2, 1e-3, -1e-3, -1e-2, 1e-1]

        if self.lower_is_better:
            self.observed_best = np.min(observed_fvals.values)
            self.observed_worst = np.max(observed_fvals.values)
            desired_fval = self.observed_best - alpha*range
            print(f'Adjusted alpha: {alpha} | [original alpha: {self.alpha}], desired fval: {desired_fval:.6f}')
        else:
            self.observed_best = np.max(observed_fvals.values)
            self.observed_worst = np.min(observed_fvals.values)
            desired_fval = self.observed_best + alpha*range
            print(f'Adjusted alpha: {alpha} | [original alpha: {self.alpha}], desired fval: {desired_fval:.6f}')


        self.desired_fval = desired_fval

        if self.warping_transformer is not None:
            observed_configs = self.warping_transformer.warp(observed_configs)
        
        prompt_templates, query_templates = self._gen_prompt_tempates_acquisitions(observed_configs, observed_fvals, desired_fval, n_prompts=self.n_templates, use_context=use_context, use_feature_semantics=use_feature_semantics, shuffle_features=self.shuffle_features,
                                                                                   promptlib = config)

        print('='*100)
        print('EXAMPLE ACQUISITION PROMPT')
        print(f'Length of prompt templates: {len(prompt_templates)}')
        print(f'Length of query templates: {len(query_templates)}')
        #print(prompt_templates[0].format(A=query_templates[0][0]['A']))
        print('='*100)

        number_candidate_points = 0
        filtered_candidate_points = pd.DataFrame()

        file_path = config.make_file(use_context)

        retry = 0
        while number_candidate_points < 5:
            # 生成LLM的最重要的函数！！！！1
            llm_responses = asyncio.run(self._async_generate_concurrently(prompt_templates, query_templates))

            candidate_points = []
            tot_cost = 0
            tot_tokens = 0
            # loop through n_coroutine async calls
            for response in llm_responses:
                if response is None:
                    continue
                # loop through n_gen responses
                for response_message in response[0]['choices']:
                    response_content = response_message['message']['content']
                    try:
                        long_reason = response_content.split('## configuration ##')[0].strip()
                        clean_reason = long_reason.split('## reason ##')[0].strip()
                        with open(file_path, 'a', encoding='utf-8') as file:
                            file.write(clean_reason + '\n')

                        response_content = response_content.split('## configuration ##')[1].strip()
                        point_f_print = self._convert_to_json(response_content)
                        candidate_points.append(point_f_print)

                    except Exception as e:
                        print("An error occurred:")
                        print(response_content)  # 打印出当前的 response_content 内容
                        print("Error message:", str(e))  # 打印错误信息
                        traceback.print_exc()  # 打印完整的异常堆栈信息
                        continue
                tot_cost += response[1]
                tot_tokens += response[2]

            former_config = config.config
            paras = config.hyperparameters
            former_filtered_points = []
            rule_config_change = []
            adjusted_point = {}
            
            for point in candidate_points:
                for key in former_config:
                    print("原来的参数是：", key, "值是", former_config[key])
                    try:
                        adjusted_point[key] = point[key]
                        print("现在的参数是：", key, "值是", adjusted_point[key])
                    except:
                        adjusted_point[key] = former_config[key]
                        print("没有提示")                     
                former_filtered_points.append(adjusted_point)

            proposed_points = self._filter_candidate_points(observed_configs.to_dict(orient='records'), candidate_points)
            
            print("hzysb", former_config, adjusted_point)
                        
            for key in adjusted_point.keys():
                # 记录规则中的参数变化
                if former_config[key] != adjusted_point[key]:
                    config_change = {"knob": key, "old_value": former_config[key], "new_value": adjusted_point[key]}
                    rule_config_change.append(config_change)
   

            filtered_candidate_points = pd.concat([filtered_candidate_points, proposed_points], ignore_index=True)

            number_candidate_points = filtered_candidate_points.shape[0]

            print(f'Attempt: {retry}, number of proposed candidate points: {len(candidate_points)}, ',
                  f'number of accepted candidate points: {filtered_candidate_points.shape[0]}')


            retry += 1
            if retry > 10:
                print(f'Desired fval: {desired_fval:.6f}')
                print(f'Number of proposed candidate points: {len(candidate_points)}')
                print(f'Number of accepted candidate points: {filtered_candidate_points.shape[0]}')
                if len(candidate_points) > 0:
                    filtered_candidate_points = pd.DataFrame(candidate_points)
                    break
                else:
                    raise Exception('LLM failed to generate candidate points')

        # if self.warping_transformer is not None:
        #     filtered_candidate_points = self.warping_transformer.unwarp(filtered_candidate_points)

        end_time = time.time()
        time_taken = end_time - start_time

        task = self.task_context['hyperparameter_constraints']

        for column in filtered_candidate_points.columns:
            if column in task.keys():
                if task[column][0] == 'int' :
                    filtered_candidate_points[column] = filtered_candidate_points[column].astype(int)


        #filtered_candidate_points = filtered_candidate_points.astype(int)

        # return filtered_candidate_points, tot_cost, time_taken, rule_config_change
        return filtered_candidate_points, tot_cost, time_taken