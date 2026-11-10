import subprocess
import sys


def test_no_llm_and_model_flags_are_removed():
    proc = subprocess.run([sys.executable, "validate.py", "--gt", "x.json", "--target", "y.xlsx", "--no-llm"], text=True, capture_output=True)
    assert proc.returncode != 0
    assert "unrecognized arguments: --no-llm" in proc.stderr
    proc = subprocess.run([sys.executable, "validate.py", "--gt", "x.json", "--target", "y.xlsx", "--model", "gpt-4o"], text=True, capture_output=True)
    assert proc.returncode != 0
    assert "unrecognized arguments: --model" in proc.stderr


def test_help_describes_gpt54_table_validator():
    proc = subprocess.run([sys.executable, "validate.py", "--help"], text=True, capture_output=True)
    assert proc.returncode == 0
    assert "gpt-5.4" in proc.stdout
    assert "테이블 구조" in proc.stdout
    assert "--no-llm" not in proc.stdout
    assert "--model" not in proc.stdout
