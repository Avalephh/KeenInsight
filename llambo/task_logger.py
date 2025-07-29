import os
import json

class TaskLogger:
    def __init__(self, task, log_dir):
        
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)    
        self.log_dir = log_dir
        self.task = task
        self.log_filename = self.generate_log_filename()
        self.log_file_path = os.path.join(self.log_dir, self.log_filename)
        self.logs = []
        self.load_log_file()

    def sanitize_filename(self, name):
        return ''.join(e for e in name if e.isalnum())

    def generate_log_filename(self):
        model = self.sanitize_filename(self.task.get('model', ''))
        task_name = self.sanitize_filename(self.task.get('task', ''))
        metric = self.sanitize_filename(self.task.get('metric', ''))

        filename = f"{model}_{task_name}_{metric}.log"
        return filename

    def load_log_file(self):
        if os.path.exists(self.log_file_path):
            with open(self.log_file_path, 'r') as log_file:
                logs = json.load(log_file)
            print(f"Loaded logs from {self.log_file_path}:")
            self.logs = logs
        else:
            with open(self.log_file_path, 'w') as log_file:
                json.dump([], log_file, indent=4)
            self.logs = []
        
    def insert_log_entry(self, entry):
        if not isinstance(entry, dict):
            raise ValueError("Log entry must be a dictionary.")
        self.logs.append(entry)
        self.save_log_file()
    
    def save_log_file(self):
        with open(self.log_file_path, 'w') as log_file:
            json.dump(self.logs, log_file, indent=4)


if __name__ == "__main__":
    task_context = {}
    with open(f'../db_configurations/task/dbtune.json', 'r') as f:
        task_context = json.load(f)
    # 创建 TaskLogger 实例
    task_logger = TaskLogger(task_context, "./old_output")
    
    # 示例：插入新的日志项
    new_log_entry = {
        "timestamp": "2023-10-01 12:00:00",
        "event": "Task started",
        "details": task_logger.task
    }
    task_logger.insert_log_entry(new_log_entry)
    
