import pandas as pd
import json
import configparser
import argparse
from llambo.llambo import LLAMBO
from DBTuner.optimizer import DBTune


class SysInsight:
    """
    SysInsight 主模块，用于数据库参数优化
    """
    
    def __init__(self, config_file='DBTuner/config_test.ini', chat_engine='gpt-4o-mini', seed=42):
        """
        初始化 SysInsight
        
        Args:
            config_file: 配置文件路径
            chat_engine: 使用的聊天引擎
            seed: 随机种子
        """
        self.config_file = config_file
        self.chat_engine = chat_engine
        self.seed = seed
        self.task_context = {}
        
        # 解析命令行参数
        self._parse_args()
        # 读取配置
        self._load_config()
        # 设置任务上下文
        self._setup_task_context()
    
    def _parse_args(self):
        """解析命令行参数"""
        parser = argparse.ArgumentParser(description='DBTuner Configuration Selector')
        parser.add_argument('--config', '-c', type=str, default=self.config_file,
                help='Path to configuration file')
        args = parser.parse_args()
        self.config_file = args.config
    
    def _load_config(self):
        """加载配置文件"""
        config = configparser.ConfigParser()
        config.read(self.config_file)
        self.workload = config['database']['workload']
        self.dbms = config['database']['db']
    
    def _setup_task_context(self):
        """设置任务上下文"""
        db_name = 'dbtune'
        para_name = "MySQL_Parameters"
        
        # 加载基础任务上下文
        with open(f'db_configurations/task/{db_name}.json', 'r') as f:
            self.task_context = json.load(f)
        
        # 根据工作负载设置任务上下文
        if self.workload in ['sysbench', 'tpcc']:
            self.task_context['workload'] = self.workload
            self.task_context['task'] = ''
            self.task_context['lower_is_better'] = False
            self.task_context['dbms'] = self.dbms
        elif self.workload == 'tpch':
            self.task_context['workload'] = self.workload
            self.task_context['task'] = 'tpch'
            self.task_context['lower_is_better'] = True
            self.task_context['dbms'] = self.dbms
        
        # 加载超参数约束
        with open(f'db_configurations/{db_name}.json', 'r') as f:
            self.task_context['hyperparameter_constraints'] = json.load(f)[para_name]
        
        # 加载默认值
        with open(f'db_configurations/init/mysql_default_values.json', 'r') as f:
            self.task_context['hyperparameter_default'] = json.load(f)[0]
    
    def _generate_initialization(self, n_samples):
        """
        生成初始化配置
        
        Args:
            n_samples: 初始化样本数
            
        Returns:
            初始化配置列表
        """
        db_name = 'dbtune'
        init_configs = pd.read_json(f'db_configurations/init/{db_name}.json')
        init_configs = init_configs.to_dict(orient='records')
        assert len(init_configs) == n_samples
        return init_configs
    
    def _dbtune_wrapper(self, config_dict):
        """
        DBTune 包装器
        
        Args:
            config_dict: 配置字典
            
        Returns:
            DBTune 结果
        """
        return DBTune(self.config_file, config_dict)
    
    def optimize(self, n_candidates=10, n_templates=2, n_gens=10, alpha=0.1, 
                 n_initial_samples=1, n_trials=20):
        """
        执行优化
        
        Args:
            n_candidates: 候选数
            n_templates: 模板数
            n_gens: 生成数
            alpha: alpha 参数
            n_initial_samples: 初始样本数
            n_trials: 试验次数
            
        Returns:
            优化后的配置和值
        """
        # 实例化 LLAMBO
        llambo = LLAMBO(
            self.task_context, 
            sm_mode='discriminative', 
            n_candidates=n_candidates, 
            n_templates=n_templates, 
            n_gens=n_gens, 
            alpha=alpha, 
            n_initial_samples=n_initial_samples, 
            n_trials=n_trials, 
            init_f=self._generate_initialization,
            bbox_eval_f=self._dbtune_wrapper, 
            chat_engine=self.chat_engine
        )
        llambo.seed = self.seed
        
        # 运行优化
        configs, fvals = llambo.optimize()
        return configs, fvals


def sysinsight(config_file='DBTuner/config_test.ini', chat_engine='gpt-4o-mini', 
               seed=42, **kwargs):
    """
    简化接口函数
    
    Args:
        config_file: 配置文件路径
        chat_engine: 聊天引擎
        seed: 随机种子
        **kwargs: 优化参数（n_candidates, n_templates, n_gens, alpha, 
                  n_initial_samples, n_trials）
        
    Returns:
        优化结果
    """
    # 创建实例
    optimizer = SysInsight(config_file, chat_engine, seed)
    
    # 提取优化参数
    optimize_kwargs = {
        'n_candidates': kwargs.get('n_candidates', 10),
        'n_templates': kwargs.get('n_templates', 2),
        'n_gens': kwargs.get('n_gens', 10),
        'alpha': kwargs.get('alpha', 0.1),
        'n_initial_samples': kwargs.get('n_initial_samples', 1),
        'n_trials': kwargs.get('n_trials', 20)
    }
    
    # 执行优化
    return optimizer.optimize(**optimize_kwargs)


# 保留原来的直接执行方式
if __name__ == "__main__":
    # 示例用法
    configs, fvals = sysinsight()
    print(f"优化完成，找到 {len(configs)} 个配置")
    for i, (config, fval) in enumerate(zip(configs, fvals)):
        print(f"配置 {i+1}: 分数 = {fval}")
        
# 使用方式
# import sysinsight

# # 单行调用
# configs, fvals = sysinsight.sysinsight(
#     config_file='DBTuner/config_test.ini',
#     chat_engine='gpt-4o-mini',
#     seed=42,
#     n_candidates=10,
#     n_trials=20
# )