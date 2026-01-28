import argparse
import csv
import os
import shutil
from collections import defaultdict

# 使用方法
# python clean_sql.py --mode check_duplicate --input tpch_queries.csv
# python clean_sql.py --mode remove_duplicate --input tpch_queries.csv --output tpch_queries_dedup.csv
# python clean_sql.py --mode delete_files --dir_path sql_files
# python clean_sql.py --mode copy_files --dir_path /root/DREAM/test_sql_gen/slow_queries/pool_queries_sql_files --dst_dir /root/DREAM/test_sql_gen/slow_queries/pool_queries_sql_files1
# python clean_sql.py --mode filter_by_time_diff --input /root/DREAM/test_sql_gen/gen_slow_sql_tpch.csv --output /root/DREAM/test_sql_gen/gen_slow_sql_tpch_filtered.csv


def check_duplicate_queries(input_file):
    query_lines = defaultdict(list)
    with open(input_file, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        for idx, row in enumerate(reader, start=2):
            query = row[0].strip()
            query_lines[query].append(idx)
    has_duplicate = False
    for query, lines in query_lines.items():
        if len(lines) > 1:
            has_duplicate = True
            print(f"重复SQL出现在第{lines}行:")
            print(query)
            print("-" * 60)
    if not has_duplicate:
        print("没有发现重复的SQL语句。")


def remove_duplicate_queries(input_file, output_file):
    seen = set()
    rows = []
    with open(input_file, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows.append(header)
        for row in reader:
            query = row[0].strip()
            if query not in seen:
                seen.add(query)
                rows.append(row)
    with open(output_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    print(f"去重后已写入{len(rows)-1}条SQL到{output_file}")


def delete_sql_files(dir_path, files_to_delete):
    for filename in files_to_delete:
        file_path = os.path.join(dir_path, filename)
        try:
            os.remove(file_path)
            print(f"已删除: {file_path}")
        except FileNotFoundError:
            print(f"未找到: {file_path}")
        except Exception as e:
            print(f"删除 {file_path} 时出错: {e}")


def copy_sql_files(file_list, src_dir, dst_dir):
    if not os.path.exists(dst_dir):
        os.makedirs(dst_dir)
    for filename in file_list:
        src_path = os.path.join(src_dir, filename)
        dst_path = os.path.join(dst_dir, filename)
        try:
            shutil.copy2(src_path, dst_path)
            print(f"已复制: {src_path} -> {dst_path}")
        except FileNotFoundError:
            print(f"未找到: {src_path}")
        except Exception as e:
            print(f"复制 {src_path} 时出错: {e}")


def filter_by_time_diff(input_file, output_file, threshold=1.0):
    """
    过滤before_time和after_time差值小于threshold的行，写入output_file。
    """
    with open(input_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [row for row in reader if abs(float(row["before_time"]) - float(row["after_time"])) >= threshold]
        fieldnames = reader.fieldnames
    with open(output_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"过滤后剩余{len(rows)}条数据，已写入{output_file}")


def main():
    parser = argparse.ArgumentParser(description="SQL清洗工具：查重、去重、批量删除/复制SQL文件")
    parser.add_argument(
        "--mode",
        required=True,
        choices=[
            "check_duplicate",
            "remove_duplicate",
            "delete_files",
            "copy_files",
            "filter_by_time_diff",
        ],
        help="运行模式",
    )
    parser.add_argument("--input", type=str, help="输入CSV文件路径（查重/去重/过滤）")
    parser.add_argument("--output", type=str, help="输出CSV文件路径（去重/过滤）")
    parser.add_argument("--dir_path", type=str, help="SQL文件目录（批量删除源目录）")
    parser.add_argument("--dst_dir", type=str, help="SQL文件复制目标目录（复制模式）")
    parser.add_argument(
        "--file_list",
        type=str,
        help="要复制的SQL文件名列表txt文件，每行一个文件名（复制模式）",
    )
    args = parser.parse_args()

    if args.mode == "check_duplicate":
        if not args.input:
            print("请指定--input参数")
            return
        check_duplicate_queries(args.input)
    elif args.mode == "remove_duplicate":
        if not args.input or not args.output:
            print("请指定--input和--output参数")
            return
        remove_duplicate_queries(args.input, args.output)
    elif args.mode == "delete_files":
        files_to_delete = [
            "sql_1.sql",
            "sql_105.sql",
            "sql_109.sql",
            "sql_111.sql",
            "sql_113.sql",
            "sql_12.sql",
            "sql_124.sql",
            "sql_125.sql",
            "sql_13.sql",
            "sql_131.sql",
            "sql_137.sql",
            "sql_145.sql",
            "sql_146.sql",
            "sql_148.sql",
            "sql_149.sql",
            "sql_154.sql",
            "sql_158.sql",
            "sql_159.sql",
            "sql_165.sql",
            "sql_171.sql",
            "sql_173.sql",
            "sql_174.sql",
            "sql_176.sql",
            "sql_179.sql",
            "sql_181.sql",
            "sql_182.sql",
            "sql_190.sql",
            "sql_192.sql",
            "sql_193.sql",
            "sql_195.sql",
            "sql_196.sql",
            "sql_197.sql",
            "sql_199.sql",
            "sql_20.sql",
            "sql_200.sql",
            "sql_206.sql",
            "sql_212.sql",
            "sql_213.sql",
            "sql_214.sql",
            "sql_216.sql",
            "sql_219.sql",
            "sql_220.sql",
            "sql_231.sql",
            "sql_235.sql",
            "sql_237.sql",
            "sql_246.sql",
            "sql_247.sql",
            "sql_253.sql",
            "sql_254.sql",
            "sql_26.sql",
            "sql_261.sql",
            "sql_267.sql",
            "sql_268.sql",
            "sql_272.sql",
            "sql_275.sql",
            "sql_276.sql",
            "sql_279.sql",
            "sql_283.sql",
            "sql_288.sql",
            "sql_296.sql",
            "sql_30.sql",
            "sql_302.sql",
            "sql_303.sql",
            "sql_308.sql",
            "sql_310.sql",
            "sql_311.sql",
            "sql_312.sql",
            "sql_315.sql",
            "sql_316.sql",
            "sql_322.sql",
            "sql_323.sql",
            "sql_326.sql",
            "sql_327.sql",
            "sql_329.sql",
            "sql_331.sql",
            "sql_334.sql",
            "sql_337.sql",
            "sql_338.sql",
            "sql_34.sql",
            "sql_345.sql",
            "sql_346.sql",
            "sql_347.sql",
            "sql_348.sql",
            "sql_35.sql",
            "sql_350.sql",
            "sql_352.sql",
            "sql_355.sql",
            "sql_359.sql",
            "sql_360.sql",
            "sql_364.sql",
            "sql_366.sql",
            "sql_367.sql",
            "sql_375.sql",
            "sql_379.sql",
            "sql_381.sql",
            "sql_382.sql",
            "sql_386.sql",
            "sql_392.sql",
            "sql_397.sql",
            "sql_399.sql",
            "sql_4.sql",
            "sql_400.sql",
            "sql_401.sql",
            "sql_403.sql",
            "sql_406.sql",
            "sql_41.sql",
            "sql_417.sql",
            "sql_418.sql",
            "sql_42.sql",
            "sql_420.sql",
            "sql_422.sql",
            "sql_424.sql",
            "sql_426.sql",
            "sql_431.sql",
            "sql_433.sql",
            "sql_437.sql",
            "sql_44.sql",
            "sql_440.sql",
            "sql_448.sql",
            "sql_450.sql",
            "sql_457.sql",
            "sql_459.sql",
            "sql_460.sql",
            "sql_462.sql",
            "sql_464.sql",
            "sql_466.sql",
            "sql_472.sql",
            "sql_474.sql",
            "sql_478.sql",
            "sql_479.sql",
            "sql_485.sql",
            "sql_488.sql",
            "sql_492.sql",
            "sql_493.sql",
            "sql_5.sql",
            "sql_500.sql",
            "sql_501.sql",
            "sql_502.sql",
            "sql_506.sql",
            "sql_508.sql",
            "sql_51.sql",
            "sql_516.sql",
            "sql_520.sql",
            "sql_522.sql",
            "sql_526.sql",
            "sql_528.sql",
            "sql_529.sql",
            "sql_53.sql",
            "sql_530.sql",
            "sql_531.sql",
            "sql_535.sql",
            "sql_536.sql",
            "sql_538.sql",
            "sql_540.sql",
            "sql_541.sql",
            "sql_543.sql",
            "sql_544.sql",
            "sql_545.sql",
            "sql_547.sql",
            "sql_55.sql",
            "sql_551.sql",
            "sql_554.sql",
            "sql_555.sql",
            "sql_568.sql",
            "sql_569.sql",
            "sql_570.sql",
            "sql_573.sql",
            "sql_580.sql",
            "sql_582.sql",
            "sql_584.sql",
            "sql_593.sql",
            "sql_595.sql",
            "sql_596.sql",
            "sql_597.sql",
            "sql_599.sql",
            "sql_6.sql",
            "sql_601.sql",
            "sql_604.sql",
            "sql_607.sql",
            "sql_611.sql",
            "sql_612.sql",
            "sql_614.sql",
            "sql_62.sql",
            "sql_621.sql",
            "sql_622.sql",
            "sql_625.sql",
            "sql_628.sql",
            "sql_63.sql",
            "sql_631.sql",
            "sql_634.sql",
            "sql_637.sql",
            "sql_638.sql",
            "sql_639.sql",
            "sql_641.sql",
            "sql_644.sql",
            "sql_646.sql",
            "sql_649.sql",
            "sql_650.sql",
            "sql_652.sql",
            "sql_662.sql",
            "sql_663.sql",
            "sql_669.sql",
            "sql_670.sql",
            "sql_673.sql",
            "sql_675.sql",
            "sql_68.sql",
            "sql_682.sql",
            "sql_683.sql",
            "sql_685.sql",
            "sql_686.sql",
            "sql_69.sql",
            "sql_690.sql",
            "sql_692.sql",
            "sql_693.sql",
            "sql_695.sql",
            "sql_696.sql",
            "sql_698.sql",
            "sql_700.sql",
            "sql_703.sql",
            "sql_705.sql",
            "sql_706.sql",
            "sql_71.sql",
            "sql_712.sql",
            "sql_715.sql",
            "sql_717.sql",
            "sql_718.sql",
            "sql_72.sql",
            "sql_725.sql",
            "sql_726.sql",
            "sql_729.sql",
            "sql_730.sql",
            "sql_735.sql",
            "sql_742.sql",
            "sql_75.sql",
            "sql_77.sql",
            "sql_78.sql",
            "sql_80.sql",
            "sql_82.sql",
            "sql_84.sql",
            "sql_85.sql",
            "sql_87.sql",
            "sql_88.sql",
            "sql_91.sql",
            "sql_98.sql",
        ]
        if not args.dir_path:
            print("请指定--dir_path参数")
            return
        delete_sql_files(args.dir_path, files_to_delete)
    elif args.mode == "copy_files":
        if not args.dir_path or not args.dst_dir:
            print("请指定--dir_path、--dst_dir参数")
            return
        file_list = [
            "sql_1020.sql",
            "sql_1030.sql",
            "sql_1042.sql",
            "sql_1075.sql",
            "sql_1140.sql",
            "sql_1242.sql",
            "sql_1328.sql",
            "sql_1381.sql",
            "sql_15.sql",
            "sql_1575.sql",
            "sql_1921.sql",
            "sql_2198.sql",
            "sql_2374.sql",
            "sql_2481.sql",
            "sql_2772.sql",
            "sql_2903.sql",
            "sql_3204.sql",
            "sql_3222.sql",
            "sql_3451.sql",
            "sql_3572.sql",
            "sql_392.sql",
            "sql_3949.sql",
            "sql_400.sql",
            "sql_401.sql",
            "sql_4013.sql",
            "sql_4092.sql",
            "sql_4170.sql",
            "sql_4437.sql",
            "sql_4581.sql",
            "sql_4974.sql",
            "sql_5676.sql",
            "sql_5756.sql",
            "sql_6067.sql",
            "sql_6086.sql",
            "sql_6215.sql",
            "sql_6380.sql",
            "sql_6724.sql",
            "sql_677.sql",
            "sql_6879.sql",
            "sql_6962.sql",
            "sql_6965.sql",
            "sql_7006.sql",
            "sql_714.sql",
            "sql_7519.sql",
            "sql_7915.sql",
            "sql_7917.sql",
            "sql_7925.sql",
            "sql_8198.sql",
            "sql_8226.sql",
            "sql_8283.sql",
            "sql_8355.sql",
            "sql_8892.sql",
            "sql_8963.sql",
            "sql_90.sql",
            "sql_9130.sql",
            "sql_9447.sql",
            "sql_9481.sql",
            "sql_9607.sql",
        ]
        copy_sql_files(file_list, args.dir_path, args.dst_dir)
    elif args.mode == "filter_by_time_diff":
        if not args.input or not args.output:
            print("请指定--input和--output参数")
            return
        filter_by_time_diff(args.input, args.output)
    else:
        print("未知模式")


if __name__ == "__main__":
    main()
