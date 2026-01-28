import sys
from pathlib import Path

# Get the project root directory
root_dir = Path(__file__).parent.parent

# Add the project root directory to Python path
sys.path.append(str(root_dir))
