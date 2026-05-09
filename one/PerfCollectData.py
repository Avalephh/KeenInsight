import os
import time
import subprocess
import shutil

class PerfCollector:
    """
    A class to collect system function data using 'perf' during MySQL stress testing.
    It can record perf data, convert it to a stackcollapse format, and analyze function frequencies.
    """
    def __init__(self, output_dir=None, flamegraph_dir=None):
        """
        Initialize the PerfCollector.

        Args:
            output_dir (str): Directory where perf data and results will be stored.
            flamegraph_dir (str): Directory containing FlameGraph tools (specifically stackcollapse-perf.pl).
        """
        from config import PERF_OUTPUT_DIR, FLAMEGRAPH_DIR
        self.output_dir = output_dir or PERF_OUTPUT_DIR
        self.flamegraph_dir = flamegraph_dir or FLAMEGRAPH_DIR
        self.stackcollapse_script = os.path.join(self.flamegraph_dir, "stackcollapse-perf.pl")
        os.makedirs(self.output_dir, exist_ok=True)

    def get_mysql_pid(self):
        """Get the PID of the running mysqld process."""
        try:
            pid = subprocess.check_output("pgrep -nx mysqld", shell=True).decode().strip()
            return pid
        except subprocess.CalledProcessError:
            print("Error: MySQL process (mysqld) not found.")
            return None

    def check_perf_available(self):
        """Check if 'perf' is installed and executable by the current user."""
        try:
            # Check if perf binary exists and is executable
            perf_path = "/usr/bin/perf"
            if not os.path.exists(perf_path):
                # Try finding it in PATH
                perf_path = subprocess.check_output("which perf", shell=True).decode().strip()
            
            # Try running 'perf --version'
            process = subprocess.Popen([perf_path, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate()
            
            if process.returncode == 0:
                print(f"Perf is available: {stdout.decode().strip()}")
                return True
            else:
                print(f"Perf check failed (RC={process.returncode}): {stderr.decode().strip()}")
                if b"Permission denied" in stderr or b"Permission denied" in stdout:
                    print("Error: Permission denied when running 'perf'.")
                    print("This may be due to container/sandbox restrictions or missing execution permissions.")
                return False
        except Exception as e:
            print(f"Error checking perf availability: {e}")
            return False

    def collect(self, duration=50, frequency=300, use_sudo=False):
        """
        Run 'perf record' on the MySQL process.
        
        Args:
            duration (int): Duration of collection in seconds.
            frequency (int): Sampling frequency in Hz.
            use_sudo (bool): Whether to use 'sudo' for the perf command.
            
        Returns:
            str: Path to the generated .data file, or None if failed.
        """
        pid = self.get_mysql_pid()
        if not pid:
            return None

        timestamp = int(time.time())
        perf_data_file = os.path.join(self.output_dir, f'perf_data_{timestamp}.data')
        
        # Command: perf record -F <freq> -p <pid> -g -o <output> -- sleep <duration>
        base_cmd = f"perf record -F {frequency} -p {pid} -g -o {perf_data_file} -- sleep {duration}"
        perf_cmd = f"sudo {base_cmd}" if use_sudo else base_cmd
        
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting perf collection: {perf_cmd}")
        
        process = subprocess.Popen(perf_cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE)
        stdout, _ = process.communicate()
        
        if process.returncode == 0:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Perf collection finished: {perf_data_file}")
            return perf_data_file
        else:
            print(f"Perf collection failed with return code {process.returncode}")
            print(f"Error output:\n{stdout.decode()}")
            if process.returncode == 126:
                print("Hint: Error 126 usually means 'Permission Denied'.")
                print("Try setting 'use_sudo=True' in the collect() method or run:")
                print("  sudo sysctl -w kernel.perf_event_paranoid=-1")
            return None

    def convert_to_txt(self, perf_data_file):
        """
        Convert 'perf.data' to a collapsed stack text file.
        
        Args:
            perf_data_file (str): Path to the .data file.
            
        Returns:
            str: Path to the generated .txt file, or None if failed.
        """
        if not perf_data_file or not os.path.exists(perf_data_file):
            print(f"Error: Perf data file {perf_data_file} not found.")
            return None

        timestamp = os.path.basename(perf_data_file).split('_')[-1].split('.')[0]
        perf_txt_file = os.path.join(self.output_dir, f'perf_{timestamp}.txt')
        
        # Check if stackcollapse script exists
        if not os.path.exists(self.stackcollapse_script):
            print(f"Warning: {self.stackcollapse_script} not found. Only running 'perf script'.")
            perf_script_cmd = f"perf script --demangle -i {perf_data_file} > {perf_txt_file}"
        else:
            # Command: perf script -i <data> | stackcollapse-perf.pl > <txt>
            perf_script_cmd = f"perf script --demangle -i {perf_data_file} | {self.stackcollapse_script} > {perf_txt_file}"
        
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Converting to txt: {perf_script_cmd}")
        process = subprocess.Popen(perf_script_cmd, shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE)
        process.wait()
        
        if process.returncode == 0:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Perf txt generated: {perf_txt_file}")
            return perf_txt_file
        else:
            print(f"Perf script conversion failed with return code {process.returncode}")
            return None

    def get_perf_function_range(self, filename, result_flag=1):
        """
        Analyze the collapsed stack file and output function frequencies.
        Based on the logic from dbenv.py.
        
        Args:
            filename (str): Path to the collapsed stack .txt file.
            result_flag (int): 1 for sysbench, 2 for tpcc, 3 for tpch.
            
        Returns:
            str: Path to the analysis report file.
        """
        if not filename or not os.path.exists(filename):
            print(f"Error: File {filename} not found.")
            return None

        function_counts = {}
        function_abs_counts = {}
        total_samples = 0

        def simplify_name(name: str) -> str:
            name = str(name).strip()
            if not name:
                return name
            if '(' in name:
                name = name.split('(', 1)[0]
            name = name.strip()
            return name

        def demangle_symbols(symbols):
            if not symbols:
                return {}
            cxxfilt_path = shutil.which("c++filt")
            if not cxxfilt_path:
                return {s: s for s in symbols}
            inp = "\n".join(symbols) + "\n"
            p = subprocess.run([cxxfilt_path], input=inp, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if p.returncode != 0:
                return {s: s for s in symbols}
            outs = p.stdout.splitlines()
            m = {}
            for i, s in enumerate(symbols):
                m[s] = outs[i] if i < len(outs) else s
            return m

        raw_symbols = set()

        try:
            with open(filename, 'r') as file:
                for line in file:
                    parts = line.strip().rsplit(' ', 1)
                    if len(parts) != 2:
                        continue
                    
                    function_names = parts[0].split(';')
                    try:
                        count = int(parts[1])
                    except ValueError:
                        continue
                    
                    total_samples += count
                    
                    for function_name in function_names:
                        # Exclude common noise
                        if function_name in ["[mysqld]", "connection", "[unknown]"]:
                            continue
                        raw = str(function_name).strip()
                        if raw:
                            raw_symbols.add(raw)

                        function_counts[raw] = function_counts.get(raw, 0) + count
                        function_abs_counts[raw] = function_abs_counts.get(raw, 0) + 1
        except Exception as e:
            print(f"Error reading {filename}: {e}")
            return None

        demap = demangle_symbols(sorted(raw_symbols))

        # Calculate percentages
        function_percentages = {
            name: (count / total_samples) * 100 if total_samples > 0 else 0
            for name, count in function_counts.items()
        }

        # Sort by count descending
        sorted_functions = sorted(function_counts.items(), key=lambda x: x[1], reverse=True)

        # Generate output report name
        base, _ = os.path.splitext(filename)
        result_suffix = {1: "sysbench", 2: "tpcc", 3: "tpch"}.get(result_flag, "counts")
        output_filename = f"{base}_counts_{result_suffix}.txt"

        try:
            with open(output_filename, 'w') as out: 
                out.write("Cycles\tFunction\tSampling Rate (%)\tAbsolute Count\n")
                for function_name, count in sorted_functions:
                    percentage = function_percentages[function_name]
                    demangled = demap.get(function_name, function_name)
                    demangled = simplify_name(demangled)
                    abs_count = function_abs_counts.get(function_name, 0)
                    out.write(f"{count}\t{demangled}\t{percentage:.2f}%\t{abs_count}\n")
            print(f"Analysis report generated: {output_filename}")
            print(f"Total cycles: {total_samples}")
            return output_filename
        except Exception as e:
            print(f"Write output file error: {e}")
            return None

if __name__ == "__main__":
    # Example usage
    collector = PerfCollector()
    
    print("--- Starting Perf Collection Example ---")
    # 1. Collect perf data for 10 seconds
    data_file = collector.collect(duration=10, frequency=300)
    
    if data_file:
        # 2. Convert to collapsed stack format
        txt_file = collector.convert_to_txt(data_file)
        
        if txt_file:
            # 3. Analyze and generate report
            report = collector.get_perf_function_range(txt_file, result_flag=1)
            print(f"Process completed. Report at: {report}")
    else:
        print("Failed to collect data. Is MySQL running?")
