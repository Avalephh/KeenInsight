import json
import matplotlib.pyplot as plt

# 从 txt 文件中读取 JSON 数据
def load_json_from_txt(file_path):
    with open(file_path, 'r') as f:
        return json.load(f)

# 提取 `score` 数据
def extract_scores(data):
    return [entry['score']['score'] for entry in data]

# 主函数
def plot_scores(file1,file2):
    # 加载数据
    data1 = load_json_from_txt(file1)
    data2 = load_json_from_txt(file2)
    
    # 提取 `score` 值
    scores1 = extract_scores(data1)
    scores2 = extract_scores(data2)
    
    # 创建 x 轴
    x1 = range(len(scores1))
    x2 = range(len(scores2))
    
    # 绘制折线图
    plt.figure(figsize=(10, 6))
    plt.plot(x1, scores1, label="default(4h)", marker='o')
    plt.plot(x2, scores2, label="(min+max)/2(null)", marker='s')

    plt.axhline(y=33.48, color='r', linestyle='--', linewidth=1, label='Threshold (33.48)')
    
    # 添加标题和标签
    plt.title("Latency Changes in TPCH Experiments", fontsize=16)
    plt.xlabel("Iteration", fontsize=14)
    plt.ylabel("Latency", fontsize=14)
    plt.legend(fontsize=12)
    plt.grid(alpha=0.5)
    
    # 显示图表
    plt.tight_layout()
    plt.show()
    plt.savefig("gptuner_tpch-sys_eye-3.14-compare.png")

# 调用函数
plot_scores("/root/sysinsight-main/old_output/database_tpch_TransactionPerSecond-3.13-default.log","/root/sysinsight-main/old_output/database_tpch_TransactionPerSecond-3.14-start.log")
