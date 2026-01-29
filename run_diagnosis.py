#!/usr/bin/env python3
"""
Main script to generate diagnosis and tuning HTML pages from slow_query_list.json.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import sys
import os

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dream.agent.db_agent import DBAgent
from font_generate.generate_diagnosis import generate_diagnosis_html, generate_handling_html
from font_generate.generate_multi_tune import generate_multi_tune_html

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def main():
    """Main function to generate all HTML pages"""
    # Load config
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    if config_path is None:
        base_dir = Path(__file__).resolve().parent
        config_path = base_dir / "config" / "tpch_config.json"
        if not config_path.exists():
            config_path = base_dir / "config" / "tpch_config.json.example"
    
    logger.info(f"Using config: {config_path}")
    with open(config_path, 'r', encoding='utf-8') as f:
        configs = json.load(f)
    
    # Load slow query data
    json_path = Path(__file__).resolve().parent / "results" / "slow_query_list.json"
    logger.info(f"Loading slow queries from: {json_path}")
    with open(json_path, 'r', encoding='utf-8') as f:
        slow_query_data = json.load(f)
    
    logger.info(f"Found {len(slow_query_data)} slow queries")
    
    # Initialize agent
    logger.info("Initializing DREAM agent...")
    async with DBAgent(configs=configs) as agent:
        # Generate diagnosis.html for all queries
        logger.info("Generating diagnosis.html...")
        await generate_diagnosis_html(agent, slow_query_data)
        
        # Generate handling.html for each query
        for query_id in slow_query_data.keys():
            logger.info(f"Generating handling.html for query {query_id}...")
            try:
                await generate_handling_html(agent, query_id, slow_query_data)
                # Save with query_id in filename for multiple queries
                handling_path = Path(__file__).resolve().parent / "results" / f"handling_{query_id}.html"
                default_handling = Path(__file__).resolve().parent / "results" / "handling.html"
                if default_handling.exists():
                    import shutil
                    shutil.copy(default_handling, handling_path)
            except Exception as e:
                logger.error(f"Error generating handling.html for {query_id}: {e}", exc_info=True)
        
        # Generate multi-tune.html for first query (or can be customized)
        if slow_query_data:
            first_query_id = list(slow_query_data.keys())[0]
            logger.info(f"Generating multi-tune.html for query {first_query_id}...")
            try:
                await generate_multi_tune_html(agent, first_query_id, slow_query_data, max_rounds=5)
            except Exception as e:
                logger.error(f"Error generating multi-tune.html: {e}", exc_info=True)
    
    logger.info("All HTML pages generated successfully!")


if __name__ == '__main__':
    asyncio.run(main())
