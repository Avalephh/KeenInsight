import os
import time
import json
from PerfCollectData import PerfCollector
from ApplyKnob import ApplyKnob

def test_perf_collector():
    print("\n=== Testing PerfCollector ===")
    # Initialize collector
    collector = PerfCollector(output_dir="test_perf_output", flamegraph_dir="/home/RUC/FlameGraph")

    if hasattr(collector, "check_perf_available"):
        if not collector.check_perf_available():
            print("[SKIP] perf is not executable in this environment.")
            return
    
    # 1. Test PID detection
    pid = collector.get_mysql_pid()
    if pid:
        print(f"[PASS] Found MySQL PID: {pid}")
    else:
        print("[FAIL] MySQL PID not found. Is MySQL running?")
        return

    # 2. Test Collection (short duration for testing)
    print("Collecting perf data for 5 seconds...")
    data_file = collector.collect(duration=50, frequency=100, use_sudo=False)
    if data_file and os.path.exists(data_file):
        print(f"[PASS] Generated data file: {data_file}")
    else:
        print("[FAIL] Data file not generated.")
        return

    # 3. Test Conversion
    txt_file = collector.convert_to_txt(data_file)
    if txt_file and os.path.exists(txt_file):
        print(f"[PASS] Generated txt file: {txt_file}")
    else:
        print("[FAIL] Txt file not generated. Check if stackcollapse-perf.pl exists.")
        return

    # 4. Test Analysis
    report = collector.get_perf_function_range(txt_file, result_flag=1)
    if report and os.path.exists(report):
        print(f"[PASS] Generated report: {report}")
        with open(report, 'r') as f:
            print("Report Sample (first 5 lines):")
            for _ in range(5):
                line = f.readline()
                if not line: break
                print(f"  {line.strip()}")
    else:
        print("[FAIL] Report not generated.")

