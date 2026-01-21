import os
import json

# def extract_rule(bottleneck_functions, knobs,  value):
#     # in: keyFunctions_list, rule_config_change, sel_condidate_point, sel_candidate_fval
    
#     print("Extracting rule...")
#     # print("ketFunctions_list: ", bottleneck_functions)
#     # print("rule_config_change: ", knobs)
#     # print("change_fval: ", value)

#     if not os.path.exists("/root/AI4DB/hzt/library/rule_library/rule_data.json"):
#         rule_data = {}
#     else:
#         with open("/root/AI4DB/hzt/library/rule_library/rule_data.json", "r") as f:
#           rule_data = json.load(f)
    
#     for function in bottleneck_functions:
#         # print("function: ", function)
            
#         if function in rule_data:
#             rule_data[function]["rules"].append({
#                 "change_knobs": knobs,
#                 "change_fval": value
#             })
#             rule_data[function]["observed_times"] += 1
        
#         else:
#             rule_data[function] = {
#                 "rules": [{
#                     "change_knobs": knobs,
#                     "change_fval": value
#                 }],
#                 "observed_times": 1,
#             }
        
#     # 保存更新后的规则数据
#     with open("/root/AI4DB/hzt/library/rule_library/rule_data.json", "w") as f:
#         json.dump(rule_data, f, indent=4)
    


def extract_rule(bottleneck_functions_and_knobs, knobs, value):

    print("Extracting rule...")
    
    # Path to the output files
    json_file_path = "/root/sysinsight-main/library/rule_library/rule_data.json"
    txt_file_path = "/root/sysinsight-main/library/rule_library/rule_data.txt" 

    # Load existing rule data
    if not os.path.exists(json_file_path):
        rule_data = {}
    else:
        with open(json_file_path, "r") as f:
            rule_data = json.load(f)
            
    bottleneck_functions = bottleneck_functions_and_knobs.keys()
    
    print("Bottleneck functions and knobs: ", bottleneck_functions_and_knobs)
    
    # Update rule data
    for function in bottleneck_functions:
        
        match_knobs = bottleneck_functions_and_knobs[function]
        # print("Matched knobs: ", match_knobs)
    
        change_knobs = [] 
    
        for knob in knobs:
            knob_name = knob["knob"]
            if knob_name in match_knobs:
                change_knobs.append(knob)
        
        if function in rule_data:
            rule_data[function]["rules"].append({
                "change_knobs": change_knobs,
                "change_fval": value
            })
            rule_data[function]["observed_times"] += 1
        else:
            rule_data[function] = {
                "rules": [{
                    "change_knobs": change_knobs,
                    "change_fval": value
                }],
                "observed_times": 1,
            }
    
    # Save updated rule data to JSON
    with open(json_file_path, "w") as f:
        json.dump(rule_data, f, indent=4)

    # Convert rule data to .txt format
    with open(txt_file_path, "w") as f:
        for function, data in rule_data.items():
            f.write(f"To bottleneck function {function},\n")
            for rule in data["rules"]:
                f.write("Change knobs:\n")
                for knob in rule["change_knobs"]:
                    knob_name = knob["knob"]
                    old_value = knob["old_value"]
                    new_value = knob["new_value"]
                    f.write(f"knobs_name {knob_name}: {old_value} to {new_value};\n")
                old_tps = rule["change_fval"]["old_fval"]
                new_tps = rule["change_fval"]["new_fval"]
                f.write(f"tps change from {old_tps} to {new_tps}.\n\n")
    print(f"Rules have been saved to {txt_file_path}")

def conclude_rule(rule_path):
    """
    Conclude the rule
    :param rule_path: the path of the rule
    :return: the concluded rule
    """
       
    return "No rule found."


def use_rule(rule_path):
    """
    Use the rule
    :param rule_path: the path of the rule
    :return: the used rule
    """
    
    return "No rule found."

