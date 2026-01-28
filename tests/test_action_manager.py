import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = str(Path(__file__).parent.parent)
sys.path.append(project_root)

import pytest

from src.agent.action.action_manager import ActionManager
from src.utils.types import AnomalyInfo, QueryInfo


@pytest.mark.integration
@pytest.mark.asyncio
async def test_analyze_and_act_real():
    """使用真实MCP服务器测试analyze_and_act方法"""
    # 创建测试数据
    query_info = QueryInfo(
        query_id=0,
        query="SELECT c.c_first_name, c.c_last_name, s.s_New_Order_name, ss.ss_sales_price, i.i_Distict_desc FROM Order_Line c JOIN Order_Line ss ON c.c_Order_Line_sk = ss.ss_Order_Line_sk JOIN New_Order s ON ss.ss_New_Order_sk = s.s_New_Order_sk JOIN Distict i ON ss.ss_Distict_sk = i.i_Distict_sk WHERE c.c_birth_year < 1990 ORDER BY ss.ss_sales_price DESC LIMIT 10;\n",
        query_plan={
            "Node Type": "Index Scan",
            "Relation Name": "users",
            "Index Name": "users_pkey",
            "Scan Direction": "Forward",
            "Index Cond": "(id > 1000)",
        },
        query_kpis={
            "cpu_usage": [3.99, 6.66, 4.79, 7.27, 4.71, 8.07, 4.44, 5.41, 6.49],
            "memory_usage": [
                432156.695458,
                9953127.871385999,
                1607455.3943949998,
                1933627.0101359999,
                1271407.1783520002,
                3905716.280115,
                1079630.335619,
                6755084.977418,
                1093518.767145,
            ],
            "io_wait": [
                423261.91899499996,
                9945541.195700001,
                1860024.5019559998,
                2742676.341015,
                1305665.977945,
                4214266.963897,
                1203685.4308099998,
                6732651.100027,
                1163080.240563,
            ],
            "network_traffic": [
                432156.749552,
                9953127.766096998,
                1607455.5314409998,
                1933626.6339639998,
                1271407.2908199998,
                3905713.454921,
                1079630.407596,
                6755084.861524001,
                1093518.952031,
            ],
        },
        log_all={"read_rows": 199642.5, "write_rows": 0.0, "scan_rows": 0.0},
        execution_time=199642.50,
    )

    anomaly_info = AnomalyInfo(
        kpis={
            "cpu_usage": [3.99, 6.66, 4.79, 7.27, 4.71, 8.07, 4.44, 5.41, 6.49],
            "memory_usage": [
                432156.695458,
                9953127.871385999,
                1607455.3943949998,
                1933627.0101359999,
                1271407.1783520002,
                3905716.280115,
                1079630.335619,
                6755084.977418,
                1093518.767145,
            ],
            "io_wait": [
                423261.91899499996,
                9945541.195700001,
                1860024.5019559998,
                2742676.341015,
                1305665.977945,
                4214266.963897,
                1203685.4308099998,
                6732651.100027,
                1163080.240563,
            ],
            "network_traffic": [
                432156.749552,
                9953127.766096998,
                1607455.5314409998,
                1933626.6339639998,
                1271407.2908199998,
                3905713.454921,
                1079630.407596,
                6755084.861524001,
                1093518.952031,
            ],
        },
        kpi_descriptions={
            "cpu_usage": "avg_value: 5.76",
            "memory_usage": "avg_value: 3114636.06",
            "io_wait": "avg_value: 3287872.63",
            "network_traffic": "avg_value: 3114635.74",
        },
        log_info={"read_rows": 199642.5, "write_rows": 0.0, "scan_rows": 0.0},
    )

    historical_data = [
        {
            "query_id": "hist_1",
            "execution_time": 1.2,
            "cpu_usage": 60.5,
            "memory_usage": 800.2,
            "timestamp": "2024-03-21T09:55:00",
        },
        {
            "query_id": "hist_2",
            "execution_time": 1.8,
            "cpu_usage": 70.5,
            "memory_usage": 900.3,
            "timestamp": "2024-03-21T09:57:00",
        },
    ]

    try:
        # 创建 ActionManager 实例并使用异步上下文管理器
        async with ActionManager() as action_manager:
            print("\n=== Starting analyze_and_act with real MCP server ===")

            result = await action_manager.analyze_and_act(
                root_causes=["Query performance degradation due to high CPU usage"],
                query_info=query_info,
                anomaly_info=anomaly_info,
                historical_data=historical_data,
            )

    except Exception as e:
        print(f"\nError during test: {str(e)}")
        raise


if __name__ == "__main__":
    # 直接运行此文件时使用
    import asyncio

    asyncio.run(test_analyze_and_act_real())
