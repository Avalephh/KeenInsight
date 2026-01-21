# 使用方式
# conda activate sysinsight
# source ~/.zshrc

import sysinsight

# 单行调用
configs, fvals = sysinsight.sysinsight(
    config_file='/home/sysinsight/DBTuner/config/test_005.ini',
    chat_engine='gpt-4o-mini',
    seed=42,
    n_candidates=10,
    n_trials=5
)