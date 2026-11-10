from __future__ import annotations

import json
from pathlib import Path
from .schemas import ValidationReport


def write_report(report: ValidationReport, path: str | Path) -> None:
    Path(path).write_text(json.dumps(report.model_dump(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def print_summary(report: ValidationReport) -> None:
    print(f"Domain Semantic F1: {report.score:.2f}\n")
    print(f"{'Category':26s} {'Precision':>10s} {'Recall':>8s} {'F1':>8s}")
    for name, scores in report.category_scores.items():
        print(f"{name:26s} {scores['precision']:10.4f} {scores['recall']:8.4f} {scores['f1']:8.4f}")
