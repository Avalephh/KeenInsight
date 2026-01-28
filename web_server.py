#!/usr/bin/env python3
"""
Web server for DREAM diagnosis and tuning system.
Connects frontend HTML pages with backend DREAM diagnosis engine.
"""

import asyncio
import json
import logging
import os
import time
import threading
from pathlib import Path
from typing import Dict, List, Optional

import nest_asyncio
from flask import Flask, jsonify, render_template_string, request, send_from_directory
from flask_cors import CORS

# Apply nest_asyncio to handle nested event loops
nest_asyncio.apply()

# Import DREAM components
from dream.agent.db_agent import DBAgent
from dream.utils.types import QueryInfo

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Get absolute path to font directory
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'font')
# Don't use static_folder to avoid conflicts with our custom routes
app = Flask(__name__)
# Configure CORS to allow all origins for remote access
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# Global variables
DREAM_AGENT: Optional[DBAgent] = None
CONFIGS = None
SLOW_QUERY_DATA = None
# Multi-tune background tasks: {query_id: thread}
MULTI_TUNE_TASKS: Dict[str, threading.Thread] = {}
MULTI_TUNE_TASK_LOCK = threading.Lock()

# Storage file paths
DIAGNOSIS_STORAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'diagnosis_results.json')
TUNING_STORAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tuning_results.json')
MULTI_TUNE_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'multi_tune_progress.log')
MULTI_TUNE_STORAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'multi_tune_results.json')


def load_config(config_path: str = None):
    """Load DREAM configuration"""
    global CONFIGS
    if config_path is None:
        # Try to find config file
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'tpch_config.json')
        if not os.path.exists(config_path):
            config_path = os.path.join(os.path.dirname(__file__), 'config', 'tpch_config.json.example')
    
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            CONFIGS = json.load(f)
        logger.info(f"Loaded config from {config_path}")
    else:
        logger.error(f"Config file not found: {config_path}")
        raise FileNotFoundError(f"Config file not found: {config_path}")


def load_slow_query_list():
    """Load slow query list from JSON file"""
    global SLOW_QUERY_DATA
    json_path = os.path.join(os.path.dirname(__file__), 'slow_query_list.json')
    if os.path.exists(json_path):
        with open(json_path, 'r', encoding='utf-8') as f:
            SLOW_QUERY_DATA = json.load(f)
        logger.info(f"Loaded {len(SLOW_QUERY_DATA)} slow queries from {json_path}")
    else:
        logger.error(f"Slow query list not found: {json_path}")
        SLOW_QUERY_DATA = {}