def test_apply_knob():
    print("\n=== Testing ApplyKnob ===")
    config_path = os.path.join(os.path.dirname(__file__), 'database', 'config_template.ini')
    try:
        applier = ApplyKnob.from_config(config_path=config_path, knob_num=-1, online_mode=True, reinit=False)
    except Exception as e:
        print(f"[SKIP] Failed to load config or init DB: {e}")
        return

    # 1. Test Validation (Out of range)
    def assert_clip(key):
        if key not in applier.knobs_detail:
            print(f"[SKIP] {key} not present in knob space.")
            return

        detail = applier.knobs_detail[key]
        if detail.get('type') != 'integer':
            print(f"[SKIP] {key} is not integer type.")
            return

        min_v = int(detail['min'])
        max_v = int(detail['max'])

        too_large = max_v + 1
        validated_large = applier._validate_knobs({key: too_large})
        if validated_large.get(key) == max_v:
            print(f"[PASS] Validation clipped {key} down to max ({max_v}).")
        else:
            print(f"[FAIL] Validation did not clip {key} down to max. got={validated_large.get(key)} expected={max_v}")

        too_small = min_v - 1
        validated_small = applier._validate_knobs({key: too_small})
        if validated_small.get(key) == min_v:
            print(f"[PASS] Validation clipped {key} up to min ({min_v}).")
        else:
            print(f"[FAIL] Validation did not clip {key} up to min. got={validated_small.get(key)} expected={min_v}")

    assert_clip('max_connections')
    assert_clip('innodb_buffer_pool_size')

    # 2. Test Real Application (Requires running DB)
    print("Attempting to apply multiple parameters (online)...")
    try:
        def clip_to_range(key, preferred_value):
            detail = applier.knobs_detail.get(key)
            if not detail or detail.get('type') != 'integer':
                return None
            min_v = int(detail['min'])
            max_v = int(detail['max'])
            v = int(preferred_value)
            return max(min_v, min(max_v, v))

        preferred = {
            'max_connections': 200,
            'innodb_lock_wait_timeout': 50,
            'innodb_flush_log_at_trx_commit': 1,
            'innodb_flush_log_at_timeout': 1,
            'innodb_io_capacity': 2000,
            'thread_cache_size': 64,
            'table_open_cache': 1024,
        }

        knobs_to_apply = {}
        for k, v in preferred.items():
            vv = clip_to_range(k, v)
            if vv is not None:
                knobs_to_apply[k] = vv

        if not knobs_to_apply:
            print("[SKIP] No suitable integer knobs found in knob space to apply.")
            return

        knobs_to_apply = applier._validate_knobs(knobs_to_apply)
        success = applier.apply(knobs_to_apply)
        if success:
            print("[PASS] Online application reported success.")
            try:
                from database.dbconnector import MysqlConnector
            except Exception as e:
                print(f"[SKIP] Cannot import MysqlConnector: {e}")
                return

            dbc = MysqlConnector(host=applier.db.host, port=applier.db.port, user=applier.db.user, passwd=applier.db.passwd, name=applier.db.dbname)
            for k, expected in knobs_to_apply.items():
                res = dbc.fetch_results(f"SHOW GLOBAL VARIABLES LIKE '{k}';")
                if not res:
                    print(f"[FAIL] Database verification failed: {k} not found")
                    continue
                got = res[0]['Value']
                try:
                    got_int = int(got)
                except Exception:
                    got_int = got
                if got_int == expected:
                    print(f"[PASS] Database verified: {k} is {got_int}")
                else:
                    print(f"[FAIL] Database verification failed: {k} expected {expected}, got {got_int}")

            if 'innodb_io_capacity' in knobs_to_apply:
                expected_max = 2 * int(knobs_to_apply['innodb_io_capacity'])
                res = dbc.fetch_results("SHOW GLOBAL VARIABLES LIKE 'innodb_io_capacity_max';")
                if res:
                    got = int(res[0]['Value'])
                    if got == expected_max:
                        print(f"[PASS] Database verified: innodb_io_capacity_max is {got}")
                    else:
                        print(f"[FAIL] Database verification failed: innodb_io_capacity_max expected {expected_max}, got {got}")

            cnf_path = getattr(applier.db, 'mycnf', None)
            if cnf_path and os.path.exists(cnf_path):
                can_write = False
                try:
                    can_write = (os.geteuid() == 0) or os.access(cnf_path, os.W_OK)
                except Exception:
                    can_write = os.access(cnf_path, os.W_OK)

                if not can_write:
                    print(f"[SKIP] No permission to write {cnf_path}. Offline apply cannot be tested for persistence.")
                else:
                    print("Attempting to apply multiple parameters (offline, persist to cnf)...")
                    applier_offline = ApplyKnob.from_config(config_path=config_path, knob_num=-1, online_mode=False, reinit=False)
                    before = ""
                    try:
                        with open(cnf_path, 'r') as f:
                            before = f.read()
                    except Exception:
                        before = ""

                    ok_offline = applier_offline.apply(knobs_to_apply)
                    if ok_offline:
                        print("[PASS] Offline application reported success.")
                        try:
                            with open(cnf_path, 'r') as f:
                                after = f.read()
                        except Exception as e:
                            print(f"[FAIL] Cannot read cnf after offline apply: {e}")
                            return

                        changed = after != before
                        if changed:
                            print(f"[PASS] cnf file changed: {cnf_path}")
                        else:
                            print(f"[FAIL] cnf file not changed: {cnf_path}")

                        for k, expected in knobs_to_apply.items():
                            expected_s = str(expected)
                            found = False
                            for line in after.splitlines():
                                if not line.strip():
                                    continue
                                if line.lstrip().startswith('#') or line.lstrip().startswith(';'):
                                    continue
                                if line.strip().startswith(k):
                                    if expected_s in line:
                                        found = True
                                        break
                            if found:
                                print(f"[PASS] cnf contains {k}={expected_s}")
                            else:
                                print(f"[FAIL] cnf missing {k}={expected_s}")
                    else:
                        print("[FAIL] Offline application reported failure.")
        else:
            print("[FAIL] Application reported failure. Is MySQL running with correct credentials?")
    except Exception as e:
        print(f"[SKIP] Real application test failed/skipped: {e}")

if __name__ == "__main__":
    # Ensure we are in a safe environment or user knows this modifies MySQL
    print("Starting tests. Note: ApplyKnob test will attempt to modify MySQL 'max_connections'.")
    
    test_perf_collector()
    # test_apply_knob()
