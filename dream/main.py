import argparse
import asyncio
import json
import logging
import os
from datetime import datetime

from dream.agent.db_agent import DBAgent


# setup logging, output to console and file
def setup_logging():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(project_root, "logs")
    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"dream_diagnosis_{timestamp}.log")

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


def parse_args():
    parser = argparse.ArgumentParser(description="Slow Query Diagnosis Tool")
    parser.add_argument("--data_path", type=str, required=True, help="Slow query data file path")
    parser.add_argument("--order", type=str, required=True, help="Slow query execution order")
    parser.add_argument("--duration", type=int, default=30, help="Optimization duration in hours")
    parser.add_argument("--config", type=str, required=True, help="Configuration file path")

    return parser.parse_args()


async def main_async():
    """
    Main async function for running DREAM database diagnosis system.
    """
    logger = logging.getLogger(__name__)

    args = parse_args()
    logger.info("Starting DREAM Database Diagnosis System")
    logger.info(f"Data path: {args.data_path}")
    logger.info(f"Execution order: {args.order}")
    logger.info(f"Optimization duration: {args.duration} hours")
    logger.info(f"Configuration file: {args.config}")

    # Load JSON configuration file
    with open(args.config, "r", encoding="utf-8") as f:
        configs = json.load(f)

    async with DBAgent(configs=configs) as agent:
        await agent.run(
            slow_query_path=args.data_path,
            order=args.order,
            duration=args.duration,
            no_improvement_threshold=configs["AGENT_CONFIG"]["no_improvement_threshold"],
            epsilon=configs["AGENT_CONFIG"]["epsilon"],
        )


def main():
    """
    Main entry point for command-line interface.
    
    Usage:
        dream --data_path /path/to/data --order qorder.txt --duration 30 --config /path/to/config.json
    """
    try:
        asyncio.run(main_async())
    except asyncio.CancelledError:
        # anyio/MCP throws cancellation when normally closing stdio, which is a normal shutdown path
        pass


if __name__ == "__main__":
    main()
