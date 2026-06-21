import os
import sys

# Make the skill package importable by bare module name so that
# server.py's `import dataset_search` resolves during tests.
SKILL_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../../../services/skills/dataset-search")
)
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)
