source ~/.zshrc

# conda init

# conda activate llambo
start_time=$(date +%s%N)

python main.py

# 记录程序结束时间
end_time=$(date +%s%N)

# 计算运行时间（单位：毫秒）
elapsed_time=$(( (end_time - start_time) / 1000000 ))

# 输出运行时间
echo "程序运行时间: ${elapsed_time} 毫秒"