def save_diagnosis_result(query_id: str, diagnosis_result: Dict):
    """Save diagnosis result to storage file"""
    try:
        # Load existing results
        if os.path.exists(DIAGNOSIS_STORAGE_FILE):
            with open(DIAGNOSIS_STORAGE_FILE, 'r', encoding='utf-8') as f:
                all_results = json.load(f)
        else:
            all_results = {}
        
        # Update with new result
        all_results[query_id] = diagnosis_result
        
        # Save back to file
        with open(DIAGNOSIS_STORAGE_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Saved diagnosis result for query {query_id}")
    except Exception as e:
        logger.error(f"Error saving diagnosis result for query {query_id}: {e}")


def load_diagnosis_result(query_id: str) -> Optional[Dict]:
    """Load diagnosis result from storage file"""
    try:
        if os.path.exists(DIAGNOSIS_STORAGE_FILE):
            with open(DIAGNOSIS_STORAGE_FILE, 'r', encoding='utf-8') as f:
                all_results = json.load(f)
                return all_results.get(query_id)
        return None
    except Exception as e:
        logger.error(f"Error loading diagnosis result for query {query_id}: {e}")
        return None


def save_tuning_result(query_id: str, tuning_result: Dict):
    """Save tuning result (fix_action, rewrite_sql) to storage file"""
    try:
        # Load existing results
        if os.path.exists(TUNING_STORAGE_FILE):
            with open(TUNING_STORAGE_FILE, 'r', encoding='utf-8') as f:
                all_results = json.load(f)
        else:
            all_results = {}
        
        # Update with new result
        all_results[query_id] = tuning_result
        
        # Save back to file
        with open(TUNING_STORAGE_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Saved tuning result for query {query_id}")
    except Exception as e:
        logger.error(f"Error saving tuning result for query {query_id}: {e}")


def load_tuning_result(query_id: str) -> Optional[Dict]:
    """Load tuning result from storage file"""
    try:
        if os.path.exists(TUNING_STORAGE_FILE):
            with open(TUNING_STORAGE_FILE, 'r', encoding='utf-8') as f:
                all_results = json.load(f)
                return all_results.get(query_id)
        return None
    except Exception as e:
        logger.error(f"Error loading tuning result for query {query_id}: {e}")
        return None


async def initialize_dream_agent():
    """Initialize DREAM agent"""
    global DREAM_AGENT
    if CONFIGS is None:
        load_config()
    
    if DREAM_AGENT is None:
        DREAM_AGENT = DBAgent(configs=CONFIGS)
        await DREAM_AGENT.__aenter__()
        logger.info("DREAM agent initialized")


@app.after_request
def after_request(response):
    """Add CORS headers to all responses"""
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    # Only set Content-Type if it's not already set (e.g., by jsonify or mimetype)
    # Don't override Content-Type for JSON responses
    if 'Content-Type' not in response.headers:
        # Only set for HTML responses
        if response.content_type and 'text/html' in response.content_type:
            response.headers.add('Content-Type', 'text/html; charset=utf-8')
        elif response.content_type is None:
            # If content_type is None, check if it's likely HTML
            if hasattr(response, 'data') and isinstance(response.data, bytes):
                try:
                    content = response.data.decode('utf-8')
                    if content.strip().startswith('<!DOCTYPE') or content.strip().startswith('<html'):
                        response.headers.add('Content-Type', 'text/html; charset=utf-8')
                except:
                    pass
    return response


@app.route('/')
def index():
    """Redirect to diagnosis page"""
    try:
        diagnosis_path = os.path.join(FONT_DIR, 'diagnosis.html')
        logger.info(f"Serving diagnosis.html from {diagnosis_path}")
        if not os.path.exists(diagnosis_path):
            logger.error(f"diagnosis.html not found at {diagnosis_path}")
            return f"Error: diagnosis.html not found at {diagnosis_path}", 404
        if not os.access(diagnosis_path, os.R_OK):
            logger.error(f"diagnosis.html is not readable at {diagnosis_path}")
            return f"Error: diagnosis.html is not readable at {diagnosis_path}", 403
        with open(diagnosis_path, 'r', encoding='utf-8') as f:
            content = f.read()
            response = app.response_class(
                response=content,
                status=200,
                mimetype='text/html'
            )
            logger.info(f"Successfully served diagnosis.html, size: {len(content)} bytes")
            return response
    except PermissionError as e:
        logger.error(f"Permission denied reading diagnosis.html: {e}")
        return f"Error: Permission denied - {str(e)}", 403
    except Exception as e:
        logger.error(f"Error reading diagnosis.html: {e}", exc_info=True)
        return f"Error: {str(e)}", 500


@app.route('/diagnosis.html')
def diagnosis_page():
    """Serve diagnosis page"""
    try:
        diagnosis_path = os.path.join(FONT_DIR, 'diagnosis.html')
        with open(diagnosis_path, 'r', encoding='utf-8') as f:
            content = f.read()
            response = app.response_class(
                response=content,
                status=200,
                mimetype='text/html'
            )
            return response
    except Exception as e:
        logger.error(f"Error reading diagnosis.html: {e}")
        return f"Error: {str(e)}", 500


@app.route('/handling.html')
def handling_page():
    """Serve handling page"""
    try:
        handling_path = os.path.join(FONT_DIR, 'handling.html')
        with open(handling_path, 'r', encoding='utf-8') as f:
            content = f.read()
            response = app.response_class(
                response=content,
                status=200,
                mimetype='text/html'
            )
            return response
    except Exception as e:
        logger.error(f"Error reading handling.html: {e}")
        return f"Error: {str(e)}", 500


@app.route('/multi-tune.html')
def multi_tune_page():
    """Serve multi-tune page"""
    try:
        multi_tune_path = os.path.join(FONT_DIR, 'multi-tune.html')
        with open(multi_tune_path, 'r', encoding='utf-8') as f:
            content = f.read()
            response = app.response_class(
                response=content,
                status=200,
                mimetype='text/html'
            )
            return response
    except Exception as e:
        logger.error(f"Error reading multi-tune.html: {e}")
        return f"Error: {str(e)}", 500


@app.route('/api/slow-queries', methods=['GET'])
def get_slow_queries():
    """Get list of slow queries"""
    if SLOW_QUERY_DATA is None:
        load_slow_query_list()
    
    return jsonify(SLOW_QUERY_DATA)


def run_async(coro):
    """Helper to run async functions in Flask"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@app.route('/api/diagnose/<query_id>', methods=['POST'])
def diagnose_query(query_id: str):
    """Diagnose a specific query"""
    try:
        if SLOW_QUERY_DATA is None:
            load_slow_query_list()
        
        if query_id not in SLOW_QUERY_DATA:
            return jsonify({'error': f'Query {query_id} not found'}), 404
        
        query_data = SLOW_QUERY_DATA[query_id]['query_info']
        
        async def _diagnose():
            # Initialize agent if needed
            if DREAM_AGENT is None:
                await initialize_dream_agent()
            
            # Build QueryInfo
            query_info = QueryInfo(
                query_id=query_data['query_id'],
                query=query_data['query'],
                plan_json=query_data['plan_json'],
                external_metrics=query_data['external_metrics'],
                internal_metrics=query_data['internal_metrics'],
                execution_time=query_data['execution_time'],
                is_rewrite=query_data.get('is_rewrite', False)
            )
            
            # Run diagnosis
            state = {
                "root_tried": set(),
                "current_root": None,
                "mode": "exploit",
                "confidence": {},
                "attempts": {},
                "successes": {},
                "component_attempts": {},
            }
            
            predicted_root, updated_state, explanation = await DREAM_AGENT.planner.predict(
                query_info, state, DREAM_AGENT.memory_manager
            )
            
            # Get diagnosis results
            root_causes = predicted_root if isinstance(predicted_root, list) else [predicted_root]
            final_confidence = updated_state.get('confidence', {})
            
            # Ensure all root cause types are included with default confidence 0.0
            all_root_cause_types = [
                "missing indexes",
                "inappropriate query knobs", 
                "suboptimal plan optimizer",
                "poorly written queries"
            ]
            for rc_type in all_root_cause_types:
                if rc_type not in final_confidence:
                    final_confidence[rc_type] = 0.0
            
            # Ensure explanation is a string
            if not isinstance(explanation, str):
                explanation = str(explanation) if explanation else '暂无解释说明'
            
            # Format diagnosis results
            diagnosis_results = {
                'query_id': query_id,
                'root_causes': root_causes,
                'confidence': final_confidence,
                'explanation': explanation,
                'query_info': {
                    'query': query_info.query,
                    'execution_time': query_info.execution_time,
                    'plan_json': query_info.plan_json,
                    'internal_metrics': query_info.internal_metrics,
                    'external_metrics': query_info.external_metrics,
                }
            }
            
            # Save diagnosis result to storage
            save_diagnosis_result(query_id, diagnosis_results)
            
            return diagnosis_results
        
        diagnosis_results = run_async(_diagnose())
        return jsonify(diagnosis_results)
    
    except Exception as e:
        logger.error(f"Error diagnosing query {query_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/diagnose-all', methods=['POST'])
def diagnose_all_queries():
    """Diagnose all queries"""
    try:
        if SLOW_QUERY_DATA is None:
            load_slow_query_list()
        
        async def _diagnose_all():
            # Initialize agent if needed
            if DREAM_AGENT is None:
                await initialize_dream_agent()
            
            results = {}
            
            for query_id, query_data in SLOW_QUERY_DATA.items():
                try:
                    query_info_data = query_data['query_info']
                    
                    # Build QueryInfo
                    query_info = QueryInfo(
                        query_id=query_info_data['query_id'],
                        query=query_info_data['query'],
                        plan_json=query_info_data['plan_json'],
                        external_metrics=query_info_data['external_metrics'],
                        internal_metrics=query_info_data['internal_metrics'],
                        execution_time=query_info_data['execution_time'],
                        is_rewrite=query_info_data.get('is_rewrite', False)
                    )
                    
                    # Run diagnosis
                    state = {
                        "root_tried": set(),
                        "current_root": None,
                        "mode": "exploit",
                        "confidence": {},
                        "attempts": {},
                        "successes": {},
                        "component_attempts": {},
                    }
                    
                    predicted_root, updated_state, explanation = await DREAM_AGENT.planner.predict(
                        query_info, state, DREAM_AGENT.memory_manager
                    )
                    
                    root_causes = predicted_root if isinstance(predicted_root, list) else [predicted_root]
                    final_confidence = updated_state.get('confidence', {})
                    
                    # Ensure all root cause types are included with default confidence 0.0
                    all_root_cause_types = [
                        "missing indexes",
                        "inappropriate query knobs", 
                        "suboptimal plan optimizer",
                        "poorly written queries"
                    ]
                    for rc_type in all_root_cause_types:
                        if rc_type not in final_confidence:
                            final_confidence[rc_type] = 0.0
                    
                    # Ensure explanation is a string
                    if not isinstance(explanation, str):
                        explanation = str(explanation) if explanation else '暂无解释说明'
                    
                    diagnosis_result = {
                        'query_id': query_id,
                        'root_causes': root_causes,
                        'confidence': final_confidence,
                        'explanation': explanation,
                        'query_info': {
                            'query': query_info.query,
                            'execution_time': query_info.execution_time,
                            'plan_json': query_info.plan_json,
                            'internal_metrics': query_info.internal_metrics,
                            'external_metrics': query_info.external_metrics,
                        }
                    }
                    
                    # Save diagnosis result to storage
                    save_diagnosis_result(query_id, diagnosis_result)
                    
                    results[query_id] = {
                        'root_causes': root_causes,
                        'confidence': final_confidence,
                        'explanation': explanation,
                    }
                except Exception as e:
                    logger.error(f"Error diagnosing query {query_id}: {e}", exc_info=True)
                    results[query_id] = {
                        'error': str(e),
                        'root_causes': [],
                        'confidence': {},
                        'explanation': '诊断过程中出现错误，请稍后重试。'
                    }
            
            return {'results': results, 'total': len(results)}
        
        result = run_async(_diagnose_all())
        return jsonify(result)
    
    except Exception as e:
        logger.error(f"Error in diagnose_all_queries: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/tune/<query_id>', methods=['POST'])
def tune_query(query_id: str):
    """Generate tuning suggestions for a query and save to handling.html (without executing)"""
    try:
        if SLOW_QUERY_DATA is None:
            load_slow_query_list()
        
        if query_id not in SLOW_QUERY_DATA:
            return jsonify({'error': f'Query {query_id} not found'}), 404
        
        async def _tune():
            query_data = SLOW_QUERY_DATA[query_id]['query_info']
            
            # Initialize agent if needed
            if DREAM_AGENT is None:
                await initialize_dream_agent()
            
            # Load diagnosis result from storage (must exist)
            diagnosis_result = load_diagnosis_result(query_id)
            
            if not diagnosis_result:
                return {
                    'error': f'No diagnosis result found for query {query_id}. Please run diagnosis first.',
                    'query_id': query_id
                }
            
            # Use saved diagnosis result
            root_causes = diagnosis_result.get('root_causes', [])
            state_confidence = diagnosis_result.get('confidence', {})
            logger.info(f"Loaded diagnosis result for query {query_id} from storage")
            
            # Build QueryInfo for tuning
            query_info = QueryInfo(
                query_id=query_data['query_id'],
                query=query_data['query'],
                plan_json=query_data['plan_json'],
                external_metrics=query_data['external_metrics'],
                internal_metrics=query_data['internal_metrics'],
                execution_time=query_data['execution_time'],
                is_rewrite=query_data.get('is_rewrite', False)
            )
            
            # Generate tuning actions (only generate, don't execute)
            if not DREAM_AGENT.action_manager.agent:
                await DREAM_AGENT.action_manager.initialize()
            
            from dream.agent.action import diagnose_tools
            database_config = DREAM_AGENT.action_manager.configs.get("DATABASE_CONFIG")
            base_info = diagnose_tools.base_information_collect(query_info, database_config)
            
            # Retrieve cases
            if DREAM_AGENT.action_manager.enable_retrieval:
                retrieval = DREAM_AGENT.memory_manager.retrieve_cases(query_info, root_causes, mode="exploit")
                positives = retrieval.get("positive", [])
                negatives = retrieval.get("negative", [])
            else:
                positives = []
                negatives = []
            
            action_space = await DREAM_AGENT.action_manager.action_space_collect(root_causes, query_info)
            action_result = await DREAM_AGENT.action_manager.action_generate(
                root_causes, base_info, action_space, "exploit", positives, negatives
            )
            
            # Extract fix action and rewrite sql (without executing)
            fix_action, rewrite_sql = DREAM_AGENT.action_manager.extract_fix_action(action_result)
            
            # Save tuning result (fix_action and rewrite_sql) to storage
            tuning_result = {
                'query_id': query_id,
                'fix_action': fix_action,
                'rewrite_sql': rewrite_sql,
                'root_causes': root_causes,
                'state_confidence': state_confidence
            }
            save_tuning_result(query_id, tuning_result)
            
            # Parse actions into categories
            tuning_suggestions = parse_tuning_actions(fix_action, rewrite_sql, root_causes)
            
            # Generate handling.html using saved diagnosis result if available
            from generate_diagnosis import generate_handling_html
            await generate_handling_html(DREAM_AGENT, query_id, SLOW_QUERY_DATA, fix_action, rewrite_sql, root_causes, state_confidence)
            
            return {
                'query_id': query_id,
                'tuning_suggestions': tuning_suggestions,
                'fix_action': fix_action,
                'rewrite_sql': rewrite_sql,
                'handling_html_generated': True
            }
        
        result = run_async(_tune())
        return jsonify(result)
    
    except Exception as e:
        logger.error(f"Error tuning query {query_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/execute-tune/<query_id>', methods=['POST'])
def execute_tune(query_id: str):
    """Execute tuning actions and return actual results"""
    try:
        if SLOW_QUERY_DATA is None:
            load_slow_query_list()
        
        if query_id not in SLOW_QUERY_DATA:
            return jsonify({'error': f'Query {query_id} not found'}), 404
        
        async def _execute():
            query_data = SLOW_QUERY_DATA[query_id]['query_info']
            
            # Initialize agent if needed
            if DREAM_AGENT is None:
                await initialize_dream_agent()
            
            # Load tuning result from storage
            tuning_result = load_tuning_result(query_id)
            
            if not tuning_result:
                return {
                    'error': f'No tuning result found for query {query_id}. Please generate tuning suggestions first.',
                    'query_id': query_id
                }
            
            fix_action = tuning_result.get('fix_action', '')
            rewrite_sql = tuning_result.get('rewrite_sql', '')
            
            if not fix_action and not rewrite_sql:
                return {
                    'error': f'No fix_action or rewrite_sql found for query {query_id}',
                    'query_id': query_id
                }
            
            # Build QueryInfo
            query_info = QueryInfo(
                query_id=query_data['query_id'],
                query=query_data['query'],
                plan_json=query_data['plan_json'],
                external_metrics=query_data['external_metrics'],
                internal_metrics=query_data['internal_metrics'],
                execution_time=query_data['execution_time'],
                is_rewrite=query_data.get('is_rewrite', False)
            )
            
            # Execute tuning actions directly using evaluate_action
            # This will execute fix_action in the database and test the query execution time
            evaluation_result = await DREAM_AGENT.action_manager.evaluate_action(
                fix_action, rewrite_sql, query_info
            )
            
            old_time = query_info.execution_time
            new_time = evaluation_result.get('new_time', old_time)
            approve_time = evaluation_result.get('approve_time', 0.0)
            
            return {
                'query_id': query_id,
                'old_time': old_time,
                'new_time': new_time,
                'improvement': approve_time,
                'improvement_ratio': (old_time - new_time) / old_time if old_time > 0 else 0.0,
                'status': evaluation_result.get('status', 0),
                'message': evaluation_result.get('msg', '')
            }
        
        result = run_async(_execute())
        return jsonify(result)
    
    except Exception as e:
        logger.error(f"Error executing tune for query {query_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


def parse_tuning_actions(fix_action: str, rewrite_sql: str, root_causes: List[str]) -> List[Dict]:
    """Parse tuning actions into structured format based on root causes"""
    from generate_diagnosis import parse_tuning_actions as parse_tuning_actions_impl
    return parse_tuning_actions_impl(fix_action, rewrite_sql, root_causes)


def save_multi_tune_result(query_id: str, round_num: int, result: Dict):
    """Save multi-tune result to storage file"""
    try:
        # Load existing results
        if os.path.exists(MULTI_TUNE_STORAGE_FILE):
            with open(MULTI_TUNE_STORAGE_FILE, 'r', encoding='utf-8') as f:
                all_results = json.load(f)
        else:
            all_results = {}
        
        # Initialize query_id entry if not exists
        if query_id not in all_results:
            all_results[query_id] = {
                'total_rounds': 30,
                'current_round': 0,
                'rounds': [],
                'initial_time': None
            }
        
        # Update round data
        if all_results[query_id]['initial_time'] is None:
            all_results[query_id]['initial_time'] = result.get('old_time', result.get('execution_time', 0.0))
        
        # Add or update round result
        rounds = all_results[query_id]['rounds']
        # Check if round already exists
        round_index = round_num - 1
        if round_index < len(rounds):
            rounds[round_index] = result
        else:
            rounds.append(result)
        
        all_results[query_id]['current_round'] = round_num
        
        # Save back to file
        with open(MULTI_TUNE_STORAGE_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        
        # Also write to log file
        log_entry = {
            'timestamp': time.time(),
            'query_id': query_id,
            'round': round_num,
            'result': result
        }
        with open(MULTI_TUNE_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
        
        logger.info(f"Saved multi-tune result for query {query_id}, round {round_num}")
    except Exception as e:
        logger.error(f"Error saving multi-tune result for query {query_id}, round {round_num}: {e}")


def load_multi_tune_result(query_id: str) -> Optional[Dict]:
    """Load multi-tune result from storage file"""
    try:
        if os.path.exists(MULTI_TUNE_STORAGE_FILE):
            with open(MULTI_TUNE_STORAGE_FILE, 'r', encoding='utf-8') as f:
                all_results = json.load(f)
                return all_results.get(query_id)
        return None
    except Exception as e:
        logger.error(f"Error loading multi-tune result for query {query_id}: {e}")
        return None


def run_multi_tune_background(query_id: str, total_rounds: int = 30):
    """Background task to run multiple rounds of tuning"""
    def _run():
        try:
            if SLOW_QUERY_DATA is None:
                load_slow_query_list()
            
            if query_id not in SLOW_QUERY_DATA:
                logger.error(f"Query {query_id} not found for multi-tune")
                return
            
            query_data = SLOW_QUERY_DATA[query_id]['query_info']
            
            # Load existing progress
            existing_result = load_multi_tune_result(query_id)
            start_round = 1
            initial_time = query_data.get('execution_time', 0.0)
            
            if existing_result:
                current_round = existing_result.get('current_round', 0)
                if current_round >= total_rounds:
                    logger.info(f"Multi-tune for query {query_id} already completed ({current_round}/{total_rounds})")
                    return
                start_round = current_round + 1
                if existing_result.get('initial_time'):
                    initial_time = existing_result['initial_time']
            
            # Create a new event loop for this thread
            # Use nest_asyncio to handle nested event loops properly
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            async def _async_run():
                try:
                    # Initialize agent if needed
                    global DREAM_AGENT
                    if DREAM_AGENT is None:
                        await initialize_dream_agent()
                    
                    current_exec_time = initial_time
                    
                    # Run multiple rounds
                    for round_num in range(start_round, total_rounds + 1):
                        try:
                            logger.info(f"Starting round {round_num}/{total_rounds} for query {query_id}")
                            
                            # Build QueryInfo with current execution time
                            query_info = QueryInfo(
                                query_id=query_data['query_id'],
                                query=query_data['query'],
                                plan_json=query_data['plan_json'],
                                external_metrics=query_data['external_metrics'],
                                internal_metrics=query_data['internal_metrics'],
                                execution_time=current_exec_time,
                                is_rewrite=query_data.get('is_rewrite', False)
                            )
                            
                            # Run one round of tuning
                            state = {
                                "root_tried": set(),
                                "current_root": None,
                                "mode": "exploit",
                                "confidence": {},
                                "attempts": {},
                                "successes": {},
                                "component_attempts": {},
                            }
                            
                            predicted_root, updated_state, explanation = await DREAM_AGENT.planner.predict(
                                query_info, state, DREAM_AGENT.memory_manager
                            )
                            
                            root_causes = predicted_root if isinstance(predicted_root, list) else [predicted_root]
                            
                            # Generate tuning actions and execute
                            evaluation_result = await DREAM_AGENT.action_manager.step(
                                query_info, root_causes, mode="exploit"
                            )
                            
                            # Extract tuning suggestions
                            fix_action = evaluation_result.get('fix_action', '')
                            rewrite_sql = evaluation_result.get('rewrite_sql', '')
                            
                            tuning_suggestions = parse_tuning_actions(fix_action, rewrite_sql, root_causes)
                            
                            # Get new execution time
                            new_time = evaluation_result.get('new_time', query_info.execution_time)
                            old_time = query_info.execution_time
                            improvement_ratio = (old_time - new_time) / old_time if old_time > 0 else 0.0
                            
                            # Update current execution time for next round
                            current_exec_time = new_time
                            
                            # Map root causes to Chinese
                            root_cause_map = {
                                "missing indexes": "索引选择",
                                "inappropriate query knobs": "参数调优",
                                "suboptimal plan optimizer": "查询计划调优",
                                "poorly written queries": "查询改写",
                            }
                            root_causes_chinese = [root_cause_map.get(rc.lower().replace(" ", "_"), rc) for rc in root_causes]
                            
                            # Prepare result
                            round_result = {
                                'round': round_num,
                                'exec_time': new_time,
                                'old_time': old_time,
                                'improvement_ratio': improvement_ratio,
                                'root_causes': root_causes_chinese,
                                'explanation': explanation if isinstance(explanation, str) else str(explanation) if explanation else '暂无解释说明',
                                'operations': [
                                    {
                                        'rootCause': suggestion['type'],
                                        'operation': suggestion['action']
                                    }
                                    for suggestion in tuning_suggestions
                                ],
                                'fix_action': fix_action,
                                'rewrite_sql': rewrite_sql
                            }
                            
                            # Save result (this also writes to log)
                            save_multi_tune_result(query_id, round_num, round_result)
                            
                            logger.info(f"Completed round {round_num}/{total_rounds} for query {query_id}: {old_time:.2f}s -> {new_time:.2f}s (improvement: {improvement_ratio*100:.1f}%)")
                            
                            # Small delay between rounds
                            await asyncio.sleep(1)
                            
                        except Exception as e:
                            logger.error(f"Error in round {round_num} for query {query_id}: {e}", exc_info=True)
                            # Continue to next round even if this one failed
                            # Check if event loop is still valid
                            try:
                                asyncio.get_event_loop()
                            except RuntimeError:
                                logger.error(f"Event loop closed, cannot continue for query {query_id}")
                                break
                            continue
                    
                    logger.info(f"Multi-tune completed for query {query_id}: {total_rounds} rounds")
                except Exception as e:
                    logger.error(f"Fatal error in async run for query {query_id}: {e}", exc_info=True)
                    raise
            
            try:
                # Ensure event loop is running
                if loop.is_closed():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                
                loop.run_until_complete(_async_run())
            except RuntimeError as e:
                if "Event loop is closed" in str(e):
                    logger.error(f"Event loop was closed during execution for query {query_id}, attempting to recover")
                    # Try to create a new loop and continue
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(_async_run())
                    except Exception as e2:
                        logger.error(f"Failed to recover from event loop error for query {query_id}: {e2}", exc_info=True)
                else:
                    raise
            finally:
                # Only close the loop if we created it
                try:
                    if not loop.is_closed():
                        # Cancel all pending tasks
                        pending = asyncio.all_tasks(loop)
                        for task in pending:
                            task.cancel()
                        # Wait for tasks to complete cancellation
                        if pending:
                            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                        loop.close()
                except Exception as e:
                    logger.warning(f"Error closing event loop for query {query_id}: {e}")
            
        except Exception as e:
            logger.error(f"Error in multi-tune background task for query {query_id}: {e}", exc_info=True)
        finally:
            # Remove task from tracking
            with MULTI_TUNE_TASK_LOCK:
                if query_id in MULTI_TUNE_TASKS:
                    del MULTI_TUNE_TASKS[query_id]
    
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


@app.route('/api/multi-tune/<query_id>', methods=['POST'])
def multi_tune_query(query_id: str):
    """Start or continue multi-round tuning (default 30 rounds) in background"""
    try:
        data = request.get_json() or {}
        total_rounds = data.get('total_rounds', 30)  # Default 30 rounds
        
        if SLOW_QUERY_DATA is None:
            load_slow_query_list()
        
        if query_id not in SLOW_QUERY_DATA:
            return jsonify({'error': f'Query {query_id} not found'}), 404
        
        # Check if task is already running
        with MULTI_TUNE_TASK_LOCK:
            if query_id in MULTI_TUNE_TASKS:
                task = MULTI_TUNE_TASKS[query_id]
                if task.is_alive():
                    return jsonify({
                        'query_id': query_id,
                        'message': '多轮调优任务已在运行中',
                        'status': 'running'
                    })
                else:
                    # Task finished, remove it
                    del MULTI_TUNE_TASKS[query_id]
        
        # Load existing progress
        existing_result = load_multi_tune_result(query_id)
        if existing_result:
            current_round = existing_result.get('current_round', 0)
            if current_round >= total_rounds:
                return jsonify({
                    'query_id': query_id,
                    'message': f'调优已完成，共 {total_rounds} 轮',
                    'completed': True,
                    'current_round': current_round,
                    'total_rounds': total_rounds
                })
        
        # Start background task
        thread = run_multi_tune_background(query_id, total_rounds)
        
        with MULTI_TUNE_TASK_LOCK:
            MULTI_TUNE_TASKS[query_id] = thread
        
        return jsonify({
            'query_id': query_id,
            'message': f'多轮调优任务已启动，将执行 {total_rounds} 轮',
            'status': 'started',
            'total_rounds': total_rounds
        })
    
    except Exception as e:
        logger.error(f"Error starting multi-tune for query {query_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/multi-tune-progress/<query_id>', methods=['GET'])
def get_multi_tune_progress(query_id: str):
    """Get multi-tune progress for a query"""
    try:
        result = load_multi_tune_result(query_id)
        
        if not result:
            # 如果没有多轮调优结果，尝试从诊断结果中获取初始时间
            diagnosis_result = load_diagnosis_result(query_id)
            initial_time = None
            if diagnosis_result and diagnosis_result.get('query_info'):
                initial_time = diagnosis_result['query_info'].get('execution_time')
            
            return jsonify({
                'query_id': query_id,
                'current_round': 0,
                'total_rounds': 30,
                'rounds': [],
                'initial_time': initial_time
            })
        
        # Format rounds data for frontend
        rounds = []
        for round_data in result.get('rounds', []):
            rounds.append({
                'round': round_data.get('round', 0),
                'exec_time': round_data.get('exec_time', 0.0),
                'old_time': round_data.get('old_time', 0.0),
                'improvement_ratio': round_data.get('improvement_ratio', 0.0),
                'root_causes': round_data.get('root_causes', []),
                'explanation': round_data.get('explanation', ''),
                'operations': round_data.get('operations', [])
            })
        
        return jsonify({
            'query_id': query_id,
            'current_round': result.get('current_round', 0),
            'total_rounds': result.get('total_rounds', 30),
            'rounds': rounds,
            'initial_time': result.get('initial_time', 0.0)
        })
    
    except Exception as e:
        logger.error(f"Error getting multi-tune progress for query {query_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/diagnosis-result/<query_id>', methods=['GET'])
def get_diagnosis_result(query_id: str):
    """Get stored diagnosis result for a query (without re-running diagnosis)"""
    try:
        result = load_diagnosis_result(query_id)
        if result:
            return jsonify(result)
        else:
            return jsonify({'error': f'No diagnosis result found for query {query_id}'}), 404
    except Exception as e:
        logger.error(f"Error getting diagnosis result for query {query_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/tuning-result/<query_id>', methods=['GET'])
def get_tuning_result(query_id: str):
    """Get stored tuning result for a query (without re-running tuning)"""
    try:
        result = load_tuning_result(query_id)
        if result:
            # Parse tuning suggestions for frontend
            from generate_diagnosis import parse_tuning_actions
            tuning_suggestions = parse_tuning_actions(
                result.get('fix_action', ''),
                result.get('rewrite_sql', ''),
                result.get('root_causes', [])
            )
            result['tuning_suggestions'] = tuning_suggestions
            return jsonify(result)
        else:
            return jsonify({'error': f'No tuning result found for query {query_id}'}), 404
    except Exception as e:
        logger.error(f"Error getting tuning result for query {query_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    # Load configuration and data
    load_config()
    load_slow_query_list()
    
    # Add error handler for 403
    @app.errorhandler(403)
    def forbidden(error):
        logger.error(f"403 Forbidden: {request.url}")
        return jsonify({'error': 'Forbidden', 'message': str(error)}), 403
    
    # Add error handler for 404
    @app.errorhandler(404)
    def not_found(error):
        logger.error(f"404 Not Found: {request.url}")
        return jsonify({'error': 'Not Found', 'message': str(error)}), 404
    
    # Run Flask app
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
