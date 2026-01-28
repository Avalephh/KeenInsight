#!/usr/bin/env python3
"""
Simplified DBAgent that only uses LLM for diagnosis
without root cause prediction and exploration.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime

# Setup logging, output to console and file
def setup_logging():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(project_root, "logs")
    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"simple_diagnosis_{timestamp}.log")

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

from dream.agent.simple_db_agent import SimpleDBAgent


def parse_args():
    parser = argparse.ArgumentParser(description="Simplified Database Query Optimization Tool")
    parser.add_argument("--data_path", type=str, required=True, help="Path to slow query data files")
    parser.add_argument("--order", type=str, required=True, help="Query execution order file")
    parser.add_argument("--duration", type=int, default=1, help="Optimization duration in hours")
    parser.add_argument("--config", type=str, required=True, help="Configuration file path")
    parser.add_argument(
        "--no_improvement_threshold",
        type=int,
        default=3,
        help="Maximum attempts per SQL before giving up",
    )

    return parser.parse_args()


async def main():
    """
    Example usage:
    python simple.py --data_path /root/DREAM/data/slow_queries/TPC-H --order qorder.txt --duration 1 --config /root/DREAM/config/base_config.json
    """
    logger = logging.getLogger(__name__)

    args = parse_args()
    logger.info("Starting Simplified Database Optimization System")
    logger.info(f"Data path: {args.data_path}")
    logger.info(f"Execution order: {args.order}")
    logger.info(f"Optimization duration: {args.duration} hours")
    logger.info(f"Configuration file: {args.config}")
    logger.info(f"No improvement threshold: {args.no_improvement_threshold}")

    # Load JSON configuration file
    try:
        with open(args.config, "r", encoding="utf-8") as f:
            configs = json.load(f)
    except FileNotFoundError:
        logger.error(f"Configuration file {args.config} not found.")
        return
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing configuration file: {e}")
        return

    # Initialize the simplified agent
    async with SimpleDBAgent(configs) as agent:
        logger.info("Starting simplified database optimization...")

        # Run the optimization process
        await agent.run(
            slow_query_path=args.data_path,
            order=args.order,
            duration=args.duration,
            no_improvement_threshold=args.no_improvement_threshold,
        )

        logger.info("Simplified database optimization completed!")

# cd /root/DREAM/src
# python simple.py --data_path /root/DREAM/data/slow_queries/TPC-H --order qorder.txt --duration 30 --config /root/DREAM/config/tpch_config.json
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except asyncio.CancelledError:
        # Handle cancellation gracefully
        pass
    except KeyboardInterrupt:
        print("\nOptimization interrupted by user.")
    except Exception as e:
        print(f"Error during optimization: {e}")
        sys.exit(1)
