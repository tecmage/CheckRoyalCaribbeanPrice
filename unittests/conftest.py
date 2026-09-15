# Make the repository root importable so `pytest unittests/` works without
# PYTHONPATH=. (the scripts under test live one directory up).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
