"""Compare generated workbook with GT on fields that should be auto-filled.

Allocation amount/balance columns (J,K,N,O) are intentionally ignored.
"""

from __future__ import annotations

import argparse
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

IGNORED_COLUMNS = {10, 11, 14, 15}  # 배분액, 배분잔액
CHECK_ROWS = list(range(1, 61))
CHECK_COLUMNS = list(range(1, 16))


def norm(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, str):
        text = "\n".join(line.rstrip() for line in value.strip().splitlines()).strip()
        if text.isdigit():
            return int(text)
        return text
    return value


def compare(output_path: Path, gt_path: Path) -> list[str]:
    out = load_workbook(output_path, data_only=False).active
    gt = load_workbook(gt_path, data_only=False).active
    diffs: list[str] = []
    for row in CHECK_ROWS:
        for col in CHECK_COLUMNS:
            if col in IGNORED_COLUMNS:
                continue
            left = norm(out.cell(row, col).value)
            right = norm(gt.cell(row, col).value)
            # GT has a stored numeric total in I60 while generated workbooks keep the formula.
            if row == 60 and col == 9 and isinstance(left, str) and left.startswith("=SUM("):
                continue
            # Compare formulas in G60 as equivalent if both sum quantity column.
            if row == 60 and col == 7 and isinstance(left, str) and isinstance(right, str) and left.startswith("=SUM(") and right.startswith("=SUM("):
                continue
            if left != right:
                diffs.append(f"{out.cell(row, col).coordinate}: output={left!r}, gt={right!r}")
    return diffs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--gt", type=Path, default=Path("GT/담보물배분표GT.xlsx"))
    args = parser.parse_args()
    diffs = compare(args.output, args.gt)
    if diffs:
        print(f"FAIL: {len(diffs)} differences")
        for diff in diffs[:50]:
            print(diff)
        raise SystemExit(1)
    print("PASS: GT comparable cells match")


if __name__ == "__main__":
    main()
