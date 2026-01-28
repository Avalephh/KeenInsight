import decimal
import json
import os
from collections.abc import Mapping

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# 匹配慢SQL的执行结果
# 使用方法
# python tpch_match.py

DB_URL = os.getenv("TPCH_DB_URL", "postgresql://postgres:postgres@localhost:5432/tpch10G")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")


# 递归转换Row对象为dict
def row_to_dict(obj):
    if isinstance(obj, Mapping):
        return {k: row_to_dict(v) for k, v in obj.items()}
    elif hasattr(obj, "_mapping"):
        return {k: row_to_dict(v) for k, v in obj._mapping.items()}
    elif isinstance(obj, list):
        return [row_to_dict(i) for i in obj]
    elif isinstance(obj, tuple):
        return tuple(row_to_dict(i) for i in obj)
    else:
        return obj


def decimal_default(obj):
    if isinstance(obj, decimal.Decimal):
        return str(obj)
    raise TypeError


def compare_results(new_result, old_result):
    return json.dumps(new_result, ensure_ascii=False, indent=2, default=decimal_default) == json.dumps(old_result, ensure_ascii=False, indent=2, default=decimal_default)


def main(query_file, output_file):
    # 读取SQL
    with open(query_file, "r", encoding="utf-8") as f:
        query = f.read()
    # 执行SQL
    engine = create_engine(DB_URL)
    with engine.connect() as conn:
        try:
            result = conn.execute(text(query))
            rows = result.fetchall()
            new_result = row_to_dict(rows)
        except SQLAlchemyError as e:
            print(f"SQL 执行出错: {e}")
            return
    # 读取旧结果
    with open(output_file, "r", encoding="utf-8") as f:
        old_result = json.load(f)
    # 比对
    if compare_results(new_result, old_result):
        print("结果一致！")
    else:
        print("结果不一致！")
        print("新结果:")
        print(json.dumps(new_result, ensure_ascii=False, indent=2, default=decimal_default))
        print("旧结果:")
        print(json.dumps(old_result, ensure_ascii=False, indent=2, default=decimal_default))


if __name__ == "__main__":
    import sys

    main(sys.argv[1], sys.argv[2])
