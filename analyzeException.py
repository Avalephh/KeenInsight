import json
import os
import argparse
import numpy as np
import configparser
from DBTuner.utils.analyzeException import get_key_functions
from DBTuner.utils.matchFunctions import get_knob_in_keyFunctions

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze Exception Functions")
    parser.add_argument("--config", type=str, required=True, help="Path to the configuration file")
    args = parser.parse_args()
    config_file = args.config

    config = configparser.ConfigParser()
    config.read(config_file)

    file = config['database']['current_function_file']
    normal_file = config['database']['base_function_file']

    output_file_path = get_key_functions(file, normal_file)
    staticFile = "/home/sysinsight/DBTuner/utils/paramater_association_library.json"
    function_to_knob = get_knob_in_keyFunctions(output_file_path,staticFile,10)

    
    print(f"[RESULT_FILE]{output_file_path}")
    print(f"[FUNCTION_TO_KNOB]{json.dumps(function_to_knob)}")

