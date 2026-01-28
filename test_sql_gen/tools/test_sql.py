#!/usr/bin/env python3
import argparse
import glob
import os
import sys
import time

import psycopg2
from psycopg2 import errors

# 检验SQL能否执行
# 使用方法
# python test_sql.py --dir_path ./missing_indexes_sql_files
# python test_sql.py --origin_dir ./slow_queries/suboptimizer_plan_sql_files --rewritten_dir ./slow_queries/suboptimizer_plan_sql_files_rewritten


def test_sql_files(sql_dir):
    # 连接到PostgreSQL数据库
    try:
        conn = psycopg2.connect("postgresql://postgres:postgres@localhost:5432/tpch100G")
        conn.autocommit = False  # 确保手动控制事务
        cursor = conn.cursor()
        print("成功连接到PostgreSQL数据库")
    except Exception as e:
        print(f"连接数据库失败: {e}")
        return

    # 获取所有SQL文件
    sql_files = sorted(glob.glob(f"{sql_dir}/*.sql"))

    # 测试每个SQL文件
    error_list = []
    empty_results = []
    success = 0
    long_long_queries = []  # 新增超时SQL列表
    long_queries = []  # 新增超时SQL列表
    long_queries_threshold = 5
    comment_files = []

    for sql_file in sql_files:
        # test_all_sql_files = ['sql_735.sql', 'sql_78.sql', 'sql_89.sql', 'sql_91.sql', 'sql_42.sql' , 'sql_44.sql', 'sql_448.sql', 'sql_462.sql', 'sql_463.sql', 'sql_476.sql', 'sql_499.sql', 'sql_508.sql', 'sql_51.sql', 'sql_513.sql', 'sql_520.sql', 'sql_526.sql', 'sql_527.sql', 'sql_531.sql', 'sql_532.sql', 'sql_538.sql', 'sql_540.sql', 'sql_545.sql', 'sql_554.sql', 'sql_577.sql', 'sql_579.sql', 'sql_593.sql', 'sql_612.sql', 'sql_619.sql', 'sql_622.sql', 'sql_63.sql', 'sql_637.sql', 'sql_641.sql', 'sql_644.sql', 'sql_646.sql', 'sql_650.sql', 'sql_693.sql', 'sql_694.sql', 'sql_695.sql', 'sql_696.sql', 'sql_698.sql', 'sql_705.sql', 'sql_716.sql', 'sql_717.sql', 'sql_718.sql', 'sql_721.sql', 'sql_722.sql', 'sql_729.sql']
        test_all_sql_files = [
            "q1.sql",
            "q2.sql",
            "q3.sql",
            "q4.sql",
            "q5.sql",
            "q6.sql",
            "q7.sql",
            "q8.sql",
            "q9.sql",
            "q10.sql",
            "q11.sql",
            "q12.sql",
            "q13.sql",
            "q14.sql",
            "q15.sql",
            "q16.sql",
            "q18.sql",
            "q19.sql",
            "q21.sql",
            "q22.sql",
        ]
        test_all_sql_files = [sql_dir + "/" + file for file in test_all_sql_files]

        if sql_file not in test_all_sql_files:
            continue
        else:
            print(f"开始执行{sql_file}")

        file_name = os.path.basename(sql_file)

        # 读取SQL文件内容
        with open(sql_file, "r") as f:
            sql = f.read()

        # 检查是否包含注释
        if "--" in sql or "/*" in sql:
            comment_files.append(file_name)
            print(f"🔍 {file_name} - 包含注释")

        try:
            # 设置statement_timeout为300秒（300000毫秒）
            cursor.execute("SET statement_timeout = 3000000;")
            # 记录开始时间
            start_time = time.time()
            print(f"开始执行{file_name}")

            # 每个查询使用单独的事务
            cursor.execute(sql)

            # 检查结果是否为空
            if cursor.description is not None:  # 如果是SELECT查询
                results = cursor.fetchall()

                # 计算执行时间
                end_time = time.time()
                execution_time = end_time - start_time

                if len(results) == 0:
                    empty_results.append(file_name)
                    print(f"⚠ {file_name} - 执行成功但结果为空 (耗时: {execution_time:.4f}秒)")
                else:
                    print(f"✓ {file_name} - 执行成功 (耗时: {execution_time:.4f}秒)")

                if execution_time > long_queries_threshold:
                    long_queries.append([file_name, execution_time])
                    print(f"⏰ {file_name} - 执行超时(>1秒)，标记为long query")
            else:
                # 计算执行时间
                end_time = time.time()
                execution_time = end_time - start_time

                print(f"✓ {file_name} - 执行成功 (耗时: {execution_time:.4f}秒)")

            conn.commit()  # 提交事务
            success += 1
        except errors.QueryCanceled as e:
            conn.rollback()
            long_long_queries.append(file_name)
            print(f"⏰ {file_name} - 执行超时(>300秒)，标记为long long query")
        except Exception as e:
            # 记录错误信息
            conn.rollback()  # 回滚事务
            error_list.append((file_name, str(e)))
            print(f"✗ {file_name} - 执行失败: {e}")

    # 关闭数据库连接
    cursor.close()
    conn.close()

    # 打印测试统计信息
    print("\n测试结果统计:")
    print(f"总共SQL文件: {len(sql_files)}")
    print(f"成功执行: {success}")
    print(f"执行失败: {len(error_list)}")
    print(f"结果为空: {len(empty_results)}")
    print(f"超时(long long query): {len(long_long_queries)}")
    print(f"超时(long query): {len(long_queries)}")

    # 打印详细错误信息
    if error_list:
        print("\n执行失败的SQL文件及错误信息:")
        for file_name, error in error_list:
            print(f"\n文件: {file_name}")
            print(f"错误: {error}")

    # 打印结果为空的SQL文件
    if empty_results:
        print("\n结果为空的SQL文件:")
        for file_name in empty_results:
            print(f"- {file_name}")
    # 打印超时SQL文件
    if long_long_queries:
        print("\n超时(long long query)的SQL文件:")
        for file_name in long_long_queries:
            print(f"- {file_name}")
    if long_queries:
        print("\n超时(long query)的SQL文件:")
        for file_name, execution_time in long_queries:
            print(f"- {file_name} (耗时: {execution_time:.4f}秒)")

    if comment_files:
        print("\n包含注释的SQL文件:")
        for file_name in comment_files:
            print(f"- {file_name}")


