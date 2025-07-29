import os
import shutil

# 定义文件夹路径
source_folder = '/root/AI4DB/hzt/library/knobs_library/test'
destination_folder = '/root/AI4DB/hzt/library/knobs_library/code_new'

# 如果目标文件夹不存在，则创建它
os.makedirs(destination_folder, exist_ok=True)

# 遍历源文件夹
for filename in os.listdir(source_folder):
    # 判断文件是否以 'function.txt' 结尾
    if filename.endswith('function.txt'):
        # 提取 knob 名称并生成新文件名
        knob_name = filename.split('-')[1].split('_function.txt')[0]  # 获取 knob 名称
        new_filename = f"{knob_name}_code.txt"
        
        # 源文件路径和目标文件路径
        src_path = os.path.join(source_folder, filename)
        dest_path = os.path.join(destination_folder, new_filename)
        
        # 复制并重命名文件
        shutil.copy(src_path, dest_path)
        print(f"文件 '{filename}' 已复制并重命名为 '{new_filename}' 到 '{destination_folder}' 文件夹中。")