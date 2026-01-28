import argparse
import csv
import os
import re

# 使用方法
# python extract_sql.py --mode sql_to_files --input tpch.sql --output missing_indexes_sql_files
# python extract_sql.py --mode txt_to_files --input tpch10000.txt --output pool_queries_sql_files
# python extract_sql.py --mode files_to_csv --input sql_files --output tpch_queries.csv
# python extract_sql.py --mode csv_to_files --input tpch_queries.csv --output sql_files
# python extract_sql.py --mode csv_to_files --input pos_pool_tpch_cleaned.csv --output /root/DREAM/test_sql_gen/slow_queries/pool_queries_sql_files2
# python extract_sql.py --mode csv_to_files --input pos_pool_tpch_cleaned.csv --output /root/DREAM/test_sql_gen/slow_queries/pool_queries_sql_files2_rewritten


def extract_sql_to_sql_files(input_sql_path, output_dir):
    """每行一条SQL，分割为单独SQL文件"""
    os.makedirs(output_dir, exist_ok=True)
    with open(input_sql_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    count = 0
    for idx, line in enumerate(lines):
        sql = line.strip()
        if not sql:
            continue
        file_name = f"sql_{idx+1}.sql"
        file_path = os.path.join(output_dir, file_name)
        with open(file_path, "w", encoding="utf-8") as sql_file:
            sql_file.write(sql + "\n")
        count += 1
    print(f"共提取 {count} 条 SQL 语句，已保存到：{output_dir}")


def extract_txt_to_sql_files(input_sql_path, output_dir):
    """按分号分割SQL，分割为单独SQL文件"""
    os.makedirs(output_dir, exist_ok=True)
    with open(input_sql_path, "r", encoding="utf-8") as f:
        content = f.read()
    statements = [stmt.strip() for stmt in content.split(";") if stmt.strip()]
    for idx, sql in enumerate(statements):
        file_name = f"sql_{idx+1}.sql"
        file_path = os.path.join(output_dir, file_name)
        with open(file_path, "w", encoding="utf-8") as sql_file:
            sql_file.write(sql + ";\n")
    print(f"共提取 {len(statements)} 条 SQL 语句，已保存到：{output_dir}")


def extract_sql_files_to_csv(sql_dir, output_csv):
    """将目录下所有.sql文件合并为csv，每行为一条SQL"""
    count = 0
    with open(output_csv, "w", encoding="utf-8") as out_f:
        out_f.write("query\n")
        for fname in sorted(os.listdir(sql_dir)):
            if fname.endswith(".sql"):
                fpath = os.path.join(sql_dir, fname)
                with open(fpath, "r", encoding="utf-8") as sql_f:
                    sql = sql_f.read().strip().replace("\n", " ")
                    out_f.write(f'"{sql}"\n')
                    count += 1
    print(f"已提取所有SQL到: {output_csv}")
    print(f"已提取SQL数量: {count}")


def extract_csv_to_sql_files(input_csv, output_dir):
    """将csv中的'sql'列批量导出为SQL文件"""
    os.makedirs(output_dir, exist_ok=True)
    with open(input_csv, "r", encoding="utf-8") as csv_file:
        csv_reader = csv.DictReader(csv_file)
        for i, row in enumerate(csv_reader):
            sql = row.get("rewritten_sql", "").strip()
            if not sql:
                continue
            sql = re.sub(r'^"|"$', "", sql)
            if not sql.strip().endswith(";"):
                sql = sql.strip() + ";"
            file_name = f"query_{i+1}.sql"
            file_path = os.path.join(output_dir, file_name)
            with open(file_path, "w", encoding="utf-8") as sql_file:
                sql_file.write(sql + "\n")
    print(f"已将SQL语句提取到{output_dir}目录中")


def main():
    parser = argparse.ArgumentParser(description="SQL批量处理工具")
    parser.add_argument(
        "--mode",
        type=str,
        required=True,
        choices=["sql_to_files", "txt_to_files", "files_to_csv", "csv_to_files"],
        help="操作模式",
    )
    parser.add_argument("--input", type=str, required=True, help="输入文件/目录")
    parser.add_argument("--output", type=str, required=True, help="输出文件/目录")
    args = parser.parse_args()

    if args.mode == "sql_to_files":
        extract_sql_to_sql_files(args.input, args.output)
    elif args.mode == "txt_to_files":
        extract_txt_to_sql_files(args.input, args.output)
    elif args.mode == "files_to_csv":
        extract_sql_files_to_csv(args.input, args.output)
    elif args.mode == "csv_to_files":
        extract_csv_to_sql_files(args.input, args.output)
    else:
        print("未知模式")


if __name__ == "__main__":
    main()
