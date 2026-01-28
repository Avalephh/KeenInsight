import argparse
import asyncio
import itertools
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dream.agent.db_agent import DBAgent
from dream.utils.types import QueryInfo

# Setup logging
def setup_logging():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(project_root, "logs")
    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"workload_root_cause_test_{timestamp}.log")

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    print(f"Log file saved to: {log_file}")
    return log_file


setup_logging()
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Test workload SQL performance with all root causes")
    parser.add_argument("--data_path", type=str, required=True, help="Slow query data file path")
    parser.add_argument("--order", type=str, required=True, help="Slow query execution order file")
    parser.add_argument("--config", type=str, required=True, help="Configuration file path")
    parser.add_argument("--rounds", type=int, default=1, help="Number of test rounds (default: 1)")
    parser.add_argument("--output", type=str, default=None, help="Output JSON file path for results")
    
    return parser.parse_args()


def generate_all_root_cause_combinations():
    """Generate all possible root cause combinations"""
    root_cause_types = [
        "missing indexes",
        "inappropriate query knobs",
        "suboptimal plan optimizer",
        "poorly written queries",
    ]
    
    combinations = []
    # Generate all combinations from 1 to len(root_cause_types)
    for i in range(1, len(root_cause_types) + 1):
        for combo in itertools.combinations(root_cause_types, i):
            combinations.append(list(combo))
    
    return combinations


def read_queries(slow_query_path, order):
    """Read queries from order file"""
    queries_order = []
    order_file = os.path.join(slow_query_path, order)
    if not os.path.exists(order_file):
        logger.warning(f"Order file {order_file} not found, reading all SQL files in directory")
        # If order file doesn't exist, read all SQL files
        for fname in sorted(os.listdir(slow_query_path)):
            if fname.endswith(".sql"):
                queries_order.append(os.path.join(slow_query_path, fname))
    else:
        with open(order_file, "r", encoding="utf-8") as f:
            for fname in f:
                fname = fname.strip()
                if fname.endswith(".sql"):
                    queries_order.append(os.path.join(slow_query_path, fname))
    return queries_order


def clean_query(sql):
    """Clean SQL query"""
    sql = re.sub(r"--.*?(\n|$)", "", sql)
    sql = sql.strip()
    sql = re.sub(r"\s+", " ", sql)
    return sql


