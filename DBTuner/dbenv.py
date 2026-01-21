import os
import pdb
from sre_constants import SUCCESS
import time
import glob
import json
import subprocess
import numpy as np
from multiprocessing import Manager
from multiprocessing.connection import Client
import sys
from DBTuner.knobs import logger
from DBTuner.parser import TIMEOUT, is_number, parse_sysbench, parse_oltpbench, parse_job
from DBTuner.knobs import initialize_knobs, get_default_knobs
import psutil
import multiprocessing as mp
from .resource_monitor import ResourceMonitor 
from DBTuner.workload import SYSBENCH_WORKLOAD, JOB_WORKLOAD, OLTPBENCH_WORKLOADS, TPCH_WORKLOAD,TPCC_WORKLOAD
# from autotune.utils.constants import MAXINT, SUCCESS, FAILED, TIMEOUT
# from autotune.utils.parser import is_number
from DBTuner.database.postgresqldb import PostgresqlDB
# from DBTuner.database.postgresqldb import PostgresqlDB
from DBTuner.database.mysqldb import MysqlDB

import re
from collections import defaultdict
import pandas as pd



class DBEnv:
    def __init__(self, args, args_tune, db):
        self.db = db
        self.args = args
        self.args_tune = args_tune
        self.workload = self.get_workload()
        self.log_path = "./log"
        self.num_metrics = self.db.num_metrics
        self.threads = int(args['thread_num'])
        self.best_result = './autotune_best.res'
        self.knobs_detail = initialize_knobs(args['knob_config_file'], int(args['knob_num']))
        self.default_knobs = get_default_knobs()
        self.online_mode = eval(args['online_mode'])
        self.remote_mode = eval(args['remote_mode'])
        self.oltpbench_config_xml = args['oltpbench_config_xml']
        self.step_count = 0
        self.connect_sucess = True
        self.reinit_interval = 0
        self.reinit = True
        if self.reinit_interval:
            self.reinit = True
        self.generate_time()
        self.y_variable = eval(args_tune['performance_metric'])
        self.reference_point = self.generate_reference_point(eval(args_tune['reference_point']))

        if args_tune['constraints'] is None or args_tune['constraints'] == '':
            self.constraints = []
        else:
            self.constraints = eval(args_tune['constraints'])
        self.lhs_log = args['lhs_log']
        self.cpu_core = args['cpu_core']
        self.info =  {
            'objs': self.y_variable,
            'constraints': self.constraints
        }

    def generate_reference_point(self, user_defined_reference_point):
        if len(self.y_variable) <= 1:
            return None

        reference_point_dir = {
            'tps': 0,
            'lat': BENCHMARK_RUNNING_TIME,
            'qps': 0,
            'cpu': 0,
            'readIO': 0,
            'writeIO': 0,
            'virtualMem': 0,
            'physical': 0,
        }
        reference_point = []
        for key in self.y_variable:
            use_defined_value = user_defined_reference_point[self.y_variable.index(key)]
            if is_number(use_defined_value):
                reference_point.append(use_defined_value)
            else:
                key = key.strip().strip('-')
                reference_point.append(reference_point_dir[key])

        return reference_point

    def get_workload(self):
        if self.args['workload'] == 'sysbench':
            wl = dict(SYSBENCH_WORKLOAD)
            wl['type'] = self.args['workload_type']
        elif self.args['workload'].startswith('oltpbench_'):
            wl = dict(OLTPBENCH_WORKLOADS)
        elif self.args['workload'] == 'job':
            wl = dict(JOB_WORKLOAD)
        elif self.args['workload'] == 'tpch':
            wl = dict(TPCH_WORKLOAD)
        elif self.args['workload'] == 'tpcc':
            wl = dict(TPCC_WORKLOAD)
        else:
            raise ValueError('Invalid workload!')
        return wl

    def generate_time(self):
        global BENCHMARK_RUNNING_TIME
        global BENCHMARK_WARMING_TIME
        global TIMEOUT_TIME
        global RESTART_FREQUENCY

        if self.workload['name'] == 'sysbench' or self.workload['name'] == 'oltpbench':
            try:
                BENCHMARK_RUNNING_TIME = int(self.args['workload_time'])
            except:
                BENCHMARK_RUNNING_TIME = 120
            try:
                BENCHMARK_WARMING_TIME = int(self.args['workload_warmup_time'])
            except:
                BENCHMARK_WARMING_TIME = 30
            TIMEOUT_TIME = BENCHMARK_RUNNING_TIME + BENCHMARK_WARMING_TIME + 30
            RESTART_FREQUENCY = 200

        elif self.workload['name'] == 'job' or self.workload['name'] == 'tpch':
            try:
                BENCHMARK_RUNNING_TIME = int(self.args['workload_time'])
            except:
                BENCHMARK_RUNNING_TIME = 240
            try:
                BENCHMARK_WARMING_TIME = int(self.args['workload_warmup_time'])
            except:
                BENCHMARK_WARMING_TIME = 0
            TIMEOUT_TIME = BENCHMARK_RUNNING_TIME + BENCHMARK_WARMING_TIME
            RESTART_FREQUENCY = 30000
        elif self.workload['name'] == 'tpcc':
            try:
                BENCHMARK_RUNNING_TIME = int(self.args['workload_time'])
            except:
                BENCHMARK_RUNNING_TIME = 120
            try:
                BENCHMARK_WARMING_TIME = int(self.args['workload_warmup_time'])
            except:
                BENCHMARK_WARMING_TIME = 30
            TIMEOUT_TIME = BENCHMARK_RUNNING_TIME + BENCHMARK_WARMING_TIME
            RESTART_FREQUENCY = 200
        else:
            raise ValueError('Invalid workload nmae!')

    def get_external_metrics(self, filename=''):
        if self.workload['name'] == 'sysbench':
            for _ in range(60):
                if os.path.exists(filename):
                    break
                time.sleep(1)
            if not os.path.exists(filename):
                print("benchmark result file does not exist!")
            result = parse_sysbench(filename)
        elif self.workload['name'] == 'oltpbench':
            for _ in range(60):
                if os.path.exists('results/{}.summary'.format(filename)):
                    break
                time.sleep(1)
            if not os.path.exists('results/{}.summary'.format(filename)):
                print("benchmark result file does not exist!")
            result = parse_oltpbench('results/{}.summary'.format(filename))
        elif self.workload['name'] == 'job' or self.workload['name'] == 'tpch':
            for _ in range(60):
                if os.path.exists(filename):
                    break
                time.sleep(1)
            if not os.path.exists(filename):
                print("benchmark result file does not exist!")
            dirname, _ = os.path.split(os.path.abspath(__file__))
            select_file = dirname + '/cli/selectedList_{}.txt'.format(self.workload['name'])
            result = parse_job(filename, select_file, timeout=TIMEOUT_TIME)
            # result = get_total_execution_time(filename, timeout=TIMEOUT_TIME)
        else:
            raise ValueError('Invalid workload name!')
        return result

    def get_benchmark_cmd(self):
        timestamp = int(time.time())
        filename = self.log_path + '/{}.log'.format(timestamp)
        dirname, _ = os.path.split(os.path.abspath(__file__))
        if self.workload['name'] == 'sysbench':
            if(isinstance(self.db, PostgresqlDB)):
                exe_file =  '/cli/run_sysbench_postgresql.sh'
            else:
                exe_file = '/cli/run_sysbench.sh'
            cmd = self.workload['cmd'].format(dirname + exe_file,
                                              self.workload['type'],
                                              self.db.host,
                                              self.db.port,
                                              self.db.user,
                                              self.db.passwd,
                                              100,
                                              6000000,
                                              BENCHMARK_WARMING_TIME,
                                              self.threads,
                                              BENCHMARK_RUNNING_TIME,
                                              filename,
                                              self.db.dbname)
        elif self.workload['name'] == 'oltpbench':
            filename = filename.split('/')[-1].split('.')[0]
            cmd = self.workload['cmd'].format(dirname + '/cli/run_oltpbench.sh',
                                              self.db.dbname,
                                              self.oltpbench_config_xml,
                                              filename)
        elif self.workload['name'] == 'job':
            cmd = self.workload['cmd'].format(dirname + '/cli/run_job_{}.sh'.format(self.db.args['db']),
                                              dirname + '/cli/selectedList_job.txt',
                                              dirname + '/job_query/queries-{}-new'.format(self.db.args['db']),
                                              filename,
                                              self.db.sock,
                                              self.db.passwd)
        elif self.workload['name'] == 'tpch':
            cmd = self.workload['cmd'].format(dirname + '/cli/run_tpch_{}.sh'.format(self.db.args['db']),
                                              dirname + '/cli/selectedList_tpch.txt',
                                              dirname + '/tpch_query/queries-{}-new'.format(self.db.args['db']),
                                              filename,
                                              self.db.sock,
                                              self.db.passwd)
        else:
            raise ValueError('Invalid workload name!')

        logger.info('[DBG]. {}'.format(cmd))
        return cmd, filename

    def get_states(self, collect_resource=0):
        # start Internal Metrics Collection
        internal_metrics = Manager().list()
        im = mp.Process(target=self.db.get_internal_metrics,
                        args=(internal_metrics, BENCHMARK_RUNNING_TIME, BENCHMARK_WARMING_TIME))
        self.db.set_im_alive(True)
        im.start()

        # start Resource Monition (if activated)
        # if collect_resource:
        #     if self.remote_mode:
        #         # start remote Resource Monitor
        #         clientDB_address = (self.db.host, 6001)
        #         clientDB_conn = Client(clientDB_address, authkey=b'DBTuner')
        #         clientDB_conn.send(self.db.pid)
        #     else:
        #         rm = ResourceMonitor(self.db.pid, 1, BENCHMARK_WARMING_TIME, BENCHMARK_RUNNING_TIME)
        #         rm.run()

        # start Benchmark
        benchmark_timeout = False
        cmd, filename = self.get_benchmark_cmd()
        filename = 'sysbench_run.out'
        cmd = "sysbench --mysql-host=localhost --mysql-port=3306 --mysql-user=root --mysql-password=dbiir500 --mysql-db=sbtest --range-size=100 --table-size=80000 --tables=100 --threads=32 --report-interval=1 --warmup-time=10 --time=30 --db-driver=mysql oltp_read_write run > sysbench_run.out"
        print(cmd)
        print("[{}] benchmark start!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        p_benchmark = subprocess.Popen(cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
                                       close_fds=True)
        try:
            outs, errs = p_benchmark.communicate(timeout=TIMEOUT_TIME)
            # print("outs: ", outs)
            # print("errs: ", errs)
            ret_code = p_benchmark.poll()
            if ret_code == 0:
                print("[{}] benchmark finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
            else:
                print("run benchmark get error {}".format(ret_code))
        except subprocess.TimeoutExpired:
            #benchmark_timeout = True
            print("[{}] benchmark timeout!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))

        # terminate Benchmark
        if not self.remote_mode:
            subprocess.Popen(self.db.clear_cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
                             close_fds=True)
            print("[{}] clear processlist".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))

        # stop Internal Metrics Collection
        self.db.set_im_alive(False)
        im.join()

        # stop Resource Monition (if activated)
        collect_resource = False
        if collect_resource:
            if self.remote_mode:
                # send Benchmark-Finish msg to remote Resource Monitor Process
                clientDB_conn.send('benchmark_finished')
                # receive remote Monitor Data
                monitor_data = clientDB_conn.recv()
                cpu, avg_read_io, avg_write_io, avg_virtual_memory, avg_physical_memory = monitor_data
                # close connection
                clientDB_conn.close()

            else:
                rm.terminate()
                cpu, avg_read_io, avg_write_io, avg_virtual_memory, avg_physical_memory = rm.get_monitor_data_avg()
        else:
            cpu, avg_read_io, avg_write_io, avg_virtual_memory, avg_physical_memory = 0, 0, 0, 0, 0

        external_metrics = self.get_external_metrics(filename)
        # internal_metrics, dirty_pages, hit_ratio, page_data = self.db._post_handle(internal_metrics)
        logger.info('internal metrics: {}.'.format(list(internal_metrics)))

        return benchmark_timeout, external_metrics, internal_metrics

    def apply_knobs(self, knobs):
        for key in knobs.keys():
            value = knobs[key]
            if not key in self.knobs_detail.keys() or not self.knobs_detail[key]['type'] == 'integer':
                continue
            if value > self.knobs_detail[key]['max']:
                knobs[key] = self.knobs_detail[key]['max']
                logger.info("{} with value of is larger than max, adjusted".format(key))
            elif value < self.knobs_detail[key]['min']:
                knobs[key] = self.knobs_detail[key]['min']
                logger.info("{} with value of is smaller than min, adjusted".format(key))

        logger.info("[step {}] generate knobs: {}\n".format(self.step_count, knobs))
        if self.online_mode:
            flag = self.db.apply_knobs_online(knobs)
        else:
            flag = self.db.apply_knobs_offline(knobs)

        if not flag:
            if self.reinit:
                logger.info('reinitializing db begin')
                self.db.reinitdb_magic(self.remote_mode)
                logger.info('db reinitialized')

            raise Exception('Apply knobs failed!')


    def step_GP(self, knobs, collect_resource=True):
        #return False, np.random.rand(6), np.random.rand(65), np.random.rand(8)
        # re-init database if activated
        if self.reinit_interval > 0 and self.reinit_interval % RESTART_FREQUENCY == 0:
            if self.reinit:
                logger.info('reinitializing db begin')
                self.db.reinitdb_magic(self.remote_mode)
                logger.info('db reinitialized')
        self.step_count = self.step_count + 1
        self.reinit_interval = self.reinit_interval + 1

        # modify and apply knobs
        for key in knobs.keys():
            value = knobs[key]
            if not key in self.knobs_detail.keys() or not self.knobs_detail[key]['type'] == 'integer':
                continue
            if value > self.knobs_detail[key]['max']:
                knobs[key] = self.knobs_detail[key]['max']
                logger.info("{} with value of is larger than max, adjusted".format(key))
            elif value < self.knobs_detail[key]['min']:
                knobs[key] = self.knobs_detail[key]['min']
                logger.info("{} with value of is smaller than min, adjusted".format(key))
                
        
        logger.info("[step {}] generate knobs: {}\n".format(self.step_count, knobs))

        if self.online_mode:
            flag = self.db.apply_knobs_online(knobs)
        else:
            flag = self.db.apply_knobs_offline(knobs)

        if not flag:
            if self.reinit:
                logger.info('reinitializing db begin')
                self.db.reinitdb_magic(self.remote_mode)
                logger.info('db reinitialized')

            raise Exception('Apply knobs failed!')

        # s = self.get_states(collect_resource=collect_resource)
        s = self.get_states_expe(collect_resource=collect_resource)

        if s is None:
            if self.reinit:
                logger.info('reinitializing db begin')
                self.db.reinitdb_magic(self.remote_mode)
                logger.info('db reinitialized')

            raise Exception('Get states failed!')

        timeout, external_metrics, internal_metrics = s

        format_str = '{}|tps_{}|lat_{}|qps_{}|tpsVar_{}|latVar_{}|qpsVar_{}|{}|65d\n'
        res = format_str.format(knobs, str(external_metrics[0]), str(external_metrics[1]), str(external_metrics[2]),
                                external_metrics[3], external_metrics[4],
                                external_metrics[5], list(internal_metrics))

        return timeout, external_metrics, internal_metrics

    def get_objs(self, res):
        objs = []
        for y_variable in self.y_variable:
            key = y_variable.strip().strip('-')
            value = res[key]
            if not y_variable.strip()[0] == '-':
                value = - value
            objs.append(value)

        return objs

    def get_constraints(self, res):
        if len(self.constraints) == 0:
            return None

        locals().update(res)
        constraintL = []
        for constraint in self.constraints:
            value = eval(constraint)
            constraintL.append(value)

        return constraintL

    def step(self, config):

        knobs = config.get_dictionary().copy()
        for k in self.knobs_detail.keys():
            if k in knobs.keys():
                if self.knobs_detail[k]['type'] == 'integer' and self.knobs_detail[k]['max'] > sys.maxsize:
                    knobs[k] = knobs[k] * 1000
            else:
                knobs[k] = self.knobs_detail[k]['default']

        try:
            timeout, metrics, internal_metrics, resource = self.step_GP(knobs, collect_resource=True)

            if timeout:
                trial_state = TIMEOUT
            else:
                trial_state = SUCCESS

            external_metrics = {
                'tps': metrics[0],
                'lat': metrics[1],
                'qps': metrics[2],
                'tpsVar': metrics[3],
                'latVar': metrics[4],
                'qpsVar': metrics[5],
            }

            resource = {
                'cpu': resource[0],
                'readIO': resource[1],
                'writeIO': resource[2],
                'IO': resource[1] + resource[2],
                'virtualMem': resource[3],
                'physical': resource[4],
                'dirty': resource[5],
                'hit': resource[6],
                'data': resource[7],
            }

            res = dict(external_metrics, **resource)
            objs = self.get_objs(res)
            constraints = self.get_constraints(res)
            return objs, constraints, external_metrics, resource, list(internal_metrics), self.info, trial_state

        except:
            return None, None, {}, {}, [], self.info, FAILED

     # Test 
    
    def get_states_expe_sysbench(self, collect_resource=0):
        # start Internal Metrics Collection
        internal_metrics = Manager().list()
        im = mp.Process(target=self.db.get_internal_metrics,
                        args=(internal_metrics, BENCHMARK_RUNNING_TIME, BENCHMARK_WARMING_TIME))
        self.db.set_im_alive(True)
        im.start()

        # # start Resource Monition (if activated)
        if collect_resource:
            rm = ResourceMonitor(self.db.pid, 1, BENCHMARK_WARMING_TIME, BENCHMARK_RUNNING_TIME)
            rm.run()

        # start Benchmark
        benchmark_timeout = False
        cmd, filename = self.get_benchmark_cmd()
        print(cmd)
        print("sysbench")
        print("**********************************")
        perf_output_dir = f"perf_data_{self.args_tune['task_id']}"   # Define a unified folder for perf data
        os.makedirs(perf_output_dir, exist_ok=True)
        timestamp = int(time.time())
        output_file = 'sysbench_run_{}.out'.format(timestamp)
        print("[{}] benchmark start!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        # TODO 流量回放，获取tps、latency的接口
        p_benchmark = subprocess.Popen(cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
                                    close_fds=True)
        # collect perf data
        perf_file = os.path.join(perf_output_dir, f'perf_data_{timestamp}.data')
        try:
            pgrep_result = subprocess.check_output("pgrep -nx mysqld", shell=True).decode().strip()
        except subprocess.CalledProcessError as e:
            print(f"Error: {e}")
        perf_cmd = f"perf record -F 300 -p {pgrep_result} -g -o {perf_file} -- sleep 50" 
        print(perf_cmd)
        time.sleep(5)
        print("[{}] perf data collection start!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        p_runperf = subprocess.Popen(perf_cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
                                    close_fds=True)
        p_runperf.wait()
        p_benchmark.wait()
        
        print("**********************************")
        outs, errs = p_benchmark.communicate(timeout=TIMEOUT_TIME)
        print(errs)
        print(outs)
        ret_code = p_benchmark.poll()
        if ret_code == 0:
            print("[{}] benchmark finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        else:
            print("run benchmark get error {}".format(ret_code))
        outs, errs = p_runperf.communicate(timeout=TIMEOUT_TIME)
        print("collect perf data: ")
        print(errs)
        print(outs)
        ret_code1 = p_runperf.poll()
        if ret_code1 == 0:
            print("[{}] perf data collection finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        else:
            print("perf data collection get error {}".format(ret_code1))

        # terminate Benchmark
        if not self.remote_mode:
            subprocess.Popen(self.db.clear_cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
                            close_fds=True)
            print("[{}] clear processlist".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))

        # stop Internal Metrics Collection
        self.db.set_im_alive(False)
        im.join()
        
        # transfer perf data to perf txt
        flamegraph_dir = os.path.expanduser("/home/RUC/FlameGraph") 
        stackcollapse_script = os.path.join(flamegraph_dir, "stackcollapse-perf.pl")  # 确保这个脚本存在
        print(stackcollapse_script)
        
        perf_txt = os.path.join(perf_output_dir, f'perf_{timestamp}.txt')
        perf_script_cmd = f"perf script -i {perf_file} | {stackcollapse_script} > {perf_txt}"
        p_perf_script = subprocess.Popen(perf_script_cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
                                    close_fds=True)
        p_perf_script.wait()    
        outs, errs = p_perf_script.communicate(timeout=TIMEOUT_TIME)
        print("generate perf script: ")
        print("outs: ", outs)
        print("errs: ", errs) 
        ret_code2 = p_perf_script.poll()
        if ret_code2 == 0:
            print("[{}] perf script generation finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        else:
            print("perf script generation get error {}".format(ret_code2))

        # stop Resource Monition (if activated)
        if collect_resource:
            rm.terminate()
            cpu, avg_read_io, avg_write_io, avg_virtual_memory, avg_physical_memory = rm.get_monitor_data_avg()
        else:
            cpu, avg_read_io, avg_write_io, avg_virtual_memory, avg_physical_memory = 0, 0, 0, 0, 0
        # TODO 接口
        external_metrics = self.get_external_metrics(filename)
        internal_metrics, dirty_pages, hit_ratio, page_data = self.db._post_handle(internal_metrics)
        logger.info('internal metrics: {}.'.format(list(internal_metrics)))
        
        result_flag = 1
        function_range = self.get_perf_function_range(external_metrics[0],result_flag,perf_txt)

        return benchmark_timeout, external_metrics, internal_metrics, (
            cpu, avg_read_io, avg_write_io, avg_virtual_memory, avg_physical_memory, dirty_pages, hit_ratio, page_data), function_range
        
    def get_states_expe_tpch(self, collect_resource=0):
        # start Internal Metrics Collection
        internal_metrics = Manager().list()
        im = mp.Process(target=self.db.get_internal_metrics,
                        args=(internal_metrics, BENCHMARK_RUNNING_TIME, BENCHMARK_WARMING_TIME))
        self.db.set_im_alive(True)
        im.start()
        
        # # start Resource Monition (if activated)
        if collect_resource:
            rm = ResourceMonitor(self.db.pid, 1, BENCHMARK_WARMING_TIME, BENCHMARK_RUNNING_TIME)
            rm.run()
        
        # start Benchmark
        benchmark_timeout = False
        cmd, filename = self.get_benchmark_cmd()
        print("cmd: ",cmd)
        
        perf_output_dir = f"perf_data_{self.args_tune['task_id']}"  # Define a unified folder for perf data
        os.makedirs(perf_output_dir, exist_ok=True)
        timestamp = int(time.time())
        
        print("[{}] benchmark start!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        p_benchmark = subprocess.Popen(cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
                                    close_fds=True)
        # collect perf data
        perf_file = os.path.join(perf_output_dir, f'perf_data_{timestamp}.data')
        # Perf command to collect performance data, specifying the output file
        # time.sleep(10)
        pgrep_result = subprocess.check_output("pgrep -nx mysqld", shell=True).decode().strip()
        perf_cmd = f"perf record -F 300 -p {pgrep_result} -g -o {perf_file} -- sleep 50" 
        print(perf_cmd)
        time.sleep(5)
        print("[{}] perf data collection start!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        p_runperf = subprocess.Popen(perf_cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
                                    close_fds=True)
        p_runperf.wait()
        p_benchmark.wait()

        # try:
        outs, errs = p_benchmark.communicate(timeout=TIMEOUT_TIME)
        print(errs)
        print(outs)
        ret_code = p_benchmark.poll()
        if ret_code == 0:
            print("[{}] benchmark finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        else:
            print("run benchmark get error {}".format(ret_code))
        # except subprocess.TimeoutExpired:
        #     #benchmark_timeout = True
        #     print("[{}] benchmark timeout!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        outs, errs = p_runperf.communicate(timeout=TIMEOUT_TIME)
        print("collect perf data: ")
        print(errs)
        print(outs)
        ret_code1 = p_runperf.poll()
        if ret_code1 == 0:
            print("[{}] perf data collection finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        else:
            print("perf data collection get error {}".format(ret_code1))

        # terminate Benchmark
        if not self.remote_mode:
            subprocess.Popen(self.db.clear_cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
                            close_fds=True)
            print("[{}] clear processlist".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))

        # stop Internal Metrics Collection
        self.db.set_im_alive(False)
        im.join()
        
        # transfer perf data to perf txt
        flamegraph_dir = os.path.expanduser("/home/RUC/FlameGraph") 
        stackcollapse_script = os.path.join(flamegraph_dir, "stackcollapse-perf.pl")  # 确保这个脚本存在
        print(stackcollapse_script)
        
        perf_txt = os.path.join(perf_output_dir, f'perf_{timestamp}.txt')
        # perf_script_cmd = f"perf script -i {perf_file} > {perf_txt}"
        perf_script_cmd = f"perf script -i {perf_file} | {stackcollapse_script} > {perf_txt}"
        p_perf_script = subprocess.Popen(perf_script_cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
                                    close_fds=True)
        p_perf_script.wait()    
        outs, errs = p_perf_script.communicate(timeout=TIMEOUT_TIME)
        print("generate perf script: ")
        print(errs)
        print(outs) 
        ret_code2 = p_perf_script.poll()
        if ret_code2 == 0:
            print("[{}] perf script generation finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        else:
            print("perf script generation get error {}".format(ret_code2))

        # stop Resource Monition (if activated)
        if collect_resource:
            rm.terminate()
            cpu, avg_read_io, avg_write_io, avg_virtual_memory, avg_physical_memory = rm.get_monitor_data_avg()
        else:
            cpu, avg_read_io, avg_write_io, avg_virtual_memory, avg_physical_memory = 0, 0, 0, 0, 0

        external_metrics = self.get_external_metrics(filename)
        internal_metrics, dirty_pages, hit_ratio, page_data = self.db._post_handle(internal_metrics)
        logger.info('internal metrics: {}.'.format(list(internal_metrics)))

        result_flag = 3
        function_range = self.get_perf_function_range(external_metrics[0],result_flag,perf_txt)

        return benchmark_timeout, external_metrics, internal_metrics, (
            cpu, avg_read_io, avg_write_io, avg_virtual_memory, avg_physical_memory, dirty_pages, hit_ratio, page_data), function_range
    
     
    def get_states_expe_tpcc(self, collect_resource=0):
        # start Internal Metrics Collection
        internal_metrics = Manager().list()
        im = mp.Process(target=self.db.get_internal_metrics,
                        args=(internal_metrics, BENCHMARK_RUNNING_TIME, BENCHMARK_WARMING_TIME))
        self.db.set_im_alive(True)
        im.start()

        if collect_resource:
            rm = ResourceMonitor(self.db.pid, 1, BENCHMARK_WARMING_TIME, BENCHMARK_RUNNING_TIME)
            rm.run()
        # start Benchmark
        benchmark_timeout = False
        
        perf_output_dir = f"perf_data_{self.args_tune['task_id']}"   # Define a unified folder for perf data
        os.makedirs(perf_output_dir, exist_ok=True)

        timestamp = int(time.time())
        # TODO: benchbase
        result_path = "./optimization_results/temp_results"
        benchmark_path = "/home/benchbase/target/benchbase-mysql"
        for filename in os.listdir(result_path):
            print(f"REMOVE {filename}")
            filepath = os.path.join(result_path, filename)
            os.remove(filepath)
        
        with open(os.path.join(result_path, "out.txt"), 'w') as output_file:
            process = subprocess.Popen(
                ['java', '-jar', 'benchbase.jar', '-b', 'tpcc', 
                "-c", "config/mysql/sample_tpcc_config.xml", 
                "--create=false", "--clear=false", "--load=false", '--execute=true', 
                "-d", '/home/sysinsight/optimization_results/temp_results'],
                cwd=benchmark_path,
                stdout=output_file
            )
            # collect perf data
            perf_file = os.path.join(perf_output_dir, f'perf_data_{timestamp}.data')
            if(isinstance(self.db, MysqlDB)):
                pgrep_result = subprocess.check_output("pgrep -nx mysqld", shell=True).decode().strip()
                perf_cmd = f"perf record -F 300 -p {pgrep_result} -g -o {perf_file} -- sleep 50" 
                print("mysql: ",perf_cmd)

            elif (isinstance(self.db, PostgresqlDB)):
                lines = subprocess.check_output(
                    "ps -eo pid,ppid,cmd | grep '[p]ostgres.*--config_file'", 
                    shell=True
                ).decode().strip().splitlines()
                # 遍历每个 PID，找出其 cmdline 是否为主进程
                main_pid = None
                for line in lines:
                    parts = line.strip().split(None, 2)
                    if len(parts) < 3:
                        continue
                    pid, ppid, cmd = parts
                    # 过滤掉 sudo 启动命令（即不是实际 postgres 主进程）
                    if 'sudo' not in cmd:
                        main_pid = pid
                        break
                perf_cmd = f"perf record -F 300 -p {main_pid} -g -o {perf_file} -- sleep 50"
                print("postgresql: ",perf_cmd)
            time.sleep(5)
            print("[{}] perf data collection start!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
            p_runperf = subprocess.Popen(perf_cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
                                        close_fds=True)
            process.wait()
            p_runperf.wait()
        
        # try:
        outs, errs = process.communicate(timeout=TIMEOUT_TIME)
        print(errs)
        print(outs)
        ret_code = process.poll()
        if ret_code == 0:
            print("[{}] benchmark finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        else:
            print("run benchmark get error {}".format(ret_code))
        # except subprocess.TimeoutExpired:
        #     #benchmark_timeout = True
        #     print("[{}] benchmark timeout!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        outs, errs = p_runperf.communicate(timeout=TIMEOUT_TIME)
        print("collect perf data: ")
        print(errs)
        print(outs)
        ret_code1 = p_runperf.poll()
        if ret_code1 == 0:
            print("[{}] perf data collection finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        else:
            print("perf data collection get error {}".format(ret_code1))

        # terminate Benchmark
        if not self.remote_mode:
            subprocess.Popen(self.db.clear_cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
                            close_fds=True)
            print("[{}] clear processlist".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))

        # stop Internal Metrics Collection
        self.db.set_im_alive(False)
        im.join()
        
        # transfer perf data to perf txt
        flamegraph_dir = os.path.expanduser("/home/RUC/FlameGraph") 
        stackcollapse_script = os.path.join(flamegraph_dir, "stackcollapse-perf.pl")  # 确保这个脚本存在
        print(stackcollapse_script)
        
        perf_txt = os.path.join(perf_output_dir, f'perf_{timestamp}.txt')
        perf_script_cmd = f"perf script -i {perf_file} | {stackcollapse_script} > {perf_txt}"
        p_perf_script = subprocess.Popen(perf_script_cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
                                    close_fds=True)
        p_perf_script.wait()    
        outs, errs = p_perf_script.communicate(timeout=TIMEOUT_TIME)
        print("generate perf script: ")
        print(errs)
        print(outs) 
        ret_code2 = p_perf_script.poll()
        if ret_code2 == 0:
            print("[{}] perf script generation finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        else:
            print("perf script generation get error {}".format(ret_code2))

        # stop Resource Monition (if activated)
        if collect_resource:
            rm.terminate()
            cpu, avg_read_io, avg_write_io, avg_virtual_memory, avg_physical_memory = rm.get_monitor_data_avg()
        else:
            cpu, avg_read_io, avg_write_io, avg_virtual_memory, avg_physical_memory = 0, 0, 0, 0, 0

        # external_metrics = self.get_external_metrics(filename)
        
        # tpcc TODO: benchbase
        external_metrics = self.get_throughput_benchbase()
        print(external_metrics)
        
        internal_metrics, dirty_pages, hit_ratio, page_data = self.db._post_handle(internal_metrics)
        logger.info('internal metrics: {}.'.format(list(internal_metrics)))
        
        result_flag = 2
        function_range = self.get_perf_function_range(external_metrics[0],result_flag,perf_txt)

        return benchmark_timeout, external_metrics, internal_metrics, (
            cpu, avg_read_io, avg_write_io, avg_virtual_memory, avg_physical_memory, dirty_pages, hit_ratio, page_data), function_range
        
    
    def get_perf_function_range(self,tps_value, result_flag, filename=''):
    
        if not filename:
            filename = 'perf.txt'

        # 用于存储所有函数的采样次数
        function_counts = {}
        total_samples = 0

        try:
            # 打开文件进行读取
            with open(filename, 'r') as file:
                for line in file:
                    # 分割每行，格式为 [函数名1;函数名2;函数名3, 采样次数]
                    parts = line.strip().rsplit(' ', 1)
                    if len(parts) != 2:
                        print(f"文件格式错误：{line}")
                        continue
                    
                    function_names = parts[0].split(';')
                    try:
                        count = int(parts[1])
                    except ValueError:
                        print(f"采样次数不是有效的整数：{line}")
                        continue
                    
                    # 累加总采样次数
                    total_samples += count
                    
                    for function_name in function_names:
                        # 排除 [mysqld]和connection 函数
                        if function_name == "[mysqld]" or function_name == "connection" or function_name == "[unknown]":
                            continue
                        
                        if function_name in function_counts:
                            function_counts[function_name] += count
                        else:
                            function_counts[function_name] = count
        except FileNotFoundError:
            print(f"文件 {filename} 未找到。")
            return None

        # 计算每个函数的采样率
        function_percentages = {
            function_name: (count / total_samples) * 100 if total_samples > 0 else 0
            for function_name, count in function_counts.items()
        }

        # 按采样次数降序排序
        sorted_functions = sorted(function_counts.items(), key=lambda x: x[1], reverse=True)

        # 生成输出文件名
        base, ext = os.path.splitext(filename)
        result_suffix = {1: "sysbench", 2: "tpcc", 3: "tpch"}.get(result_flag, "counts")
        output_filename = f"{base}_counts_{result_suffix}.txt"

        try:
            with open(output_filename, 'w') as out: 
                out.write("Cycles\tFunction\tSampling Rate (%)\tAbsolute Count\n")
                for function_name, _ in sorted_functions:
                    count = function_counts[function_name]
                    percentage = function_percentages[function_name]
                    out.write(f"{count}\t{function_name}\t{percentage:.2f}%\n")
        except Exception as e:
            print(f"Write output file error: {e}")
            return None

        print(f"总 cycles 数量: {total_samples}")
        return output_filename

    def step_GP_data(self, knobs, collect_resource=True):
        #return False, np.random.rand(6), np.random.rand(65), np.random.rand(8)
        # re-init database if activated
        if self.reinit_interval > 0 and self.reinit_interval % RESTART_FREQUENCY == 0:
            if self.reinit:
                logger.info('reinitializing db begin')
                self.db.reinitdb_magic(self.remote_mode)
                logger.info('db reinitialized')
        self.step_count = self.step_count + 1
        self.reinit_interval = self.reinit_interval + 1

        # modify and apply knobs
        for key in knobs.keys():
            value = knobs[key]
            if not key in self.knobs_detail.keys() or not self.knobs_detail[key]['type'] == 'integer':
                continue
            if value > self.knobs_detail[key]['max']:
                knobs[key] = self.knobs_detail[key]['max']
                logger.info("{} with value of is larger than max, adjusted".format(key))
            elif value < self.knobs_detail[key]['min']:
                knobs[key] = self.knobs_detail[key]['min']
                logger.info("{} with value of is smaller than min, adjusted".format(key))

        logger.info("[step {}] generate knobs: {}\n".format(self.step_count, knobs))

        if self.online_mode:
            flag = self.db.apply_knobs_online(knobs)
        else:
            print("*********************************")
            flag = self.db.apply_knobs_offline(knobs)

        if not flag:
            if self.reinit:
                logger.info('reinitializing db begin')
                self.db.reinitdb_magic(self.remote_mode)
                logger.info('db reinitialized')

            raise Exception('Apply knobs failed!')
        
        # s = self.get_states(collect_resource=collect_resource)
        s = self.get_states_expe_tpch(collect_resource=collect_resource)


        # # drop and create database
        # if self.db.drop_and_create_db():
        #     logger.info('drop and create database success!')
        #     #load data
        #     load_cmd = [
        #         "sysbench",
        #         "--mysql-host=localhost",
        #         "--mysql-port=3306",
        #         "--mysql-user=root",
        #         "--mysql-password=Dbiir@500",
        #         "--mysql-db=sbtest",
        #         "--range-size=100",
        #         "--events=0",
        #         "--table-size=80000",
        #         "--tables=100",
        #         "--threads=150",
        #         "oltp_read_write",
        #         "prepare > load_data.out"
        #     ]
        #     load_cmd = " ".join(load_cmd)
        #     print("load_cmd: ", load_cmd)
        #     print("[{}] load data start!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
        #     p_load = subprocess.Popen(load_cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
        #                             close_fds=True)
        #     p_load.wait()
        #     print("[{}] load data finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))  
        #     outs, errs = p_load.communicate(timeout=TIMEOUT_TIME) 
        #     ret_code = p_load.poll()
        #     if ret_code == 0:
        #         print("[{}] load data finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))    
        #     else: 
        #         print("load data get error {}".format(ret_code))  

        #clean binlog
        # if self.step_count / 100 == 0:
        if self.db.clean_binlog():
            logger.info('clean binlog success!')
            print("[{}] clean binlog finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
            

        if s is None:
            if self.reinit:
                logger.info('reinitializing db begin')
                self.db.reinitdb_magic(self.remote_mode)
                logger.info('db reinitialized')

            raise Exception('Get states failed!')

        timeout, external_metrics, internal_metrics, resource, function_range_name = s
        # timeout, external_metrics, internal_metrics,function_range_name = s

        format_str = '{}|tps_{}|lat_{}|qps_{}|tpsVar_{}|latVar_{}|qpsVar_{}|{}|65d\n'
        res = format_str.format(knobs, str(external_metrics[0]), str(external_metrics[1]), str(external_metrics[2]),
                                external_metrics[3], external_metrics[4],
                                external_metrics[5], list(internal_metrics))

        return timeout, external_metrics, internal_metrics, resource, function_range_name

    def step_GP_sysinght(self, knobs, collect_resource=True):
        #return False, np.random.rand(6), np.random.rand(65), np.random.rand(8)
        # re-init database if activated
        # if self.reinit_interval > 0 and self.reinit_interval % RESTART_FREQUENCY == 0:
        #     if self.reinit:
        #         logger.info('reinitializing db begin')
        #         self.db.reinitdb_magic(self.remote_mode)
        #         logger.info('db reinitialized')
        # self.step_count = self.step_count + 1
        # self.reinit_interval = self.reinit_interval + 1
        if self.args['workload'] == 'tpcc':
            # print(self.reinit)
            if self.reinit:
                print('reinitializing db begin')
                self.db.reinitdb_magic()
                print('db reinitialized')

        self.step_count = self.step_count + 1
        self.reinit_interval = self.reinit_interval + 1

        # modify and apply knobs
        for key in knobs.keys():
            value = knobs[key]
            if not key in self.knobs_detail.keys() or not self.knobs_detail[key]['type'] == 'integer':
                continue
            if value > self.knobs_detail[key]['max']:
                knobs[key] = self.knobs_detail[key]['max']
                logger.info("{} with value of is larger than max, adjusted".format(key))
            elif value < self.knobs_detail[key]['min']:
                knobs[key] = self.knobs_detail[key]['min']
                logger.info("{} with value of is smaller than min, adjusted".format(key))

        logger.info("[step {}] generate knobs: {}\n".format(self.step_count, knobs))

        if self.online_mode:
            flag = self.db.apply_knobs_online(knobs)
        else:
            print("*********************************")
            flag = self.db.apply_knobs_offline(knobs)
            print("oooooo")

        if not flag:
            if self.reinit:
                logger.info('reinitializing db begin')
                self.db.reinitdb_magic(self.remote_mode)
                logger.info('db reinitialized')

            raise Exception('Apply knobs failed!')
        
        # s = self.get_states(collect_resource=collect_resource)
        # s = self.get_states_expe(collect_resource=collect_resource)
        method_name = f"get_states_expe_{self.args['workload']}"
        print(method_name)
        method = getattr(self, method_name, None)  # 从 self 获取方法
        if callable(method):  
            s = method(collect_resource=collect_resource)
        else:
            raise AttributeError(f"Method {method_name} not found in {self.__class__.__name__}")

        if self.args['workload'] == 'sysbench':
            # drop and create database
            if self.db.drop_and_create_db():
                logger.info('drop and create database success!')
                #load data
                load_cmd = [
                    "sysbench",
                    "--mysql-host=localhost",
                    "--mysql-port=3306",
                    "--mysql-user=root",
                    "--mysql-password=Dbiir@500",
                    "--mysql-db=sbtest",
                    "--range-size=100",
                    "--events=0",
                    "--table-size=80000",
                    "--tables=100",
                    "--threads=150",
                    "oltp_read_write",
                    "prepare > load_data.out"
                ]
                load_cmd = " ".join(load_cmd)
                print("load_cmd: ", load_cmd)
                print("[{}] load data start!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
                p_load = subprocess.Popen(load_cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE,
                                        close_fds=True)
                p_load.wait()
                print("[{}] load data finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))  
                outs, errs = p_load.communicate(timeout=TIMEOUT_TIME) 
                ret_code = p_load.poll()
                if ret_code == 0:
                    print("[{}] load data finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))    
                else: 
                    print("load data get error {}".format(ret_code))  

        # #clean binlog
        # if self.db.clean_binlog():
        #     logger.info('clean binlog success!')
        #     print("[{}] clean binlog finished!".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
            
        if s is None:
            if self.reinit:
                logger.info('reinitializing db begin')
                self.db.reinitdb_magic(self.remote_mode)
                logger.info('db reinitialized')

            raise Exception('Get states failed!')

        timeout, external_metrics, internal_metrics, resource, function_range_name = s
        # timeout, external_metrics, internal_metrics,function_range_name,resource = s

        format_str = '{}|tps_{}|lat_{}|qps_{}|tpsVar_{}|latVar_{}|qpsVar_{}|{}|65d\n'
        res = format_str.format(knobs, str(external_metrics[0]), str(external_metrics[1]), str(external_metrics[2]),
                                external_metrics[3], external_metrics[4],
                                external_metrics[5], list(internal_metrics))

         # compare function_range_name file and default file
        normal_file = f"/home/sysinsight/DBTuner/collectData/function_normal_{self.args['workload']}.csv"
        # out_of_function_file,sorted_out_of_range_functions = self.compare_file(function_range_name,default_file)
        out_of_function_file_sample_rate,sorted_out_of_range_functions_sample_rate = self.compare_file_sample_rate(function_range_name,normal_file)

        # return timeout, external_metrics, internal_metrics, function_range_name, out_of_function_file
        return timeout, external_metrics, internal_metrics, resource, function_range_name, out_of_function_file_sample_rate

    def compare_file(self, file, default_file):
        file_data = pd.read_csv(file, sep='\t')
        default_data = pd.read_csv(default_file)

        out_of_range_functions = []

        for index, row in file_data.iterrows():
            function_name = row['Function']
            absolute_count = row['Absolute Count']

            csv_row = default_data[default_data['Function'] == function_name]
            if not csv_row.empty:
                min_value = csv_row['Min'].values[0]
                max_value = csv_row['Max'].values[0]
                mean_value = csv_row['Mean'].values[0]

                # Check if the count is out of range
                if absolute_count < min_value or absolute_count > max_value:
                    # Calculate absolute difference from mean
                    diff_from_mean = abs(absolute_count - mean_value)
                    
                    # Determine if the count increased (1) or decreased (0)
                    change = 1 if absolute_count > mean_value else 0
                    
                    out_of_range_functions.append({
                        'Function': function_name,
                        'Absolute Count': absolute_count,
                        'Diff From Mean': diff_from_mean,
                        'Change': change  # 1 for increased, 0 for decreased
                    })

        # Sort functions by their difference from the mean, descending
        sorted_out_of_range_functions = sorted(out_of_range_functions, key=lambda x: x['Diff From Mean'], reverse=True)

        # Define output file path
        base, ext = os.path.splitext(file)
        output_file_path = f"{base}_keyFunctions.txt"

        # Write the sorted results to the output file
        with open(output_file_path, 'w') as f:
            f.write('Function\tAbsolute Count\tDiff From Mean\tChange\n')
            for item in sorted_out_of_range_functions:
                f.write(f"{item['Function']}\t{item['Absolute Count']}\t{item['Diff From Mean']}\t{item['Change']}\n")

        print(f"Results written to {output_file_path}")

        return output_file_path, sorted_out_of_range_functions
    

    # sample rate compare
    def compare_file_sample_rate(self, file, normal_file):
        file_data = pd.read_csv(file, sep='\t')
        normal_file_data = pd.read_csv(normal_file)
        # 转换采样率为数值格式（去掉百分号并转为浮点数）
        file_data['Sampling Rate (%)'] = file_data['Sampling Rate (%)'].str.replace('%', '').astype(float)
        normal_file_data['Min Sampling Rate (%)'] = normal_file_data['Min Sampling Rate (%)'].astype(float)
        normal_file_data['Max Sampling Rate (%)'] = normal_file_data['Max Sampling Rate (%)'].astype(float)
        normal_file_data['Average Sampling Rate (%)'] = normal_file_data['Average Sampling Rate (%)'].astype(float)

        out_of_range_functions = []

        for index, row in file_data.iterrows():
            function_name = row['Function']
            # absolute_count = row['Absolute Count']
            sample_rate = row['Sampling Rate (%)']

            csv_row = normal_file_data[normal_file_data['Function'] == function_name]
            if not csv_row.empty:
                min_value = csv_row['Min Sampling Rate (%)'].values[0]
                max_value = csv_row['Max Sampling Rate (%)'].values[0]
                mean_value = csv_row['Average Sampling Rate (%)'].values[0]

                # Check if the count is out of range
                if sample_rate < min_value or sample_rate > max_value:
                    # Calculate absolute difference from mean
                    diff_from_mean = abs(sample_rate - mean_value)
                    
                    # Determine if the count increased (1) or decreased (0)
                    change = 1 if sample_rate > mean_value else 0
                    
                    out_of_range_functions.append({
                        'Function': function_name,
                        'Sample Rate': sample_rate,
                        'Diff From Mean': diff_from_mean,
                        'Change': change  # 1 for increased, 0 for decreased
                    })

        sorted_out_of_range_functions = sorted(out_of_range_functions, key=lambda x: x['Diff From Mean'], reverse=True)

        # Define output file path
        base, ext = os.path.splitext(file)
        output_file_path = f"{base}_btFunctions.txt"

        # Write the sorted results to the output file
        with open(output_file_path, 'w') as f:
            f.write('Function\tSample Rate(%)\tDiff From Mean\tChange\n')
            for item in sorted_out_of_range_functions:
                f.write(f"{item['Function']}\t{item['Sample Rate']}\t{item['Diff From Mean']}\t{item['Change']}\n")

        print(f"Results written to {output_file_path}")

        return output_file_path, sorted_out_of_range_functions

    # 获取tpcc的metrics
    def get_tpcc_metrics(self, filename):
        with open(filename, "r") as file:
            lines = file.readlines()
        tpmc = None
        tps = -1 
        
        for line in lines:
            # 提取 TpmC
            if "TpmC" in line:
                match = re.search(r"(\d+\.?\d*) TpmC", line)
                if match:
                    tpmc = float(match.group(1))
                    tps = tpmc/60
        return [tps,-1,-1,-1,-1,-1]
    
    
    def get_latest_summary_file(self,):
        files = glob.glob(os.path.join("./optimization_results/temp_results", '*summary.json'))
        files.sort(key=os.path.getmtime, reverse=True)  
        return files[0] if files else None
    
    def get_throughput_benchbase(self):
        summary_file = self.get_latest_summary_file()
        print(summary_file)
        throughput = None
        try:
            with open(summary_file, 'r') as file:
                data = json.load(file)
            throughput = data["Throughput (requests/second)"]
            if throughput==-1 or throughput == 2147483647:
                raise ValueError(f"Benchbase return error throughput:{throughput}")
            print(f"Throughput: {throughput}")
        except Exception as e:
            print(f'Exception for JSON: {e}')
        # return throughput
        return [throughput,-1,-1,-1,-1,-1]
    
    def get_metric_benchbase(self):
        summary_file = self.get_latest_summary_file()
        throughput = None
        average_latency = None
        try:
            with open(summary_file, 'r') as file:
                data = json.load(file)
            throughput = data["Throughput (requests/second)"]
            average_latency = data["Latency Distribution"]["95th Percentile Latency (microseconds)"]
            if throughput==-1 or throughput == 2147483647:
                raise ValueError(f"Benchbase return error throughput:{throughput}")
            if average_latency == -1 or average_latency == 2147483647:
                raise ValueError(f"Benchbase return error average_latency:{average_latency}")
            print(f"Latency: {average_latency}")
            print(f"Throughput: {throughput}")
        except Exception as e:
            print(f'Exception for JSON: {e}')
        # return throughput
        lat = average_latency/1000000
        return [throughput,lat,-1,-1,-1,-1]
        
# ################keenInsight#####################  

    # def get_states_expe(self, collect_resource=0):
    #     # start Internal Metrics Collection
    #     internal_metrics = Manager().list()
    #     im = mp.Process(target=self.db.get_internal_metrics,
    #                     args=(internal_metrics, BENCHMARK_RUNNING_TIME, BENCHMARK_WARMING_TIME))
    #     self.db.set_im_alive(True)
    #     im.start()
        
    #     # # start Resource Monition (if activated)
    #     if collect_resource:
    #         rm = ResourceMonitor(self.db.pid, 1, BENCHMARK_WARMING_TIME, BENCHMARK_RUNNING_TIME)
    #         rm.run()
        
    #     # start Benchmark
    #     benchmark_timeout = False
        
    #     perf_output_dir = f"perf_data_{self.args_tune['task_id']}"
    #     os.makedirs(perf_output_dir, exist_ok=True)