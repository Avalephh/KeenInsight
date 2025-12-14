import os
import sys
import time
import yaml
import psutil
import subprocess
import threading
import matplotlib.pyplot as plt
from datetime import datetime

class BenchmarkRunner:
    def __init__(self, config_path):
        self.config_path = config_path
        self.load_config()
        self.stats = {
            'time': [],
            'cpu': [],
            'memory': []
        }
        self.running = False
        # 存储上一次采样的 CPU 时间
        self.last_cpu_times = {}
        self.last_sample_time = None
        # 缓存主进程，避免重复查找
        self._main_process = None

    def load_config(self):
        if not os.path.exists(self.config_path):
            print(f"Error: Config file {self.config_path} not found.")
            sys.exit(1)
        
        with open(self.config_path, 'r') as f:
            self.config = yaml.safe_load(f)

    def find_pg_main_process(self):
        """
        自动查找 PostgreSQL 主进程
        返回主进程的 PID
        """
        try:
            # 方法1: 通过进程名查找 PostgreSQL 主进程
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    # 检查进程名
                    if proc.info['name'] in ['postgres', 'postgresql']:
                        cmdline = proc.info['cmdline'] or []
                        # 检查命令行参数，查找包含 -D 的主进程（数据目录参数）
                        if any('-D' in arg for arg in cmdline):
                            return proc.info['pid']
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            # 方法2: 如果上面的方法找不到，尝试查找所有 postgres 进程中最老的
            postgres_procs = []
            for proc in psutil.process_iter(['pid', 'name', 'create_time']):
                try:
                    if proc.info['name'] in ['postgres', 'postgresql']:
                        postgres_procs.append(proc.info)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            if postgres_procs:
                # 返回创建时间最早的进程（通常是主进程）
                oldest_proc = min(postgres_procs, key=lambda x: x['create_time'])
                return oldest_proc['pid']
            
            # 方法3: 使用 pgrep 命令作为后备
            try:
                result = subprocess.run(['pgrep', '-o', 'postgres'], 
                                      capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    return int(result.stdout.strip())
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
                
        except Exception as e:
            print(f"Error finding PostgreSQL process: {e}")
        
        return None

    def get_pg_process(self):
        """
        获取 PostgreSQL 主进程
        优先使用配置文件中的 PID，如果没有则自动查找
        """
        # 如果已经缓存了主进程，直接返回
        if self._main_process:
            try:
                self._main_process.memory_info()  # 检查进程是否还存在
                return self._main_process
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._main_process = None

        pid = self.config.get('target_pid')

        if pid:
            try:
                p = psutil.Process(pid)
                self._main_process = p
                return p
            except psutil.NoSuchProcess:
                print(f"Warning: Configured PID {pid} not found, trying to find automatically...")
            except psutil.AccessDenied:
                print(f"Warning: Access denied to configured PID {pid}, trying to find automatically...")

        # 自动查找 PostgreSQL 进程
        print("Searching for PostgreSQL main process...")
        pid = self.find_pg_main_process()

        if pid:
            print(f"Found PostgreSQL main process with PID: {pid}")
            try:
                p = psutil.Process(pid)
                self._main_process = p
                return p
            except psutil.NoSuchProcess:
                print(f"Error: Found PID {pid} but process no longer exists.")
                return None
            except psutil.AccessDenied:
                print(f"Error: Access denied to PID {pid}. Try running as root/sudo.")
                return None
        else:
            print("Error: Could not find PostgreSQL main process.")
            print("Please ensure PostgreSQL is running, or specify target_pid in config.yaml")
            return None

    def get_all_pg_processes(self):
        """获取 PostgreSQL 主进程及其所有子进程"""
        main_process = self.get_pg_process()
        if not main_process:
            return []
        
        processes = [main_process]
        try:
            children = main_process.children(recursive=True)
            processes.extend(children)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        
        return processes

    def get_process_cpu_times(self, process):
        """获取进程的 CPU 时间（用户态 + 内核态）"""
        try:
            times = process.cpu_times()
            return times.user + times.system
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def calculate_cpu_percent(self, processes, elapsed_time):
        """
        手动计算 CPU 使用率
        CPU% = (当前CPU时间 - 上次CPU时间) / 采样间隔 * 100
        """
        if elapsed_time <= 0:
            return 0.0
        
        total_cpu_percent = 0.0
        current_cpu_times = {}
        
        for p in processes:
            try:
                pid = p.pid
                cpu_time = self.get_process_cpu_times(p)
                if cpu_time is None:
                    continue
                
                current_cpu_times[pid] = cpu_time
                
                if pid in self.last_cpu_times:
                    cpu_delta = cpu_time - self.last_cpu_times[pid]
                    # CPU 使用率 = CPU时间增量 / 实际时间 * 100
                    cpu_percent = (cpu_delta / elapsed_time) * 100
                    total_cpu_percent += cpu_percent
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        # 更新上次的 CPU 时间
        self.last_cpu_times = current_cpu_times
        
        return total_cpu_percent

    def monitor_resources(self, pid):
        print(f"Starting monitoring for PostgreSQL processes...")

        # 获取 CPU 核心数
        cpu_count = psutil.cpu_count()
        print(f"System has {cpu_count} CPU cores")

        self.last_sample_time = time.time()
        start_time = self.last_sample_time

        # 等待一小段时间再开始正式采样
        time.sleep(0.5)

        sample_interval = 0.5  # 采样间隔 500ms
        sample_count = 0

        while self.running:
            try:
                current_time = time.time()
                elapsed = current_time - self.last_sample_time

                # 每次都重新获取所有 PostgreSQL 进程（包括动态创建的 worker）
                current_processes = self.get_all_pg_processes()

                # 计算 CPU 使用率
                total_cpu = self.calculate_cpu_percent(current_processes, elapsed)

                # 计算内存使用
                total_memory = 0.0
                for p in current_processes:
                    try:
                        mem_info = p.memory_info()
                        total_memory += mem_info.rss / 1024 / 1024
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

                relative_time = current_time - start_time

                self.stats['time'].append(relative_time)
                self.stats['cpu'].append(total_cpu)
                self.stats['memory'].append(total_memory)

                self.last_sample_time = current_time
                sample_count += 1

                # 每10个采样点输出一次进程数量状态
                if sample_count % 10 == 0:
                    print(f"Sample {sample_count}: {len(current_processes)} processes, CPU: {total_cpu:.2f}%, Memory: {total_memory:.2f}MB")

                time.sleep(sample_interval)
            except Exception as e:
                print(f"Monitor error: {e}")
                import traceback
                traceback.print_exc()
                break

    def prepare_sysbench(self):
        """Prepare sysbench tables if not exist"""
        print("Preparing sysbench tables...")
        db_conf = self.config['database']
        sb_conf = self.config['sysbench']
        
        cmd = [
            "sysbench",
            "oltp_read_write",
            f"--db-driver=pgsql",
            f"--pgsql-host={db_conf['host']}",
            f"--pgsql-port={db_conf['port']}",
            f"--pgsql-user={db_conf['user']}",
            f"--pgsql-password={db_conf['password']}",
            f"--pgsql-db={db_conf['db_name']}",
            f"--tables={sb_conf['tables']}",
            f"--table-size={sb_conf['table_size']}",
            "prepare"
        ]
        
        try:
            subprocess.run(cmd, check=True)
            print("Sysbench prepare finished.")
        except subprocess.CalledProcessError as e:
            print(f"Sysbench prepare failed: {e}")

    def run_sysbench(self):
        db_conf = self.config['database']
        sb_conf = self.config['sysbench']
        
        cmd = [
            "sysbench",
            f"tools/sysbench_lua/fluctuating_oltp.lua",
            f"--db-driver=pgsql",
            f"--pgsql-host={db_conf['host']}",
            f"--pgsql-port={db_conf['port']}",
            f"--pgsql-user={db_conf['user']}",
            f"--pgsql-password={db_conf['password']}",
            f"--pgsql-db={db_conf['db_name']}",
            f"--threads={sb_conf['threads']}",
            f"--time={sb_conf['time']}",
            f"--tables={sb_conf['tables']}",
            f"--table-size={sb_conf['table_size']}",
            "run"
        ]
        
        print(f"Running sysbench command: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"Sysbench execution failed: {e}")

    def plot_results(self):
        if not self.stats['time']:
            print("No data collected to plot.")
            return

        print(f"\n=== Statistics ===")
        print(f"Collected {len(self.stats['time'])} data points.")
        print(f"CPU range: {min(self.stats['cpu']):.2f}% - {max(self.stats['cpu']):.2f}%")
        print(f"CPU average: {sum(self.stats['cpu'])/len(self.stats['cpu']):.2f}%")
        print(f"Memory range: {min(self.stats['memory']):.2f}MB - {max(self.stats['memory']):.2f}MB")

        cpu_count = psutil.cpu_count()
        
        plt.figure(figsize=(14, 8))
        
        # CPU Plot
        plt.subplot(2, 1, 1)
        plt.plot(self.stats['time'], self.stats['cpu'], 'b-', linewidth=0.8, label=f'CPU Usage (%)')
        plt.fill_between(self.stats['time'], self.stats['cpu'], alpha=0.3)
        plt.title('PostgreSQL Resource Usage During Sysbench Test (All Processes)')
        plt.ylabel(f'CPU % (max theoretical={cpu_count*100}%)')
        plt.grid(True, alpha=0.3)
        plt.legend()
        
        # Memory Plot
        plt.subplot(2, 1, 2)
        plt.plot(self.stats['time'], self.stats['memory'], 'r-', linewidth=0.8, label='Memory Usage (MB)')
        plt.fill_between(self.stats['time'], self.stats['memory'], alpha=0.3, color='red')
        plt.xlabel('Time (s)')
        plt.ylabel('Memory (MB)')
        plt.grid(True, alpha=0.3)
        plt.legend()
        
        plt.tight_layout()
        output_file = f"tools/benchmark_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        plt.savefig(output_file, dpi=150)
        print(f"Graph saved to {output_file}")

    def run(self):
        # 1. Check PID
        main_process = self.get_pg_process()
        if not main_process:
            print("PostgreSQL main process not found, exiting.")
            return
        
        pid = main_process.pid

        # 2. Prepare (Optional, usually run once)
        # self.prepare_sysbench()

        # 3. Start Monitor Thread
        self.running = True
        monitor_thread = threading.Thread(target=self.monitor_resources, args=(pid,))
        monitor_thread.start()

        # 4. Run Sysbench
        self.run_sysbench()

        # 5. Stop Monitor
        self.running = False
        monitor_thread.join()

        # 6. Plot
        self.plot_results()

if __name__ == "__main__":
    runner = BenchmarkRunner("tools/config.yaml")
    runner.run()