class WorkloadRootCauseTester:
    def __init__(self, configs):
        self.configs = configs
        self.results = []
        
    async def test_single_combination(self, agent, query_path, root_causes, round_num):
        """Test a single SQL with a specific root cause combination"""
        query_id_str = query_path.split("/")[-1].replace(".sql", "")
        # Try to convert to int if it's a numeric string, otherwise keep as string
        # But QueryInfo expects int, so we'll try conversion
        try:
            query_id = int(query_id_str)
        except ValueError:
            # If not numeric, use hash or 0 as fallback
            logger.warning(f"query_id '{query_id_str}' is not numeric, using 0 as fallback")
            query_id = 0
        
        # Read SQL
        query = open(query_path, "r", encoding="utf-8").read()
        query = clean_query(query)
        
        logger.info(f"Testing SQL {query_id_str} with root causes {root_causes} (Round {round_num})")
        
        # Collect query execution information
        try:
            result = agent.db.run_sql_and_collect_all(query)
        except Exception as e:
            logger.error(f"SQL {query_id_str} error collecting SQL execution data: {e}", exc_info=True)
            database_config = self.configs.get("DATABASE_CONFIG", {})
            timeout_time = database_config.get("query_timeout", 300)
            return {
                "query_id": query_id_str,
                "root_causes": root_causes,
                "round": round_num,
                "original_time": timeout_time,
                "new_time": timeout_time,
                "improvement": 0.0,
                "improvement_ratio": 0.0,
                "status": -1,
                "error": str(e),
                "fix_action": "",
                "rewrite_sql": "",
            }
        
        original_time = result["duration"]
        logger.info(f"SQL {query_id_str} original execution time: {original_time}s")
        
        # Build QueryInfo
        query_info = QueryInfo(
            query_id=query_id,
            query=query,
            plan_json=result["plan_json"],
            internal_metrics=result["internal_metrics"],
            external_metrics=result["external_metrics"],
            execution_time=result["duration"],
            is_rewrite=False,
        )
        
        # Call action_manager.step
        try:
            evaluation_result = await agent.action_manager.step(
                query_info, root_causes, mode="exploit"
            )
        except Exception as e:
            logger.error(f"SQL {query_id_str} error during step execution: {e}", exc_info=True)
            return {
                "query_id": query_id_str,
                "root_causes": root_causes,
                "round": round_num,
                "original_time": original_time,
                "new_time": original_time,
                "improvement": 0.0,
                "improvement_ratio": 0.0,
                "status": -1,
                "error": str(e),
                "fix_action": "",
                "rewrite_sql": "",
            }
        
        # Extract results
        status = evaluation_result.get("status", 0)
        new_time = evaluation_result.get("new_time", original_time)
        fix_action = evaluation_result.get("fix_action", "")
        rewrite_sql = evaluation_result.get("rewrite_sql", "")
        
        # Rollback actions to ensure test independence
        if fix_action:
            index_names = agent.action_manager.extract_index_names(fix_action)
            knob_names = agent.action_manager.extract_knob_names(fix_action)
            if index_names or knob_names:
                agent.action_manager.rollback_action(index_names, knob_names)
        
        # Calculate improvement
        improvement = original_time - new_time
        improvement_ratio = (improvement / original_time * 100) if original_time > 0 else 0.0
        
        result_data = {
            "query_id": query_id_str,
            "root_causes": root_causes,
            "round": round_num,
            "original_time": original_time,
            "new_time": new_time,
            "improvement": improvement,
            "improvement_ratio": improvement_ratio,
            "status": status,
            "fix_action": fix_action,
            "rewrite_sql": rewrite_sql,
        }
        
        logger.info(f"SQL {query_id_str} root causes {root_causes}: {original_time}s -> {new_time}s "
                   f"(improvement: {improvement:.4f}s, {improvement_ratio:.2f}%)")
        
        return result_data
    
    async def run_tests(self, slow_query_path, order, rounds):
        """Run tests for all SQLs with all root cause combinations"""
        # Read queries
        queries_order = read_queries(slow_query_path, order)
        logger.info(f"Found {len(queries_order)} SQL files to test")
        
        # Generate all root cause combinations
        root_cause_combinations = generate_all_root_cause_combinations()
        logger.info(f"Testing {len(root_cause_combinations)} root cause combinations")
        logger.info(f"Root cause combinations: {root_cause_combinations}")
        
        total_tests = len(queries_order) * len(root_cause_combinations) * rounds
        logger.info(f"Total tests to run: {total_tests}")
        
        async with DBAgent(configs=self.configs) as agent:
            test_count = 0
            for round_num in range(1, rounds + 1):
                logger.info(f"=== Round {round_num}/{rounds} ===")
                
                for query_path in queries_order:
                    for root_causes in root_cause_combinations:
                        test_count += 1
                        logger.info(f"Progress: {test_count}/{total_tests}")
                        
                        result = await self.test_single_combination(
                            agent, query_path, root_causes, round_num
                        )
                        self.results.append(result)
        
        return self.results


def save_results(results, output_path):
    """Save test results to JSON file"""
    if output_path is None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        output_dir = os.path.join(project_root, "results")
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(output_dir, f"workload_root_cause_results_{timestamp}.json")
    
    # Create summary statistics
    summary = {
        "total_tests": len(results),
        "successful_tests": len([r for r in results if r["status"] == 1]),
        "failed_tests": len([r for r in results if r["status"] == -1]),
        "ineffective_tests": len([r for r in results if r["status"] == 0]),
        "total_improvement": sum([r["improvement"] for r in results]),
        "avg_improvement_ratio": sum([r["improvement_ratio"] for r in results]) / len(results) if results else 0,
        "max_improvement": max([r["improvement"] for r in results]) if results else 0,
        "max_improvement_ratio": max([r["improvement_ratio"] for r in results]) if results else 0,
    }
    
    output_data = {
        "summary": summary,
        "results": results,
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)
    
    logger.info(f"Results saved to: {output_path}")
    logger.info(f"Summary: {summary}")
    
    return output_path


async def main():
    args = parse_args()
    logger.info("Starting Workload Root Cause Testing")
    logger.info(f"Data path: {args.data_path}")
    logger.info(f"Execution order: {args.order}")
    logger.info(f"Configuration file: {args.config}")
    logger.info(f"Test rounds: {args.rounds}")
    
    # Load JSON configuration file
    with open(args.config, "r", encoding="utf-8") as f:
        configs = json.load(f)
    
    # Create tester
    tester = WorkloadRootCauseTester(configs)
    
    # Run tests
    results = await tester.run_tests(args.data_path, args.order, args.rounds)
    
    # Save results
    output_path = save_results(results, args.output)
    
    logger.info("Testing completed")
    logger.info(f"Results saved to: {output_path}")

# cd DREAM/src
# python test_workload_root_causes.py --data_path /root/DREAM/data --order qorder.txt --config /root/DREAM/config/config.json --rounds 3 --output /root/DREAM/results/workload_root_cause_results.json

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except asyncio.CancelledError:
        pass