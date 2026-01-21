import os
import re
import csv
import argparse
from collections import defaultdict
from statistics import mean, median, stdev

def parse_sampling_file(filepath):
    """
    Parse a single sampling result file and return a dictionary of function absolute counts.
    
    Parameters:
    - filepath (str): Path to the sampling result file.
    
    Returns:
    - dict: {function name: absolute count}
    """
    absolute_counts = {}
    try:
        with open(filepath, 'r') as f:
            headers = f.readline()  # Skip header
            for line in f:
                parts = line.strip().split('\t')
                print(parts)
                if len(parts) < 3:
                    continue  # Skip improperly formatted lines
                func = parts[1]
                absolute_value = parts[3]  # Absolute Value is the fourth column
                try:
                    absolute_value = float(absolute_value)
                    if func in absolute_counts:
                        absolute_counts[func].append(absolute_value)
                    else:
                        absolute_counts[func] = [absolute_value]
                except ValueError:
                    print(f"Warning: Unable to parse absolute value '{absolute_value}' for function '{func}' in file '{filepath}'.")
    except Exception as e:
        print(f"Error: An error occurred while reading file '{filepath}': {e}")
    return absolute_counts

def aggregate_sampling_data(directory):
    """
    Aggregate function absolute count data from all result files in the specified directory.
    
    Parameters:
    - directory (str): Path to the directory containing sampling result files.
    
    Returns:
    - dict: {function name: [absolute count1, absolute count2, ...]}
    """
    print(directory)
    absolute_counts = defaultdict(list)
    # Regular expression to match sampling result files
    # file_pattern = re.compile(r'^perf_\d+_counts_normal\.txt$')
    file_pattern = re.compile(r'^perf_\d+_counts_(normal|good|bad)\.txt$')
    
    for filename in os.listdir(directory):
        if file_pattern.match(filename):
            filepath = os.path.join(directory, filename)
            print(f"Processing file: {filepath}")
            file_absolute_counts = parse_sampling_file(filepath)
            for func, counts in file_absolute_counts.items():
                absolute_counts[func].extend(counts)
    
    return absolute_counts

def compute_statistics(absolute_counts):
    """
    Calculate statistics for the absolute counts of each function.
    
    Parameters:
    - absolute_counts (dict): {function name: [absolute count1, absolute count2, ...]}
    
    Returns:
    - list of dict: Each dictionary contains function name and statistics.
    """
    stats = []
    for func, counts in absolute_counts.items():
        if len(counts) < 2:
            std_dev = 0.0  # Standard deviation requires at least two data points
        else:
            std_dev = stdev(counts)
        func_stats = {
            'Function': func,
            'Min': min(counts),
            'Max': max(counts),
            'Mean': mean(counts),
            'Median': median(counts),
            'Std Dev': std_dev,
            'Count': len(counts)
        }
        stats.append(func_stats)
    return stats

def write_stats_to_csv(stats, output_filepath):
    """
    Write statistics to a CSV file.
    
    Parameters:
    - stats (list of dict): List of statistics results.
    - output_filepath (str): Path to the output CSV file.
    """
    headers = ['Function', 'Min', 'Max', 'Mean', 'Median', 'Std Dev', 'Count']
    try:
        with open(output_filepath, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=headers)
            writer.writeheader()
            for entry in stats:
                writer.writerow(entry)
        print(f"Statistics written to '{output_filepath}'")
    except Exception as e:
        print(f"Error: An error occurred while writing to CSV file '{output_filepath}': {e}")

def main():
    parser = argparse.ArgumentParser(description="Calculate the absolute count statistics for each function in multiple perf sampling result files.")
    parser.add_argument("directory", help="Path to the directory containing perf sampling result files.")
    parser.add_argument("-o", "--output", default="function_sampling_stats.csv",
                        help="Path to output the statistics CSV file (default: function_sampling_stats.csv).")
    args = parser.parse_args()
    
    if not os.path.isdir(args.directory):
        print(f"Error: The specified directory '{args.directory}' does not exist or is not a directory.")
        return
    
    # Aggregate sampling data
    absolute_counts = aggregate_sampling_data(args.directory)
    
    if not absolute_counts:
        print("Warning: No valid sampling data found.")
        return
    
    # Compute statistics
    stats = compute_statistics(absolute_counts)
    
    # Write statistics to CSV file
    write_stats_to_csv(stats, args.output)

if __name__ == "__main__":
    main()
