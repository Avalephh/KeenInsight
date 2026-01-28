import multiprocessing as mp
import time
from multiprocessing import Manager

import numpy as np
import psutil


class ResourceMonitor:

    def __init__(self, pid, interval, warmup, t):
        self.interval = interval
        self.t = t
        self.process = psutil.Process(pid)
        self.warmup = warmup
        self.ticks = int(self.t / self.interval)

        # check if process exists
        try:
            self.process = psutil.Process(pid)
            self.n_cpu = len(self.process.cpu_affinity())
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            # if process does not exist, create a virtual process object
            self.process = None
            self.n_cpu = 1
            print(f"Warning: Process {pid} does not exist or cannot be accessed, using default values")

        self.cpu_usage_seq = Manager().list()
        self.mem_virtual_usage_seq = Manager().list()
        self.mem_physical_usage_seq = Manager().list()
        self.io_read_seq, self.io_write_seq = Manager().list(), Manager().list()
        self.net_recv_seq, self.net_sent_seq = Manager().list(), Manager().list()
        self.dirty_pages_pct_seq = Manager().list()
        self.processes = []
        self.alive = mp.Value("b", False)

    def run(self):
        p1 = mp.Process(target=self.monitor_cpu_usage, args=())
        self.processes.append(p1)
        p2 = mp.Process(target=self.monitor_mem_usage, args=())
        self.processes.append(p2)
        p3 = mp.Process(target=self.monitor_io_usage, args=())
        self.processes.append(p3)
        p4 = mp.Process(target=self.monitor_net_usage, args=())
        self.processes.append(p4)
        self.alive.value = True
        [proc.start() for proc in self.processes]

    def resample(self, arr, target_len):
        arr = np.array(arr)
        if len(arr) == target_len:
            return arr.tolist()
        elif len(arr) == 0:
            return [0] * target_len

        x_old = np.linspace(0, 1, len(arr))
        x_new = np.linspace(0, 1, target_len)
        return np.interp(x_new, x_old, arr).tolist()

    def _wait_for_processes(self, timeout=10):
        for proc in self.processes:
            if proc.is_alive():
                proc.join(timeout=timeout)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=2)
                    if proc.is_alive():
                        proc.kill()
                        proc.join()

    def _sleep_with_check(self, duration):
        """使用更短的sleep间隔，以便更快响应终止信号"""
        for _ in range(int(duration * 10)):
            if not self.alive.value:
                return
            time.sleep(0.1)

    def get_monitor_data(self, process_len=9, timeout=10):
        self._wait_for_processes(timeout)

        cpu = self.resample(list(self.cpu_usage_seq), process_len)
        io_read = self.resample(list(self.io_read_seq), process_len)
        io_write = self.resample(list(self.io_write_seq), process_len)
        mem_virtual = self.resample(list(self.mem_virtual_usage_seq), process_len)
        mem_physical = self.resample(list(self.mem_physical_usage_seq), process_len)
        net_recv = self.resample(list(self.net_recv_seq), process_len)
        net_sent = self.resample(list(self.net_sent_seq), process_len)

        return cpu, io_read, io_write, mem_virtual, mem_physical, net_recv, net_sent

    def get_monitor_data_avg(self, timeout=10):
        self._wait_for_processes(timeout)
        cpu = list(self.cpu_usage_seq)
        mem_virtual = list(self.mem_virtual_usage_seq)
        mem_physical = list(self.mem_physical_usage_seq)
        io_read = list(self.io_read_seq)
        io_write = list(self.io_write_seq)
        net_recv = list(self.net_recv_seq)
        net_sent = list(self.net_sent_seq)

        avg_cpu = sum(cpu) / (len(cpu) + 1e-9) / self.n_cpu
        avg_read_io = sum(io_read) / (len(io_read) + 1e-9)
        avg_write_io = sum(io_write) / (len(io_write) + 1e-9)
        avg_virtual_memory = sum(mem_virtual) / (len(mem_virtual) + 1e-9)
        avg_physical_memory = sum(mem_physical) / (len(mem_physical) + 1e-9)
        avg_net_recv = sum(net_recv) / (len(net_recv) + 1e-9)
        avg_net_sent = sum(net_sent) / (len(net_sent) + 1e-9)
        return (
            avg_cpu,
            avg_read_io,
            avg_write_io,
            avg_virtual_memory,
            avg_physical_memory,
            avg_net_recv,
            avg_net_sent,
        )

    def monitor_mem_usage(self):
        count = 0
        while self.alive.value and count < self.ticks:
            if count < self.warmup:
                self._sleep_with_check(self.interval)
                count = count + 1
                continue
            if self.process is None:
                self.mem_physical_usage_seq.append(0.0)
                self.mem_virtual_usage_seq.append(0.0)
            else:
                try:
                    mem_physical = self.process.memory_info()[0] / (1024.0 * 1024.0 * 1024.0)
                    mem_virtual = self.process.memory_info()[1] / (1024.0 * 1024.0 * 1024.0)
                    self.mem_physical_usage_seq.append(mem_physical)
                    self.mem_virtual_usage_seq.append(mem_virtual)
                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    self.mem_physical_usage_seq.append(0.0)
                    self.mem_virtual_usage_seq.append(0.0)
            self._sleep_with_check(self.interval)
            count += 1

    def monitor_io_usage(self):
        count = 0
        while self.alive.value and count < self.ticks:
            if count < self.warmup:
                self._sleep_with_check(self.interval)
                count = count + 1
                continue
            try:
                sp1 = psutil.disk_io_counters()
                self._sleep_with_check(self.interval)
                if not self.alive.value:
                    return
                sp2 = psutil.disk_io_counters()
                self.io_read_seq.append((sp2[2] - sp1[2]) / (1024.0 * 1024.0))
                self.io_write_seq.append((sp2[3] - sp1[3]) / (1024.0 * 1024.0))
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
                TypeError,
            ):
                self.io_read_seq.append(0.0)
                self.io_write_seq.append(0.0)
            count += 1

    def monitor_cpu_usage(self):
        count = 0
        while self.alive.value and count < self.ticks:
            if count < self.warmup:
                self._sleep_with_check(self.interval)
                count = count + 1
                continue
            if self.process is None:
                self.cpu_usage_seq.append(0.0)
            else:
                try:
                    # cpu_percent 的 interval 参数会阻塞，但这是必要的
                    # 如果 alive 被设置为 False，下次循环时会退出
                    cpu = self.process.cpu_percent(interval=self.interval)
                    self.cpu_usage_seq.append(cpu)
                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    self.cpu_usage_seq.append(0.0)
            count += 1

    def monitor_net_usage(self):
        count = 0
        while self.alive.value and count < self.ticks:
            if count < self.warmup:
                self._sleep_with_check(self.interval)
                count = count + 1
                continue
            try:
                net1 = psutil.net_io_counters()
                self._sleep_with_check(self.interval)
                if not self.alive.value:
                    return
                net2 = psutil.net_io_counters()
                self.net_recv_seq.append((net2.bytes_recv - net1.bytes_recv) / (1024.0 * 1024.0))
                self.net_sent_seq.append((net2.bytes_sent - net1.bytes_sent) / (1024.0 * 1024.0))
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
                TypeError,
            ):
                self.net_recv_seq.append(0.0)
                self.net_sent_seq.append(0.0)
            count += 1

    def terminate(self):
        self.alive.value = False
        time.sleep(0.1)
        for proc in self.processes:
            if proc.is_alive():
                proc.terminate()