def compare_sql_performance(origin_dir, rewritten_dir):
    try:
        conn = psycopg2.connect("postgresql://postgres:postgres@localhost:5432/tpch10G")
        conn.autocommit = False
        cursor = conn.cursor()
        print("成功连接到PostgreSQL数据库")
    except Exception as e:
        print(f"连接数据库失败: {e}")
        return

    origin_files = sorted(glob.glob(f"{origin_dir}/*.sql"))
    rewritten_files = sorted(glob.glob(f"{rewritten_dir}/*.sql"))
    rewritten_map = {os.path.basename(f): f for f in rewritten_files}

    results = []
    for origin_file in origin_files:
        file_name = os.path.basename(origin_file)
        rewritten_file = rewritten_map.get(file_name)
        if not rewritten_file:
            print(f"❌ 改写目录中未找到对应文件: {file_name}")
            continue

        # 读取SQL内容
        with open(origin_file, "r") as f:
            origin_sql = f.read()
        with open(rewritten_file, "r") as f:
            rewritten_sql = f.read()

        # 1. 执行清缓存SQL
        try:
            cursor.execute("SET statement_timeout = 120000;")
            print(f"\n🧹 清理缓存")
            cursor.execute("SELECT count(*) FROM generate_series(1, 100000000000);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"⚠️ 清理缓存超时: {e}")
        finally:
            cursor.execute("SET statement_timeout = 0;")  # 恢复无限制

        # 2. 执行原始SQL
        try:
            print(f"\n▶️ 执行原始SQL: {file_name}")
            start_time = time.time()
            cursor.execute(origin_sql)
            if cursor.description is not None:
                _ = cursor.fetchall()
            end_time = time.time()
            origin_time = end_time - start_time
            print(f"原始SQL耗时: {origin_time:.4f}秒")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"❌ 原始SQL执行失败: {e}")
            origin_time = None

        # 3. 执行清缓存SQL
        try:
            cursor.execute("SET statement_timeout = 120000;")
            print(f"\n🧹 清理缓存")
            cursor.execute("SELECT count(*) FROM generate_series(1, 100000000000);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"⚠️ 清理缓存超时: {e}")
        finally:
            cursor.execute("SET statement_timeout = 0;")  # 恢复无限制

        # 4. 执行改写SQL
        try:
            print(f"\n▶️ 执行改写SQL: {file_name}")
            start_time = time.time()
            cursor.execute(rewritten_sql)
            if cursor.description is not None:
                _ = cursor.fetchall()
            end_time = time.time()
            rewritten_time = end_time - start_time
            print(f"改写SQL耗时: {rewritten_time:.4f}秒")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"❌ 改写SQL执行失败: {e}")
            rewritten_time = None

        # 5. 结果统计
        if origin_time is not None and rewritten_time is not None:
            improve = origin_time - rewritten_time
            improve_percent = (improve / origin_time * 100) if origin_time > 0 else 0
            print(f"\n⏱️ {file_name} 性能提升: {improve:.4f}秒 ({improve_percent:.2f}%)")
            results.append((file_name, origin_time, rewritten_time, improve, improve_percent))
        else:
            print(f"\n⚠️ {file_name} 有SQL执行失败，无法比较性能")

    cursor.close()
    conn.close()

    # 汇总输出
    print("\n===== 性能对比汇总 =====")
    for file_name, origin_time, rewritten_time, improve, improve_percent in results:
        print(f"{file_name}: 原始={origin_time:.4f}s, 改写={rewritten_time:.4f}s, 提升={improve:.4f}s ({improve_percent:.2f}%)")
    print(f"\n共对比成功: {len(results)} 条SQL")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="测试SQL文件")
    parser.add_argument("--dir_path", type=str, help="SQL文件目录")
    parser.add_argument("--origin_dir", type=str, help="原始SQL目录")
    parser.add_argument("--rewritten_dir", type=str, help="改写SQL目录")
    args = parser.parse_args()

    if args.origin_dir and args.rewritten_dir:
        compare_sql_performance(args.origin_dir, args.rewritten_dir)
    elif args.dir_path:
        test_sql_files(args.dir_path)
    else:
        print("请指定 --dir_path 或 --origin_dir 和 --rewritten_dir 参数")
        sys.exit(1)
