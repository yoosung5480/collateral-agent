from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from validator.parser import MODEL_NAME, parse_excel
from validator.reporter import print_summary, write_report
from validator.schemas import DomainJSON, ValidatorConfig
from validator.scorer import LLMScorer


def load_config(path: str | None) -> ValidatorConfig:
    default = Path("config/default.json")
    data: dict[str, Any] = {}
    if default.exists():
        data.update(json.loads(default.read_text(encoding="utf-8")))
    if path:
        data.update(json.loads(Path(path).read_text(encoding="utf-8")))
    return ValidatorConfig.model_validate(data)


def infer_gt_format(path: str, explicit: str) -> str:
    if explicit != "auto":
        return explicit
    suffix = Path(path).suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return "excel"
    if suffix == ".json":
        return "json"
    raise ValueError(f"GT 형식을 자동 판별할 수 없습니다: {path}")


def load_gt(path: str, fmt: str, config: ValidatorConfig) -> tuple[DomainJSON, list[dict[str, Any]]]:
    if fmt == "json":
        return DomainJSON.model_validate_json(Path(path).read_text(encoding="utf-8")), []
    return parse_excel(path, is_gt=True, config=config)


def save_domain(domain: DomainJSON, path: str | None) -> None:
    if path:
        Path(path).write_text(json.dumps(domain.model_dump(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="담보물배분표 gpt-5.4 테이블 구조 기반 의미 검증기")
    p.add_argument("--gt", required=True, help="GT Excel 또는 검수 고정 GT Domain JSON 경로")
    p.add_argument("--target", required=True, help="검증 대상 Excel 경로")
    p.add_argument("--gt-format", choices=["auto", "excel", "json"], default="auto")
    p.add_argument("--output", default="validation_report.json")
    p.add_argument("--save-gt-json")
    p.add_argument("--save-target-json")
    p.add_argument("--config")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv(".env")
    args = build_arg_parser().parse_args(argv)
    config = load_config(args.config)
    gt_format = infer_gt_format(args.gt, args.gt_format)

    gt_domain, gt_derived = load_gt(args.gt, gt_format, config)
    target_domain, target_derived = parse_excel(args.target, is_gt=False, config=config)
    save_domain(gt_domain, args.save_gt_json)
    save_domain(target_domain, args.save_target_json)

    metadata = {
        "gt_path": args.gt,
        "target_path": args.target,
        "gt_format": gt_format,
        "model": MODEL_NAME,
        "llm_required": True,
        "table_structure_first": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    report = LLMScorer(config).score(gt_domain, target_domain, metadata=metadata, derived_values=gt_derived + target_derived)
    write_report(report, args.output)
    print_summary(report)
    if args.verbose:
        print(f"\nReport written to {args.output}")
        if report.errors:
            print("\n주요 오류:")
            for err in report.errors[:20]:
                print(f"- [{err.category}] {err.object}.{err.field}: {err.status} ({err.reason})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
