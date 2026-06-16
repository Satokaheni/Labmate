import subprocess
import os
from config.settings import CODEBASE_PATH
def search_code(query):
    try:
        abs_path = os.path.abspath(CODEBASE_PATH)
        result = subprocess.run(["codegraph", "search", query], cwd=abs_path, capture_output=True, text=True, timeout=5)
        return result.stdout if result.stdout else "No structural matches found."
    except Exception as e: return f"Codegraph Error: {e}"