if __name__ == "__main__":
    
    bottleneck_functions_and_knobs =  {'fil_space_release': ['innodb_buffer_pool_size', 'innodb_buffer_pool_instances', 'innodb_read_ahead_threshold', 'innodb_random_read_ahead'], 'buf_LRU_get_free_block': ['innodb_buffer_pool_size', 'innodb_buffer_pool_instances'], 'mtr_t::commit': ['innodb_buffer_pool_size'], 'buf_read_page_background': ['innodb_buffer_pool_size', 'innodb_buffer_pool_instances'], 'buf_LRU_get_free_only': ['innodb_buffer_pool_size'], 'log_flusher': ['innodb_flush_log_at_trx_commit'], 'ibuf_merge_in_background': ['innodb_io_capacity'], 'CreateIteratorFromAccessPath': ['join_buffer_size'], 'srv_purge_coordinator_thread': ['innodb_purge_batch_size'], 'trx_purge': ['innodb_purge_batch_size'], 'trx_purge_add_update_undo_to_history': ['innodb_purge_batch_size'], 'get_key_scans_params': ['read_rnd_buffer_size'], 'check_quick_select': ['read_rnd_buffer_size'], 'Sql_cmd_update::update_single_table': ['range_alloc_block_size'], 'Sql_cmd_delete::delete_from_single_table': ['range_alloc_block_size'], 'do_command': ['net_read_timeout'], 'ut_delay': ['innodb_spin_wait_delay'], 'lock_trx_release_locks': ['innodb_spin_wait_delay'], 'buf_read_page_low': ['innodb_buffer_pool_instances', 'innodb_read_ahead_threshold', 'innodb_random_read_ahead'], 'buf_page_init_for_read': ['innodb_buffer_pool_instances'], 'buf_read_ahead_random': ['innodb_buffer_pool_instances', 'innodb_random_read_ahead'], 'buf_read_ahead_linear': ['innodb_buffer_pool_instances', 'innodb_read_ahead_threshold'], 'buf_flush_do_batch': ['innodb_buffer_pool_instances'], 'ibuf_insert': ['innodb_buffer_pool_instances'], 'buf_flush_lists': ['innodb_buffer_pool_instances'], 'buf::Block_hint::buffer_fix_block_if_still_valid': ['innodb_buffer_pool_instances'], 'buf_LRU_block_free_non_file_page': ['innodb_buffer_pool_instances'], 'dblwr::force_flush': ['innodb_buffer_pool_instances'], 'buf_page_create': ['innodb_buffer_pool_instances'], 'fsync': ['innodb_use_fdatasync'], 'fdatasync': ['innodb_use_fdatasync'], 'ha_commit_trans': ['lock_wait_timeout'], 'open_table': ['lock_wait_timeout']}

    rule_config_change = [{'knob': 'thread_cache_size', 'old_value': 8192, 'new_value': 0}, {'knob': 'innodb_thread_concurrency', 'old_value': 500, 'new_value': 0}, {'knob': 'innodb_thread_sleep_delay', 'old_value': 500000, 'new_value': 10000}, {'knob': 'join_buffer_size', 'old_value': 536870976, 'new_value': 425984000}, {'knob': 'read_buffer_size', 'old_value': 1073743872, 'new_value': 131072}, {'knob': 'read_rnd_buffer_size', 'old_value': 67108864, 'new_value': 16777216}, {'knob': 'sort_buffer_size', 'old_value': 67125248, 'new_value': 262144}, {'knob': 'innodb_spin_wait_delay', 'old_value': 3000, 'new_value': 1000}, {'knob': 'innodb_sync_spin_loops', 'old_value': 15000, 'new_value': 30}, {'knob': 'optimizer_search_depth', 'old_value': 31, 'new_value': 62}, {'knob': 'innodb_io_capacity', 'old_value': 1000050, 'new_value': 1500000}, {'knob': 'innodb_io_capacity_max', 'old_value': 20050, 'new_value': 400}, {'knob': 'innodb_log_file_size', 'old_value': 538968064, 'new_value': 50331648}, {'knob': 'innodb_log_buffer_size', 'old_value': 2147614719, 'new_value': 16777216}, {'knob': 'innodb_flush_log_at_trx_commit', 'old_value': 1, 'new_value': 1}, {'knob': 'innodb_doublewrite', 'old_value': 'ON', 'new_value': 'ON'}, {'knob': 'sync_binlog', 'old_value': 2147483647, 'new_value': 1}, {'knob': 'table_open_cache', 'old_value': 125000, 'new_value': 2000}, {'knob': 'table_open_cache_instances', 'old_value': 32, 'new_value': 16}, {'knob': 'innodb_read_io_threads', 'old_value': 32, 'new_value': 4}, {'knob': 'innodb_write_io_threads', 'old_value': 32, 'new_value': 4}, {'knob': 'tmp_table_size', 'old_value': 536871424, 'new_value': 16777216}, {'knob': 'max_heap_table_size', 'old_value': 536879104, 'new_value': 16777216}, {'knob': 'innodb_adaptive_flushing', 'old_value': 'ON', 'new_value': 'ON'}, {'knob': 'innodb_flushing_avg_loops', 'old_value': 500, 'new_value': 30}, {'knob': 'innodb_flush_neighbors', 'old_value': 1, 'new_value': 1}, {'knob': 'innodb_flush_sync', 'old_value': 'ON', 'new_value': 'ON'}, {'knob': 'innodb_commit_concurrency', 'old_value': 500, 'new_value': 0}, {'knob': 'innodb_deadlock_detect', 'old_value': 'ON', 'new_value': 'ON'}, {'knob': 'innodb_table_locks', 'old_value': 'ON', 'new_value': 'ON'}, {'knob': 'innodb_rollback_segments', 'old_value': 64, 'new_value': 128}, {'knob': 'low_priority_updates', 'old_value': 'OFF', 'new_value': 'OFF'}, {'knob': 'transaction_alloc_block_size', 'old_value': 66048, 'new_value': 8192}, {'knob': 'transaction_prealloc_size', 'old_value': 66048, 'new_value': 4096}, {'knob': 'innodb_buffer_pool_size', 'old_value': 22333829939, 'new_value': 22400000000}, {'knob': 'key_buffer_size', 'old_value': 8589934596, 'new_value': 8388608}, {'knob': 'bulk_insert_buffer_size', 'old_value': 41943040, 'new_value': 8388608}, {'knob': 'innodb_sort_buffer_size', 'old_value': 33587200, 'new_value': 1048576}, {'knob': 'innodb_change_buffer_max_size', 'old_value': 25, 'new_value': 25}, {'knob': 'preload_buffer_size', 'old_value': 536871424, 'new_value': 32768}, {'knob': 'innodb_log_write_ahead_size', 'old_value': 8448, 'new_value': 8192}, {'knob': 'max_connections', 'old_value': 50000, 'new_value': 1000}, {'knob': 'connect_timeout', 'old_value': 15768001, 'new_value': 10}, {'knob': 'net_read_timeout', 'old_value': 30, 'new_value': 25}, {'knob': 'net_write_timeout', 'old_value': 60, 'new_value': 60}, {'knob': 'back_log', 'old_value': 32768, 'new_value': 900}, {'knob': 'open_files_limit', 'old_value': 327675, 'new_value': 50000}, {'knob': 'binlog_cache_size', 'old_value': 2147485696, 'new_value': 32768}, {'knob': 'binlog_stmt_cache_size', 'old_value': 2147485696, 'new_value': 32768}, {'knob': 'range_alloc_block_size', 'old_value': 34816, 'new_value': 48992}, {'knob': 'innodb_adaptive_hash_index_parts', 'old_value': 256, 'new_value': 8}, {'knob': 'innodb_purge_batch_size', 'old_value': 2500, 'new_value': 1500}, {'knob': 'innodb_lru_scan_depth', 'old_value': 5170, 'new_value': 1024}, {'knob': 'innodb_max_dirty_pages_pct', 'old_value': 49, 'new_value': 75}, {'knob': 'innodb_read_ahead_threshold', 'old_value': 32, 'new_value': 12}, {'knob': 'innodb_use_fdatasync', 'old_value': 'ON', 'new_value': 'OFF'}, {'knob': 'innodb_random_read_ahead', 'old_value': 'OFF', 'new_value': 'ON'}, {'knob': 'innodb_sync_array_size', 'old_value': 512, 'new_value': 1}, {'knob': 'innodb_buffer_pool_instances', 'old_value': 32, 'new_value': 36}]

    change_fval =  {'old_fval': 1976.5971666666665, 'new_fval': 1197.0443333333335, 'change_fval': -779.552833333333}
    
    extract_rule(bottleneck_functions_and_knobs, rule_config_change, change_fval)
    
    