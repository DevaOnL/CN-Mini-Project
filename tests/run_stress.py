#!/usr/bin/env python3
"""Run stress test and save results - designed to be run standalone."""
import subprocess
import sys
import os

project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
result = subprocess.run(
    [sys.executable, os.path.join(project_dir, 'tests', 'stress_test.py')],
    capture_output=True, text=True, timeout=120, cwd=project_dir
)
with open('/tmp/stress_results.txt', 'w') as f:
    f.write(result.stdout)
    f.write(result.stderr)
    f.write(f'\nEXIT_CODE={result.returncode}\n')
print("DONE - results in /tmp/stress_results.txt")
