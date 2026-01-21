#!/usr/bin/env python3
"""
回放对比测试脚本
1. 备份数据库状态
2. 运行 sysbench 测试并记录开始/结束时间戳
3. 从 pgaudit 日志中精确提取这段时间的 SQL
4. 恢复数据库到备份状态
5. 调用 replay API 进行回放
6. 对比原始和回放的 CPU/内存曲线

每次测试的所有文件都保存在独立目录中，并支持添加测试描述。
"""

import os
import sys
import time
import json
import yaml
import psutil
import requests
import subprocess
import threading
import re
import shutil
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

# 禁用代理，确保直接连接到本地服务
os.environ['NO_PROXY'] = 'localhost,127.0.0.1'
os.environ['no_proxy'] = 'localhost,127.0.0.1'

class ReplayCompareRunner:
    def __init__(self, config_path="tools/config.yaml", output_dir=None, description=""):
        self.config_path = config_path
        self.load_config()
        
        # 测试描述
        self.description = description
        
        # 输出目录
        self.base_output_dir = "tools/test_results"
        self.output_dir = output_dir
        self.timestamp = None
        
        # 原始数据和回放数据
        self.original_stats = {
            'time': [],
            'cpu': [],
            'memory': []
        }
        self.replay_stats = {
            'time': [],
            'cpu': [],
            'memory': []
        }
        
        self.running = False
        self.last_cpu_times = {}
        self.last_sample_time = None
        self._main_process = None
        
        # API 配置
        self.api_base = self.config.get('api', {}).get('base_url', 'http://localhost:8080')
        
        # PostgreSQL 日志文件路径
        self.pg_log_path = self.config.get('postgresql', {}).get('log_path', 
            '/opt/homebrew/var/log/postgresql@18.log')
        
        # 测试时间记录
        self.test_start_time = None
        self.test_end_time = None
        self.replay_start_time = None
        self.replay_end_time = None
        
        # 测试结果
        self.test_result = {
            'description': description,
            'timestamp': None,
            'task_id': None,
            'original': {},
            'replay': {},
            'divergence': {},
            'files': {}
        }
    
    def init_output_dir(self):
        """初始化输出目录"""
        self.timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        if self.output_dir is None:
            self.output_dir = os.path.join(self.base_output_dir, f"test_{self.timestamp}")
        
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.base_output_dir, exist_ok=True)
        
        self.test_result['timestamp'] = self.timestamp
        print(f"Test output directory: {self.output_dir}")
        return self.output_dir
    
    def get_output_path(self, filename):
        """获取输出文件路径"""
        return os.path.join(self.output_dir, filename)
        
    def load_config(self):
        if not os.path.exists(self.config_path):
            print(f"Error: Config file {self.config_path} not found.")
            sys.exit(1)
        
        with open(self.config_path, 'r') as f:
            self.config = yaml.safe_load(f)

    def find_pg_main_process(self):
        """自动查找 PostgreSQL 主进程"""
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if proc.info['name'] in ['postgres', 'postgresql']:
                        cmdline = proc.info['cmdline'] or []
                        if any('-D' in arg for arg in cmdline):
                            return proc.info['pid']
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            postgres_procs = []
            for proc in psutil.process_iter(['pid', 'name', 'create_time']):
                try:
                    if proc.info['name'] in ['postgres', 'postgresql']:
                        postgres_procs.append(proc.info)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            if postgres_procs:
                oldest_proc = min(postgres_procs, key=lambda x: x['create_time'])
                return oldest_proc['pid']
                
        except Exception as e:
            print(f"Error finding PostgreSQL process: {e}")
        
        return None

    def get_pg_process(self):
        """获取 PostgreSQL 主进程"""
        if self._main_process:
            try:
                self._main_process.memory_info()
                return self._main_process
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._main_process = None

        print("Searching for PostgreSQL main process...")
        pid = self.find_pg_main_process()

        if pid:
            print(f"Found PostgreSQL main process with PID: {pid}")
            try:
                p = psutil.Process(pid)
                self._main_process = p
                return p
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                print(f"Error accessing process: {e}")
                return None
        else:
            print("Error: Could not find PostgreSQL main process.")
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
        """获取进程的 CPU 时间"""
        try:
            times = process.cpu_times()
            return times.user + times.system
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def calculate_cpu_percent(self, processes, elapsed_time):
        """计算 CPU 使用率"""
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
                    cpu_percent = (cpu_delta / elapsed_time) * 100
                    total_cpu_percent += cpu_percent
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        self.last_cpu_times = current_cpu_times
        return total_cpu_percent

    def monitor_resources(self, stats_dict):
        """监控资源使用"""
        print(f"Starting resource monitoring...")
        
        cpu_count = psutil.cpu_count()
        print(f"System has {cpu_count} CPU cores")
        
        self.last_sample_time = time.time()
        start_time = self.last_sample_time
        self.last_cpu_times = {}
        
        time.sleep(0.5)
        
        sample_interval = 0.5
        sample_count = 0
        
        while self.running:
            try:
                current_time = time.time()
                elapsed = current_time - self.last_sample_time
                
                current_processes = self.get_all_pg_processes()
                total_cpu = self.calculate_cpu_percent(current_processes, elapsed)
                
                total_memory = 0.0
                for p in current_processes:
                    try:
                        mem_info = p.memory_info()
                        total_memory += mem_info.rss / 1024 / 1024
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                
                relative_time = current_time - start_time
                
                stats_dict['time'].append(relative_time)
                stats_dict['cpu'].append(total_cpu)
                stats_dict['memory'].append(total_memory)
                
                self.last_sample_time = current_time
                sample_count += 1
                
                if sample_count % 10 == 0:
                    print(f"Sample {sample_count}: {len(current_processes)} processes, CPU: {total_cpu:.2f}%, Memory: {total_memory:.2f}MB")
                
                time.sleep(sample_interval)
            except Exception as e:
                print(f"Monitor error: {e}")
                import traceback
                traceback.print_exc()
                break
    
    # ==================== 数据库备份恢复功能 ====================
    
    def backup_database(self, backup_file=None):
        """
        备份数据库（在采集前执行）
        使用 pg_dump 进行逻辑备份
        """
        db_conf = self.config['database']
        
        if backup_file is None:
            backup_file = self.get_output_path("database_backup.sql")
        
        print(f"\n=== Backing up database ===")
        print(f"Host: {db_conf['host']}:{db_conf['port']}")
        print(f"Database: {db_conf['db_name']}")
        print(f"Backup file: {backup_file}")
        
        # 设置环境变量避免密码提示
        env = os.environ.copy()
        env['PGPASSWORD'] = db_conf['password']
        
        # 使用 pg_dump 备份
        # -c: 在恢复前先清理（DROP）已存在的对象
        # --if-exists: DROP 时加上 IF EXISTS
        cmd = [
            "pg_dump",
            "-h", db_conf['host'],
            "-p", str(db_conf['port']),
            "-U", db_conf['user'],
            "-d", db_conf['db_name'],
            "-c", "--if-exists",  # 清理模式
            "-f", backup_file
        ]
        
        try:
            print(f"Running: {' '.join(cmd)}")
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
            
            if result.returncode == 0:
                file_size = os.path.getsize(backup_file) / 1024 / 1024
                print(f"Backup completed successfully! Size: {file_size:.2f} MB")
                self.test_result['files']['database_backup'] = backup_file
                return backup_file
            else:
                print(f"Backup failed: {result.stderr}")
                return None
                
        except subprocess.TimeoutExpired:
            print("Error: Backup timed out")
            return None
        except FileNotFoundError:
            print("Error: pg_dump command not found. Make sure PostgreSQL client tools are installed.")
            return None
        except Exception as e:
            print(f"Error during backup: {e}")
            return None
    
    def restore_database(self, backup_file):
        """
        恢复数据库（在回放前执行）
        使用 psql 恢复 SQL 备份
        """
        db_conf = self.config['database']
        
        if not os.path.exists(backup_file):
            print(f"Error: Backup file not found: {backup_file}")
            return False
        
        print(f"\n=== Restoring database ===")
        print(f"Host: {db_conf['host']}:{db_conf['port']}")
        print(f"Database: {db_conf['db_name']}")
        print(f"Backup file: {backup_file}")
        
        # 设置环境变量
        env = os.environ.copy()
        env['PGPASSWORD'] = db_conf['password']
        
        # 使用 psql 恢复
        cmd = [
            "psql",
            "-h", db_conf['host'],
            "-p", str(db_conf['port']),
            "-U", db_conf['user'],
            "-d", db_conf['db_name'],
            "-f", backup_file,
            "-q"  # 安静模式
        ]
        
        try:
            print(f"Running: {' '.join(cmd)}")
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
            
            if result.returncode == 0:
                print("Database restored successfully!")
                return True
            else:
                # psql 可能会输出一些警告但仍然成功
                if "ERROR" in result.stderr:
                    print(f"Restore had errors: {result.stderr[:500]}")
                    return False
                else:
                    print("Database restored (with warnings)")
                    return True
                
        except subprocess.TimeoutExpired:
            print("Error: Restore timed out")
            return False
        except FileNotFoundError:
            print("Error: psql command not found. Make sure PostgreSQL client tools are installed.")
            return False
        except Exception as e:
            print(f"Error during restore: {e}")
            return False
    
    def backup_sysbench_tables(self, backup_file=None):
        """
        只备份 sysbench 表（更快的备份方式）
        """
        db_conf = self.config['database']
        sb_conf = self.config['sysbench']
        
        if backup_file is None:
            backup_file = self.get_output_path("sysbench_backup.sql")
        
        print(f"\n=== Backing up sysbench tables ===")
        
        # 设置环境变量
        env = os.environ.copy()
        env['PGPASSWORD'] = db_conf['password']
        
        # 获取要备份的表
        tables = [f"sbtest{i}" for i in range(1, sb_conf['tables'] + 1)]
        
        # 构建 pg_dump 命令，只备份指定的表
        cmd = [
            "pg_dump",
            "-h", db_conf['host'],
            "-p", str(db_conf['port']),
            "-U", db_conf['user'],
            "-d", db_conf['db_name'],
            "-c", "--if-exists",
        ]
        
        # 添加表参数
        for table in tables:
            cmd.extend(["-t", table])
        
        cmd.extend(["-f", backup_file])
        
        try:
            print(f"Backing up tables: {', '.join(tables)}")
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
            
            if result.returncode == 0:
                file_size = os.path.getsize(backup_file) / 1024 / 1024
                print(f"Backup completed! Size: {file_size:.2f} MB")
                self.test_result['files']['sysbench_backup'] = backup_file
                return backup_file
            else:
                print(f"Backup failed: {result.stderr}")
                return None
                
        except Exception as e:
            print(f"Error during backup: {e}")
            return None
    
    # ==================== 审计日志提取 ====================
    
    def extract_audit_logs_by_time(self, start_time, end_time, username, output_file):
        """根据时间范围和用户名从 pgaudit 日志中提取 SQL"""
        print(f"\n=== Extracting audit logs ===")
        print(f"Time range: {start_time} to {end_time}")
        print(f"Username: {username}")
        print(f"Log file: {self.pg_log_path}")
        
        if not os.path.exists(self.pg_log_path):
            print(f"Error: PostgreSQL log file not found: {self.pg_log_path}")
            return 0
        
        # 支持 Unix 时间戳 (float) 和 标准时间格式
        start_ts = start_time.timestamp()
        end_ts = end_time.timestamp()
        
        start_str = start_time.strftime('%Y-%m-%d %H:%M:%S')
        end_str = end_time.strftime('%Y-%m-%d %H:%M:%S')
        
        count = 0
        matched_lines = []
        
        # 匹配 standard stderr format: time=2023-01-01 12:00:00
        time_pattern_std = re.compile(r'time=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
        # 匹配 CSV/Unix format: user,db,1768995830.784,...
        # assuming timestamp is the 3rd field, but let's be more loose: find a large float early in the line
        time_pattern_unix = re.compile(r'^[^,]+,[^,]+,(\d{10}\.\d+)')
        
        print(f"Reading log file (this may take a moment)...")
        
        test_duration = (end_time - start_time).total_seconds()
        estimated_lines = int(test_duration * 60000)
        estimated_lines = max(estimated_lines, 500000)
        
        print(f"Estimated lines to scan: {estimated_lines}")
        
        try:
            # 简化 grep，只查找 AUDIT:，不强制 user=username 因为格式可能是 csv
            # 用户名过滤在 Python 中通过简单字符串检查进行 (CSV格式 user 在行首)
            cmd = f"tail -n {estimated_lines} '{self.pg_log_path}' | grep 'AUDIT:'"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
            lines = result.stdout.strip().split('\n') if result.stdout.strip() else []
            
            print(f"Found {len(lines)} audit log candidates (before filtering)")
            
            for line in lines:
                if not line.strip():
                    continue
                
                # Filter by username if it appears in the line (simple check)
                if username and username not in line:
                    continue

                # Check Timestamp
                matched = False
                
                # Try Unix timestamp first (observed format)
                match = time_pattern_unix.search(line)
                if match:
                    try:
                        ts = float(match.group(1))
                        if start_ts - 1 <= ts <= end_ts + 1: # Add 1s buffer
                            matched_lines.append(line)
                            count += 1
                            matched = True
                    except:
                        pass
                
                if not matched:
                    # Try Standard format
                    match = time_pattern_std.search(line)
                    if match:
                        log_time_str = match.group(1)
                        if start_str <= log_time_str <= end_str:
                            matched_lines.append(line)
                            count += 1
            
            print(f"Matched {count} lines in time range")
            
            with open(output_file, 'w') as f:
                for line in matched_lines:
                    f.write(line + '\n')
            
            print(f"Saved to {output_file}")
            
        except subprocess.TimeoutExpired:
            print("Error: Log extraction timed out")
        except Exception as e:
            print(f"Error extracting logs: {e}")
            import traceback
            traceback.print_exc()
        
        return count
    
    # ==================== 审计日志解析与验证 ====================
    
    def parse_audit_log_file(self, log_file):
        """
        解析审计日志文件，提取每条 SQL 的关键信息
        返回: 列表，每个元素包含 session_id, vxid, operation, sql_key, sql_preview
        """
        # 通用正则表达式，提取 session, vxid 和 AUDIT 内容
        # 格式: session=xxx xid=xxx vxid=xxx LOG:  AUDIT: SESSION,num,num,TYPE,OP,,,SQL,...
        pattern_main = re.compile(
            r'session=(\S+)\s+xid=(\d+)\s+vxid=(\S+)\s+LOG:\s+AUDIT:\s+\w+,\d+,\d+,(\w+),(\w+),.*?,(.+)$'
        )
        
        # 备用格式 vxid=... tx=...
        pattern_vxid_tx = re.compile(
            r'session=(\S+)\s+vxid=(\S+)\s+tx=(\d+)\s+LOG:\s+AUDIT:\s+\w+,\d+,\d+,(\w+),(\w+),.*?,(.+)$'
        )
        
        # 备用格式 只有 tx=...
        pattern_tx_only = re.compile(
            r'session=(\S+)\s+tx=(\d+)\s+LOG:\s+AUDIT:\s+\w+,\d+,\d+,(\w+),(\w+),.*?,(.+)$'
        )
        
        # CSV-like format observed: user,db,timestamp,sqlstate,session_id,vxid,query_id/txid? LOG: AUDIT: ...
        # Example: test,test,1768995830.784,00000,6970bbec.c0c0,28/296,-5454431914034686354 LOG:  AUDIT: ...
        pattern_csv = re.compile(
            r'^[^,]+,[^,]+,(\d+\.\d+),[^,]+,([^,]+),([^,]+),[^ ]+ LOG:\s+AUDIT:\s+\w+,\d+,\d+,(\w+),(\w+),.*?,(.+)$'
        )
        
        # 时间戳匹配
        time_pattern = re.compile(r'time=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?)')
        
        results = []
        parse_errors = 0
        
        try:
            with open(log_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or 'AUDIT:' not in line:
                        continue
                    
                    session_id = None
                    vxid = None
                    sql_type = None
                    operation = None
                    sql_part = None
                    timestamp = None
                    
                    # 尝试 CSV 格式
                    match = pattern_csv.search(line)
                    if match:
                        try:
                            ts_float = float(match.group(1))
                            timestamp = datetime.fromtimestamp(ts_float)
                        except:
                            pass
                        
                        session_id = match.group(2)
                        vxid = match.group(3)
                        sql_type = match.group(4)
                        operation = match.group(5)
                        sql_part = match.group(6)
                    else:
                        # 尝试标准格式
                        
                        # 尝试匹配时间戳
                        time_match = time_pattern.search(line)
                        if time_match:
                            try:
                                ts_str = time_match.group(1)
                                if '.' in ts_str:
                                    dt = datetime.strptime(ts_str.split('.')[0], '%Y-%m-%d %H:%M:%S')
                                    timestamp = dt
                                else:
                                    timestamp = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                            except:
                                pass
                        
                        # 尝试匹配主格式 (xid vxid)
                        match = pattern_main.search(line)
                        if match:
                            session_id = match.group(1)
                            # xid = match.group(2)
                            vxid = match.group(3)
                            sql_type = match.group(4)
                            operation = match.group(5)
                            sql_part = match.group(6)
                        else:
                            # 尝试 vxid tx 格式
                            match = pattern_vxid_tx.search(line)
                            if match:
                                session_id = match.group(1)
                                vxid = match.group(2)
                                # tx = match.group(3)
                                sql_type = match.group(4)
                                operation = match.group(5)
                                sql_part = match.group(6)
                            else:
                                # 尝试只有 tx 格式
                                match = pattern_tx_only.search(line)
                                if match:
                                    session_id = match.group(1)
                                    tx = match.group(2)
                                    vxid = f"tx/{tx}"  # 生成虚拟 vxid
                                    sql_type = match.group(3)
                                    operation = match.group(4)
                                    sql_part = match.group(5)
                    
                    if not match:
                        parse_errors += 1
                        if parse_errors <= 3:
                            print(f"  Parse warning: Could not match line pattern")
                        continue
                    
                    # 从 sql_part 中提取 SQL 语句
                    # 格式可能是: ,"SQL",params,rows 或 ,SQL,params,rows
                    sql = None
                    if sql_part:
                        # 去掉开头的逗号
                        if sql_part.startswith(','):
                            sql_part = sql_part[1:]
                        
                        # 尝试提取带引号的 SQL
                        if sql_part.startswith('"'):
                            quote_end = sql_part.find('",', 1)
                            if quote_end > 0:
                                sql = sql_part[1:quote_end]
                            else:
                                # 可能整个都是 SQL 或只有结尾引号
                                sql = sql_part.strip('"')
                        else:
                            # 不带引号的 SQL（如 BEGIN, COMMIT, 或简单语句）
                            parts = sql_part.split(',')
                            if parts:
                                sql = parts[0]
                    
                    if session_id and vxid and sql:
                        sql = sql.replace('""', '"') # Unescape CSV quotes if needed
                        
                        sql_preview = sql[:100] if len(sql) > 100 else sql
                        # 生成一个标识符用于匹配（操作类型 + SQL 模板）
                        sql_key = f"{operation}:{sql[:200]}"
                        results.append({
                            'session_id': session_id,
                            'vxid': vxid,
                            'sql_type': sql_type,
                            'operation': operation,
                            'sql_key': sql_key,
                            'sql_preview': sql_preview,
                            'timestamp': timestamp
                        })
                        
        except Exception as e:
            print(f"Error parsing log file: {e}")
            import traceback
            traceback.print_exc()
        
        if parse_errors > 3:
            print(f"  Total parse warnings: {parse_errors}")
        
        return results
    
    def verify_replay_consistency(self, original_log_file, replay_log_file):
        """
        验证回放的一致性：
        1. 原来在同一个连接的语句回放时还在同一个连接
        2. 原来在同一个事务中的语句回放时还在同一个事务
        
        注意：原始和回放的 vxid 是不同的标识符（PostgreSQL 在不同执行时分配不同的 vxid），
        所以我们通过事务大小分布来验证事务一致性。
        
        返回验证结果字典
        """
        print(f"\n" + "="*60)
        print("VERIFYING REPLAY CONSISTENCY")
        print("="*60)
        
        # 解析原始日志
        print(f"\nParsing original audit log: {original_log_file}")
        original_stmts = self.parse_audit_log_file(original_log_file)
        print(f"  Parsed {len(original_stmts)} statements")
        
        # 解析回放日志
        print(f"Parsing replay audit log: {replay_log_file}")
        replay_stmts = self.parse_audit_log_file(replay_log_file)
        print(f"  Parsed {len(replay_stmts)} statements")
        
        if not original_stmts or not replay_stmts:
            print("Warning: Not enough data to verify consistency")
            return {
                'verified': False,
                'reason': 'Insufficient data',
                'original_count': len(original_stmts),
                'replay_count': len(replay_stmts)
            }
        
        # ========== 验证1: Session 数量是否一致 ==========
        print(f"\n--- Verifying Session Consistency ---")
        
        original_sessions = set(stmt['session_id'] for stmt in original_stmts)
        replay_sessions = set(stmt['session_id'] for stmt in replay_stmts)
        
        print(f"  Original sessions: {len(original_sessions)}")
        print(f"  Replay sessions: {len(replay_sessions)}")
        
        session_match = len(original_sessions) == len(replay_sessions)
        print(f"  Session count match: {'✓' if session_match else '✗'}")
        
        # ========== 验证2: 事务结构分析 ==========
        print(f"\n--- Verifying Transaction Structure ---")
        
        # 按 vxid 分组（vxid 是事务的唯一标识）
        original_vxid_groups = {}
        for stmt in original_stmts:
            vxid = stmt['vxid']
            if vxid not in original_vxid_groups:
                original_vxid_groups[vxid] = []
            original_vxid_groups[vxid].append(stmt)
        
        replay_vxid_groups = {}
        for stmt in replay_stmts:
            vxid = stmt['vxid']
            if vxid not in replay_vxid_groups:
                replay_vxid_groups[vxid] = []
            replay_vxid_groups[vxid].append(stmt)
        
        print(f"  Original transactions (by vxid): {len(original_vxid_groups)}")
        print(f"  Replay transactions (by vxid): {len(replay_vxid_groups)}")
        
        # 统计事务大小分布
        def get_size_distribution(groups):
            dist = {}
            for stmts in groups.values():
                size = len(stmts)
                dist[size] = dist.get(size, 0) + 1
            return dist
        
        original_dist = get_size_distribution(original_vxid_groups)
        replay_dist = get_size_distribution(replay_vxid_groups)
        
        print(f"\n  Original transaction size distribution (top 5):")
        for size, count in sorted(original_dist.items(), key=lambda x: -x[1])[:5]:
            print(f"    {size} statements: {count} transactions")
        
        print(f"\n  Replay transaction size distribution (top 5):")
        for size, count in sorted(replay_dist.items(), key=lambda x: -x[1])[:5]:
            print(f"    {size} statements: {count} transactions")
        
        # 计算分布相似度
        common_sizes = set(original_dist.keys()) & set(replay_dist.keys())
        total_original = sum(original_dist.values())
        total_replay = sum(replay_dist.values())
        
        similarity_score = 0
        for size in common_sizes:
            orig_ratio = original_dist[size] / total_original
            replay_ratio = replay_dist[size] / total_replay
            similarity_score += min(orig_ratio, replay_ratio)
        
        tx_similarity = similarity_score * 100
        print(f"\n  Transaction size distribution similarity: {tx_similarity:.2f}%")
        
        # ========== 验证3: 检查多语句事务的保持率 ==========
        print(f"\n--- Verifying Multi-Statement Transaction Preservation ---")
        
        original_multi_tx = sum(1 for v in original_vxid_groups.values() if len(v) > 1)
        replay_multi_tx = sum(1 for v in replay_vxid_groups.values() if len(v) > 1)
        
        # 计算平均事务大小
        original_avg_size = sum(len(v) for v in original_vxid_groups.values()) / len(original_vxid_groups) if original_vxid_groups else 0
        replay_avg_size = sum(len(v) for v in replay_vxid_groups.values()) / len(replay_vxid_groups) if replay_vxid_groups else 0
        
        print(f"  Original: {original_multi_tx} multi-statement transactions (avg size: {original_avg_size:.2f})")
        print(f"  Replay: {replay_multi_tx} multi-statement transactions (avg size: {replay_avg_size:.2f})")
        
        # 多语句事务保持率
        multi_tx_preservation = replay_multi_tx / original_multi_tx if original_multi_tx > 0 else 1.0
        print(f"  Multi-statement TX preservation: {multi_tx_preservation:.2%}")
        
        # ========== 生成验证报告 ==========
        stmt_ratio = len(replay_stmts) / len(original_stmts) if original_stmts else 0
        tx_ratio = len(replay_vxid_groups) / len(original_vxid_groups) if original_vxid_groups else 0
        
        # 评估结果
        # 使用多种指标综合评估
        scores = []
        scores.append(100 if session_match else 50)  # Session 一致性
        scores.append(tx_similarity)  # 事务大小分布相似度
        scores.append(min(100, multi_tx_preservation * 100))  # 多语句事务保持率
        scores.append(min(100, stmt_ratio * 100))  # 语句执行率
        
        overall_score = sum(scores) / len(scores)
        
        if overall_score >= 80:
            consistency_level = "HIGH"
            message = "Replay maintains good session and transaction consistency"
        elif overall_score >= 50:
            consistency_level = "MEDIUM"
            message = "Replay has moderate consistency, some transactions may be fragmented"
        else:
            consistency_level = "LOW"
            message = "Replay shows significant differences from original execution"
        
        verification_result = {
            'verified': True,
            'consistency_level': consistency_level,
            'message': message,
            'overall_score': round(overall_score, 2),
            'original_statements': len(original_stmts),
            'replay_statements': len(replay_stmts),
            'statement_ratio': round(stmt_ratio, 4),
            'original_sessions': len(original_sessions),
            'replay_sessions': len(replay_sessions),
            'session_match': session_match,
            'original_transactions': len(original_vxid_groups),
            'replay_transactions': len(replay_vxid_groups),
            'transaction_ratio': round(tx_ratio, 4),
            'transaction_distribution_similarity': round(tx_similarity, 2),
            'original_multi_tx': original_multi_tx,
            'replay_multi_tx': replay_multi_tx,
            'multi_tx_preservation': round(multi_tx_preservation, 4),
            'original_avg_tx_size': round(original_avg_size, 2),
            'replay_avg_tx_size': round(replay_avg_size, 2),
        }
        
        print(f"\n" + "="*60)
        print(f"VERIFICATION RESULT: {consistency_level} (Score: {overall_score:.1f}/100)")
        print(f"Message: {message}")
        print(f"  - Session match: {'✓' if session_match else '✗'}")
        print(f"  - Statement execution rate: {stmt_ratio:.2%}")
        print(f"  - Transaction distribution similarity: {tx_similarity:.2f}%")
        print(f"  - Multi-statement TX preservation: {multi_tx_preservation:.2%}")
        print("="*60)
        
        return verification_result
    
    # ==================== API 调用 ====================
    
    def start_replay(self, task_id, speed_factor=1.0):
        """启动回放"""
        print(f"Starting replay for task {task_id} with speed factor {speed_factor}...")
        
        try:
            resp = requests.post(
                f"{self.api_base}/api/v1/replay/run",
                params={
                    "task_id": task_id,
                    "speed_factor": speed_factor
                },
                timeout=30
            )
            
            if resp.status_code == 200:
                result = resp.json()
                if result.get('code') == 0:
                    print(f"Replay started successfully")
                    return True
                else:
                    print(f"Replay start failed: {result.get('msg')}")
                    return False
            else:
                print(f"API error: {resp.status_code} - {resp.text}")
                return False
                
        except Exception as e:
            print(f"Error starting replay: {e}")
            return False
    
    def prepare_task(self, log_file):
        """准备回放任务"""
        db_conf = self.config['database']
        
        print(f"Preparing replay task from {log_file}...")
        
        try:
            with open(log_file, 'rb') as f:
                resp = requests.post(
                    f"{self.api_base}/api/v1/replay/prepare",
                    data={
                        "db_host": db_conf['host'],
                        "db_port": db_conf['port'],
                        "db_user": db_conf['user'],
                        "db_pass": db_conf['password'],
                        "db_name": db_conf['db_name']
                    },
                    files={"log_file": (os.path.basename(log_file), f)},
                    timeout=300
                )
            
            if resp.status_code == 200:
                result = resp.json()
                if result.get('code') == 0:
                    data = result.get('data', {})
                    print(f"Task prepared successfully:")
                    print(f"  Task ID: {data.get('task_id')}")
                    print(f"  Statements: {data.get('total_statements')}")
                    print(f"  Transactions: {data.get('total_transactions')}")
                    return data.get('task_id')
                else:
                    print(f"Prepare failed: {result.get('msg')}")
                    return None
            else:
                print(f"API error: {resp.status_code} - {resp.text}")
                return None
                
        except Exception as e:
            print(f"Error preparing task: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def get_replay_progress(self, task_id):
        """获取回放进度"""
        try:
            resp = requests.get(
                f"{self.api_base}/api/v1/replay/progress",
                params={"task_id": task_id},
                timeout=10
            )
            
            if resp.status_code == 200:
                result = resp.json()
                if result.get('code') == 0:
                    return result.get('data', {})
            return None
        except Exception as e:
            print(f"Error getting progress: {e}")
            return None
    
    def get_replay_report(self, task_id):
        """获取回放报告"""
        try:
            resp = requests.get(
                f"{self.api_base}/api/v1/replay/report",
                params={"task_id": task_id},
                timeout=10
            )
            
            if resp.status_code == 200:
                result = resp.json()
                if result.get('code') == 0:
                    return result.get('data', {})
            return None
        except Exception as e:
            print(f"Error getting report: {e}")
            return None
    
    def wait_for_replay_complete(self, task_id, timeout=600):
        """等待回放完成"""
        print("Waiting for replay to complete...")
        start_time = time.time()
        last_executed = 0
        stall_count = 0
        
        while time.time() - start_time < timeout:
            progress = self.get_replay_progress(task_id)
            
            if progress:
                running = progress.get('running', False)
                percentage = progress.get('percentage', 0)
                executed = progress.get('executed_statements', 0)
                total = progress.get('total_statements', 0)
                success = progress.get('success_count', 0)
                failure = progress.get('failure_count', 0)
                
                print(f"Progress: {percentage:.1f}% ({executed}/{total}), Success: {success}, Failed: {failure}, Running: {running}")
                
                if executed == last_executed and executed > 0:
                    stall_count += 1
                    if stall_count > 5:
                        print("Replay appears to be stalled, checking report...")
                        report = self.get_replay_report(task_id)
                        if report:
                            print(f"Replay finished: {report.get('executed_stmts')} executed, {report.get('success_stmts')} success, {report.get('failed_stmts')} failed")
                            return True
                else:
                    stall_count = 0
                
                last_executed = executed
                
                if not running and executed > 0:
                    print("Replay completed!")
                    return True
            
            time.sleep(2)
        
        print("Replay timeout!")
        return False
    
    # ==================== 数据保存和加载 ====================
    
    def load_original_data(self, data_file):
        """从文件加载原始测试数据"""
        if os.path.exists(data_file):
            print(f"Loading original data from {data_file}...")
            with open(data_file, 'r') as f:
                data = json.load(f)
                self.original_stats = data
                print(f"Loaded {len(data.get('time', []))} data points")
                return True
        return False
    
    def save_stats(self, stats_dict, filename):
        """保存统计数据到文件"""
        filepath = self.get_output_path(filename)
        with open(filepath, 'w') as f:
            json.dump(stats_dict, f, indent=2)
        print(f"Stats saved to {filepath}")
        return filepath
    
    # ==================== 测试执行 ====================
    
    def run_sysbench(self):
        """运行 sysbench 并返回开始和结束时间"""
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
        
        self.test_start_time = datetime.now() - timedelta(seconds=1)
        
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"Sysbench execution failed: {e}")
        
        self.test_end_time = datetime.now() + timedelta(seconds=1)
        
        print(f"\nTest time range: {self.test_start_time} to {self.test_end_time}")
    
    def run_original_benchmark(self):
        """运行原始基准测试（使用 sysbench）"""
        print("\n" + "="*60)
        print("Running Original Benchmark (Sysbench)")
        print("="*60 + "\n")
        
        main_process = self.get_pg_process()
        if not main_process:
            print("PostgreSQL main process not found, exiting.")
            return False
        
        # 重置统计
        self.original_stats = {
            'time': [],
            'cpu': [],
            'memory': []
        }
        self.last_cpu_times = {}
        
        # 启动监控线程
        self.running = True
        monitor_thread = threading.Thread(
            target=self.monitor_resources, 
            args=(self.original_stats,)
        )
        monitor_thread.start()
        
        # 运行 sysbench
        self.run_sysbench()
        
        # 停止监控
        self.running = False
        monitor_thread.join()
        
        # 保存原始数据
        original_stats_file = self.save_stats(self.original_stats, "original_stats.json")
        self.test_result['files']['original_stats'] = original_stats_file
        
        # 保存时间范围信息
        time_info = {
            'start_time': self.test_start_time.isoformat(),
            'end_time': self.test_end_time.isoformat(),
            'username': self.config['database']['user'],
            'duration_seconds': (self.test_end_time - self.test_start_time).total_seconds()
        }
        time_info_file = self.get_output_path("test_time.json")
        with open(time_info_file, 'w') as f:
            json.dump(time_info, f, indent=2)
        self.test_result['files']['test_time'] = time_info_file
        
        # 更新测试结果
        if self.original_stats.get('time'):
            self.test_result['original'] = {
                'duration': max(self.original_stats['time']),
                'cpu_max': max(self.original_stats['cpu']),
                'cpu_avg': sum(self.original_stats['cpu']) / len(self.original_stats['cpu']),
                'memory_max': max(self.original_stats['memory']),
                'memory_avg': sum(self.original_stats['memory']) / len(self.original_stats['memory'])
            }
        
        return True
    
    def run_replay_benchmark(self, task_id, speed_factor=1.0):
        """运行回放基准测试"""
        print(f"\n" + "="*60)
        print(f"Running Replay Benchmark (Task: {task_id})")
        print("="*60 + "\n")
        
        self.test_result['task_id'] = task_id
        
        main_process = self.get_pg_process()
        if not main_process:
            print("PostgreSQL main process not found, exiting.")
            return False
        
        # 重置回放统计
        self.replay_stats = {
            'time': [],
            'cpu': [],
            'memory': []
        }
        self.last_cpu_times = {}
        
        # 启动监控线程
        self.running = True
        monitor_thread = threading.Thread(
            target=self.monitor_resources, 
            args=(self.replay_stats,)
        )
        monitor_thread.start()
        
        # 记录回放开始时间
        self.replay_start_time = datetime.now() - timedelta(seconds=1)
        
        # 启动回放
        if not self.start_replay(task_id, speed_factor):
            self.running = False
            monitor_thread.join()
            return False
        
        # 等待回放完成
        self.wait_for_replay_complete(task_id)
        
        # 记录回放结束时间
        self.replay_end_time = datetime.now() + timedelta(seconds=1)
        
        # 停止监控
        self.running = False
        monitor_thread.join()
        
        # 打印回放报告
        report = self.get_replay_report(task_id)
        if report:
            print(f"\n=== Replay Report ===")
            print(f"Total: {report.get('total_statements')}")
            print(f"Executed: {report.get('executed_stmts')}")
            print(f"Success: {report.get('success_stmts')}")
            print(f"Failed: {report.get('failed_stmts')}")
            print(f"Success Rate: {report.get('success_rate', 0):.2f}%")
            print(f"Duration: {report.get('duration')}")
            
            # 显示差异统计
            divergence_count = report.get('divergence_count', 0)
            rows_diff = report.get('rows_affected_diff', 0)
            error_diff = report.get('error_state_diff', 0)
            divergence_rate = report.get('divergence_rate', 0)
            
            print(f"\n=== Divergence Statistics ===")
            print(f"Total Divergences: {divergence_count}")
            print(f"  - Rows Affected Diff: {rows_diff}")
            print(f"  - Error State Diff: {error_diff}")
            print(f"Divergence Rate: {divergence_rate:.4f}%")
            
            # 更新测试结果
            self.test_result['divergence'] = {
                'total': divergence_count,
                'rows_affected_diff': rows_diff,
                'error_state_diff': error_diff,
                'divergence_rate': divergence_rate
            }
            
            if report.get('divergences'):
                print(f"\nFirst 3 divergences:")
                for div in report.get('divergences', [])[:3]:
                    div_type = div.get('divergence_type', 'unknown')
                    if div_type == 'rows_affected':
                        print(f"  - [ROWS] Original: {div.get('original_rows_affected')}, Replay: {div.get('replay_rows_affected')}")
                    elif div_type == 'error_state':
                        print(f"  - [ERROR] Original: {div.get('original_state')}, Replay: {div.get('replay_state')}")
                    elif div_type == 'error_code':
                        print(f"  - [CODE] Original: {div.get('original_state')}, Replay: {div.get('replay_state')}")
                    sql = div.get('sql', '')[:60]
                    print(f"    SQL: {sql}...")
            
            if report.get('errors'):
                print(f"\nFirst 3 errors:")
                for err in report.get('errors', [])[:3]:
                    print(f"  - {err.get('error')[:100]}...")
            
            # 保存完整报告
            report_file = self.get_output_path("replay_report.json")
            with open(report_file, 'w') as f:
                json.dump(report, f, indent=2)
            self.test_result['files']['replay_report'] = report_file
        
        # 保存回放数据
        replay_stats_file = self.save_stats(self.replay_stats, "replay_stats.json")
        self.test_result['files']['replay_stats'] = replay_stats_file
        
        # 更新测试结果
        if self.replay_stats.get('time'):
            self.test_result['replay'] = {
                'duration': max(self.replay_stats['time']),
                'cpu_max': max(self.replay_stats['cpu']),
                'cpu_avg': sum(self.replay_stats['cpu']) / len(self.replay_stats['cpu']),
                'memory_max': max(self.replay_stats['memory']),
                'memory_avg': sum(self.replay_stats['memory']) / len(self.replay_stats['memory'])
            }
        
        return True
    
    # ==================== 绘图和报告 ====================
    
    def plot_comparison(self):
        """绘制对比图"""
        if not self.original_stats.get('time') and not self.replay_stats.get('time'):
            print("No data to plot.")
            return
        
        cpu_count = psutil.cpu_count()
        
        plt.style.use('seaborn-v0_8-whitegrid')
        fig, axes = plt.subplots(3, 1, figsize=(14, 15))
        
        # 添加标题（包含描述）
        title = 'Database Replay Comparison Test'
        if self.description:
            title += f'\n{self.description}'
        fig.suptitle(title, fontsize=14, fontweight='bold')
        
        # CPU 对比图
        ax1 = axes[0]
        
        has_original = bool(self.original_stats.get('time'))
        has_replay = bool(self.replay_stats.get('time'))
        
        if has_original:
            ax1.plot(self.original_stats['time'], self.original_stats['cpu'], 
                    'b-', linewidth=1.2, label='Original (Sysbench)', alpha=0.8)
            ax1.fill_between(self.original_stats['time'], self.original_stats['cpu'], 
                           alpha=0.2, color='blue')
        
        if has_replay:
            ax1.plot(self.replay_stats['time'], self.replay_stats['cpu'], 
                    'r-', linewidth=1.2, label='Replay', alpha=0.8)
            ax1.fill_between(self.replay_stats['time'], self.replay_stats['cpu'], 
                           alpha=0.2, color='red')
        
        ax1.set_title('CPU Usage Comparison', fontsize=12)
        ax1.set_ylabel(f'CPU % (max theoretical={cpu_count*100}%)')
        ax1.legend(loc='upper right')
        ax1.grid(True, alpha=0.3)
        
        # 内存对比图
        ax2 = axes[1]
        
        if has_original:
            ax2.plot(self.original_stats['time'], self.original_stats['memory'], 
                    'b-', linewidth=1.2, label='Original (Sysbench)', alpha=0.8)
            ax2.fill_between(self.original_stats['time'], self.original_stats['memory'], 
                           alpha=0.2, color='blue')
        
        if has_replay:
            ax2.plot(self.replay_stats['time'], self.replay_stats['memory'], 
                    'r-', linewidth=1.2, label='Replay', alpha=0.8)
            ax2.fill_between(self.replay_stats['time'], self.replay_stats['memory'], 
                           alpha=0.2, color='red')
        
        ax2.set_title('Memory Usage Comparison', fontsize=12)
        ax2.set_xlabel('Time (s)')
        ax2.set_ylabel('Memory (MB)')
        ax2.legend(loc='upper right')
        ax2.grid(True, alpha=0.3)
        
        # QPS Comparison Graph
        ax3 = axes[2]
        
        def get_qps_data(log_file):
            if not log_file or not os.path.exists(log_file):
                return None, None
            
            # 使用已有的 parse_audit_log_file 方法
            entries = self.parse_audit_log_file(log_file)
            if not entries:
                return None, None
                
            # 过滤有效时间戳
            valid_entries = [e for e in entries if e.get('timestamp')]
            if not valid_entries:
                return None, None
                
            # 按时间排序
            valid_entries.sort(key=lambda x: x['timestamp'])
            
            start_time = valid_entries[0]['timestamp']
            end_time = valid_entries[-1]['timestamp']
            
            # 按秒聚合
            data = {} # second -> count
            
            for e in valid_entries:
                delta = (e['timestamp'] - start_time).total_seconds()
                if delta < 0: delta = 0
                sec = int(delta)
                
                data[sec] = data.get(sec, 0) + 1
            
            if not data:
                return None, None
                
            secs = sorted(data.keys())
            max_sec = secs[-1] if secs else 0
            # 填充每一秒
            time_axis = list(range(max_sec + 1))
            qps = [data.get(s, 0) for s in time_axis]
            
            return time_axis, qps

        # 绘制 QPS 对比
        orig_log = self.test_result.get('files', {}).get('audit_log')
        replay_log = self.test_result.get('files', {}).get('replay_audit_log')
        
        has_orig_qps = False
        if orig_log:
            t_orig, qps_orig = get_qps_data(orig_log)
            if t_orig:
                # 计算滑动平均以更清晰展示趋势
                window_size = 5
                if len(qps_orig) > window_size:
                    qps_smooth = []
                    for i in range(len(qps_orig)):
                        start_idx = max(0, i - window_size // 2)
                        end_idx = min(len(qps_orig), i + window_size // 2 + 1)
                        qps_smooth.append(sum(qps_orig[start_idx:end_idx]) / (end_idx - start_idx))
                    ax3.plot(t_orig, qps_smooth, 'b-', linewidth=1.5, label='Original QPS (Smoothed)', alpha=0.9)
                    ax3.plot(t_orig, qps_orig, 'b.', markersize=2, alpha=0.2) # 原始点
                else:
                    ax3.plot(t_orig, qps_orig, 'b-', linewidth=1.5, label='Original QPS', alpha=0.9)
                
                avg_orig_qps = sum(qps_orig) / len(qps_orig) if qps_orig else 0
                ax3.axhline(y=avg_orig_qps, color='b', linestyle=':', alpha=0.5, label=f'Avg Original: {avg_orig_qps:.1f}')
                has_orig_qps = True

        if replay_log:
            t_replay, qps_replay = get_qps_data(replay_log)
            if t_replay:
                # 计算滑动平均
                window_size = 5
                if len(qps_replay) > window_size:
                    qps_smooth = []
                    for i in range(len(qps_replay)):
                        start_idx = max(0, i - window_size // 2)
                        end_idx = min(len(qps_replay), i + window_size // 2 + 1)
                        qps_smooth.append(sum(qps_replay[start_idx:end_idx]) / (end_idx - start_idx))
                    ax3.plot(t_replay, qps_smooth, 'r-', linewidth=1.5, label='Replay QPS (Smoothed)', alpha=0.9)
                    ax3.plot(t_replay, qps_replay, 'r.', markersize=2, alpha=0.2) # 原始点
                else:
                    ax3.plot(t_replay, qps_replay, 'r-', linewidth=1.5, label='Replay QPS', alpha=0.9)
                    
                avg_replay_qps = sum(qps_replay) / len(qps_replay) if qps_replay else 0
                ax3.axhline(y=avg_replay_qps, color='r', linestyle=':', alpha=0.5, label=f'Avg Replay: {avg_replay_qps:.1f}')
                
        ax3.set_title('QPS Comparison (Queries Per Second)', fontsize=12)
        ax3.set_xlabel('Time (s)')
        ax3.set_ylabel('QPS')
        ax3.legend(loc='upper right')
        ax3.grid(True, alpha=0.3)
        
        plt.tight_layout()
        
        output_file = self.get_output_path("comparison.png")
        plt.savefig(output_file, dpi=150)
        self.test_result['files']['comparison_chart'] = output_file
        print(f"Comparison graph saved to {output_file}")
        
        # 打印统计信息
        self.print_statistics()
    
    def print_statistics(self):
        """打印统计信息"""
        print("\n" + "="*60)
        print("STATISTICS COMPARISON")
        print("="*60)
        
        if self.original_stats.get('time'):
            print("\n[Original (Sysbench)]")
            print(f"  Duration: {max(self.original_stats['time']):.2f}s")
            print(f"  CPU: min={min(self.original_stats['cpu']):.2f}%, max={max(self.original_stats['cpu']):.2f}%, avg={sum(self.original_stats['cpu'])/len(self.original_stats['cpu']):.2f}%")
            print(f"  Memory: min={min(self.original_stats['memory']):.2f}MB, max={max(self.original_stats['memory']):.2f}MB")
        
        if self.replay_stats.get('time'):
            print("\n[Replay]")
            print(f"  Duration: {max(self.replay_stats['time']):.2f}s")
            print(f"  CPU: min={min(self.replay_stats['cpu']):.2f}%, max={max(self.replay_stats['cpu']):.2f}%, avg={sum(self.replay_stats['cpu'])/len(self.replay_stats['cpu']):.2f}%")
            print(f"  Memory: min={min(self.replay_stats['memory']):.2f}MB, max={max(self.replay_stats['memory']):.2f}MB")
        
        print("="*60)
    
    def save_test_summary(self):
        """保存测试摘要"""
        summary_file = self.get_output_path("summary.json")
        
        # 添加配置信息
        self.test_result['config'] = {
            'database': {
                'host': self.config['database']['host'],
                'port': self.config['database']['port'],
                'user': self.config['database']['user'],
                'db_name': self.config['database']['db_name']
            },
            'sysbench': self.config['sysbench']
        }
        
        with open(summary_file, 'w') as f:
            json.dump(self.test_result, f, indent=2)
        
        print(f"\nTest summary saved to {summary_file}")
        
        # 同时生成一个 README
        readme_file = self.get_output_path("README.md")
        with open(readme_file, 'w') as f:
            f.write(f"# Test Results - {self.timestamp}\n\n")
            
            if self.description:
                f.write(f"## Description\n\n{self.description}\n\n")
            
            f.write(f"## Test Time\n\n")
            f.write(f"- Timestamp: {self.timestamp}\n")
            if self.test_start_time:
                f.write(f"- Start: {self.test_start_time}\n")
            if self.test_end_time:
                f.write(f"- End: {self.test_end_time}\n")
            f.write("\n")
            
            if self.test_result.get('original'):
                f.write("## Original Benchmark Results\n\n")
                orig = self.test_result['original']
                f.write(f"- Duration: {orig.get('duration', 0):.2f}s\n")
                f.write(f"- CPU Max: {orig.get('cpu_max', 0):.2f}%\n")
                f.write(f"- CPU Avg: {orig.get('cpu_avg', 0):.2f}%\n")
                f.write(f"- Memory Max: {orig.get('memory_max', 0):.2f}MB\n\n")
            
            if self.test_result.get('replay'):
                f.write("## Replay Results\n\n")
                replay = self.test_result['replay']
                f.write(f"- Duration: {replay.get('duration', 0):.2f}s\n")
                f.write(f"- CPU Max: {replay.get('cpu_max', 0):.2f}%\n")
                f.write(f"- CPU Avg: {replay.get('cpu_avg', 0):.2f}%\n")
                f.write(f"- Memory Max: {replay.get('memory_max', 0):.2f}MB\n\n")
            
            if self.test_result.get('divergence'):
                f.write("## Divergence Statistics\n\n")
                div = self.test_result['divergence']
                f.write(f"- Total Divergences: {div.get('total', 0)}\n")
                f.write(f"- Rows Affected Diff: {div.get('rows_affected_diff', 0)}\n")
                f.write(f"- Error State Diff: {div.get('error_state_diff', 0)}\n")
                f.write(f"- Divergence Rate: {div.get('divergence_rate', 0):.4f}%\n\n")
            
            if self.test_result.get('verification'):
                f.write("## Consistency Verification\n\n")
                ver = self.test_result['verification']
                f.write(f"- **Consistency Level: {ver.get('consistency_level', 'N/A')}**\n")
                f.write(f"- Message: {ver.get('message', 'N/A')}\n\n")
                f.write("### Statistics\n\n")
                f.write(f"| Metric | Original | Replay |\n")
                f.write(f"|--------|----------|--------|\n")
                f.write(f"| Statements | {ver.get('original_statements', 0)} | {ver.get('replay_statements', 0)} |\n")
                f.write(f"| Sessions | {ver.get('original_sessions', 0)} | {ver.get('replay_sessions', 0)} |\n")
                f.write(f"| Transactions | {ver.get('original_transactions', 0)} | {ver.get('replay_transactions', 0)} |\n")
                f.write(f"| Single-stmt TX | {ver.get('original_single_tx', 0)} | {ver.get('replay_single_tx', 0)} |\n")
                f.write(f"| Multi-stmt TX | {ver.get('original_multi_tx', 0)} | {ver.get('replay_multi_tx', 0)} |\n\n")
                f.write(f"- Transaction distribution similarity: **{ver.get('transaction_distribution_similarity', 0):.2f}%**\n")
                f.write(f"- Statement ratio (replay/original): {ver.get('statement_ratio', 0):.4f}\n")
                f.write(f"- Transaction ratio (replay/original): {ver.get('transaction_ratio', 0):.4f}\n\n")
            
            f.write("## Files\n\n")
            for name, path in self.test_result.get('files', {}).items():
                f.write(f"- {name}: `{os.path.basename(path)}`\n")
        
        print(f"README saved to {readme_file}")
        
        return summary_file
    
    # ==================== 完整测试流程 ====================
    
    def run_full_test(self, speed_factor=1.0, with_restore=True):
        """
        运行完整的对比测试流程
        
        Args:
            speed_factor: 回放速度因子
            with_restore: 是否在回放前恢复数据库
        """
        print("\n" + "="*60)
        print("FULL COMPARISON TEST")
        if self.description:
            print(f"Description: {self.description}")
        print(f"Database restore before replay: {'Yes' if with_restore else 'No'}")
        print("="*60)
        
        # 初始化输出目录
        self.init_output_dir()
        
        # 1. 备份数据库（在运行原始测试前）
        backup_file = None
        if with_restore:
            backup_file = self.backup_sysbench_tables()
            if not backup_file:
                print("Warning: Database backup failed, continuing without restore capability")
                with_restore = False
        
        # 2. 运行原始 sysbench 测试
        if not self.run_original_benchmark():
            print("Original benchmark failed")
            self.save_test_summary()
            return
        
        # 3. 从审计日志中提取这段时间的 SQL
        db_user = self.config['database']['user']
        audit_log_file = self.get_output_path("audit_log.log")
        
        count = self.extract_audit_logs_by_time(
            self.test_start_time,
            self.test_end_time,
            db_user,
            audit_log_file
        )
        
        self.test_result['files']['audit_log'] = audit_log_file
        
        if count == 0:
            print("No audit logs found in time range. Make sure pgaudit is enabled.")
            self.plot_comparison()
            self.save_test_summary()
            return
        
        # 4. 准备回放任务
        task_id = self.prepare_task(audit_log_file)
        if not task_id:
            print("Failed to prepare replay task")
            self.plot_comparison()
            self.save_test_summary()
            return
        
        self.test_result['task_id'] = task_id
        
        # 5. 恢复数据库到备份状态（如果启用）
        if with_restore and backup_file:
            print("\n" + "="*60)
            print("RESTORING DATABASE BEFORE REPLAY")
            print("="*60)
            if not self.restore_database(backup_file):
                print("Warning: Database restore failed, replay results may have errors")
        
        # 6. 等待一段时间让数据库稳定
        print("\nWaiting 5 seconds before starting replay...")
        time.sleep(5)
        
        # 7. 运行回放测试
        self.run_replay_benchmark(task_id, speed_factor)
        
        # 8. 提取回放期间的审计日志并验证一致性
        if hasattr(self, 'replay_start_time') and hasattr(self, 'replay_end_time'):
            replay_audit_file = self.get_output_path("replay_audit_log.log")
            
            print(f"\nExtracting replay audit logs...")
            replay_log_count = self.extract_audit_logs_by_time(
                self.replay_start_time,
                self.replay_end_time,
                db_user,
                replay_audit_file
            )
            
            if replay_log_count > 0:
                self.test_result['files']['replay_audit_log'] = replay_audit_file
                
                # 验证回放一致性
                verification = self.verify_replay_consistency(audit_log_file, replay_audit_file)
                self.test_result['verification'] = verification
                
                # 保存验证结果
                verification_file = self.get_output_path("verification_result.json")
                with open(verification_file, 'w') as f:
                    json.dump(verification, f, indent=2)
                self.test_result['files']['verification'] = verification_file
            else:
                print("Warning: No replay audit logs found, skipping consistency verification")
        
        # 9. 绘制对比图
        self.plot_comparison()
        
        # 10. 保存测试摘要
        self.save_test_summary()
        
        print(f"\n{'='*60}")
        print(f"All test files saved to: {self.output_dir}")
        print(f"{'='*60}")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Database Replay Comparison Tool')
    parser.add_argument('--config', default='tools/config.yaml', help='Config file path')
    parser.add_argument('--task-id', help='Task ID for replay (skip original test)')
    parser.add_argument('--speed', type=float, default=0, help='Replay speed factor (default: 0 = fast mode)')
    parser.add_argument('--original-data', help='Path to original stats JSON file')
    parser.add_argument('--mode', choices=['original', 'replay', 'full', 'compare'], default='full',
                       help='Test mode: original (run sysbench only), replay (run replay only), full (complete test), compare (just plot)')
    parser.add_argument('--pg-log', help='PostgreSQL log file path')
    parser.add_argument('--output-dir', help='Output directory for test results')
    parser.add_argument('--description', '-d', default='', help='Test description (what changes were made)')
    parser.add_argument('--no-restore', action='store_true', help='Skip database restore before replay')
    parser.add_argument('--backup-file', help='Use existing backup file for restore')
    
    args = parser.parse_args()
    
    runner = ReplayCompareRunner(
        config_path=args.config,
        output_dir=args.output_dir,
        description=args.description
    )
    
    # 覆盖 PostgreSQL 日志路径
    if args.pg_log:
        runner.pg_log_path = args.pg_log
    
    # 加载原始数据（如果提供）
    if args.original_data:
        runner.load_original_data(args.original_data)
    
    # 初始化输出目录（如果不是 compare 模式）
    if args.mode != 'compare':
        runner.init_output_dir()
    
    if args.mode == 'original':
        # 只运行原始测试
        runner.run_original_benchmark()
        runner.plot_comparison()
        runner.save_test_summary()
        
    elif args.mode == 'replay':
        # 只运行回放测试
        if not args.task_id:
            print("Error: --task-id is required for replay mode")
            sys.exit(1)
        
        # 如果提供了备份文件，先恢复
        if args.backup_file and not args.no_restore:
            runner.restore_database(args.backup_file)
            time.sleep(3)
        
        runner.run_replay_benchmark(args.task_id, args.speed)
        runner.plot_comparison()
        runner.save_test_summary()
        
    elif args.mode == 'full':
        # 完整测试流程
        runner.run_full_test(speed_factor=args.speed, with_restore=not args.no_restore)
        
    elif args.mode == 'compare':
        # 只绘图（需要提供数据文件）
        import glob
        
        runner.init_output_dir()
        
        if not runner.original_stats.get('time'):
            original_files = sorted(glob.glob('tools/test_results/*/original_stats.json'), reverse=True)
            if original_files:
                runner.load_original_data(original_files[0])
        
        replay_files = sorted(glob.glob('tools/test_results/*/replay_stats.json'), reverse=True)
        if replay_files:
            with open(replay_files[0], 'r') as f:
                runner.replay_stats = json.load(f)
            print(f"Loaded replay data from {replay_files[0]}")
        
        runner.plot_comparison()
        runner.save_test_summary()


if __name__ == "__main__":
    main()
