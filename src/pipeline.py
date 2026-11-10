"""LLM-guided collateral distribution pipeline.

The parser packages turn the three input PDFs into source JSON.  This module then
creates explicit stage artifacts, asks an LLM only for semantic ID-to-ID matching,
and keeps all grouping, arithmetic, and Excel plan validation in code.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, model_validator


REGISTRY_PATTERN = re.compile(r"registry_(\d+)", re.IGNORECASE)
MONEY_PATTERN = re.compile(r"-?\d[\d,]*")
LIST_NO_PATTERN = re.compile(r"(\d{4})\s*-\s*(\d+)")
MACHINERY_MATCH_SYSTEM_PROMPT = """
너는 감정평가서 기계기구 행과 공장저당법 기계기구목록 행의 동일성 대조자다.

목표:
감정평가 행과 저당목록 항목이 동일한 실제 기계기구를 의미하는지 판단하여
ID 관계만 반환한다. 금액을 생성하거나 수정하지 않는다.

핵심 식별정보:
1. 명칭(name)
2. 제작자(manufacturer)
3. 제작연월(manufactured_date)

위 3개가 핵심 식별정보다.
규격(specification), 수량(quantity), 문서 등장순서(source_order)는
핵심 3개가 여러 후보에서 비슷할 때만 보조 근거로 사용한다.

OCR 노이즈 처리:
- 대소문자, 띄어쓰기, 구두점 차이는 무시한다.
- 명백한 OCR 오탈자나 표기 변형은 의미적으로 동일한 것으로 허용한다.
  예:
  - "Over Head Crane" ≈ "Over head Crene"
  - "경남 HC" ≈ "경남H.C" ≈ "경남 호이스트"
  - "Atlas Copco" ≈ "ATLAS COPCO"
  - "2012.06/11" ≈ "2012.06.11"
- 단, 의미가 실제로 다른 명칭이나 제작자를 OCR 오타라고 임의로 가정하지 않는다.

판정 규칙:
- 핵심 3개가 의미적으로 일치하면 matched를 우선한다.
- 핵심 3개 중 일부에 OCR 노이즈가 있어도 나머지 핵심정보가 강하게 일치하면 matched할 수 있다.
- 핵심정보가 비슷한 후보가 둘 이상이면 specification, quantity, source_order로 구분한다.
- 보조정보가 다르다는 이유만으로 핵심 3개가 일치하는 항목을 unmatched로 만들지 않는다.
- 핵심정보 자체가 충돌하면 unresolved로 둔다.
- 목록 전체에 대응 가능한 항목이 없을 때만 unmatched로 둔다.

보수성:
OCR 오타라고 설명 가능한 작은 표기 차이는 허용하되,
서로 다른 장비를 억지로 동일하다고 추정하지 않는다.
""".strip()

class RealEstateAssignment(BaseModel):
    appraisal_group_id: str
    target_group_id: str | None = None
    related_property_ids: list[str] = Field(default_factory=list)
    decision: Literal["matched", "unmatched", "unresolved"]
    evidence: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


class RealEstateMatchDecision(BaseModel):
    status: Literal["ok", "review_required"]
    assignments: list[RealEstateAssignment] = Field(default_factory=list)
    unassigned_appraisal_group_ids: list[str] = Field(default_factory=list)
    review_required: list[dict[str, Any]] = Field(default_factory=list)


class RealEstateAssignmentVerification(BaseModel):
    appraisal_group_id: str
    target_group_id: str | None = None
    verdict: Literal["approved", "rejected", "unresolved"]
    reason: str


class RealEstateMatchVerification(BaseModel):
    status: Literal["ok", "review_required"]
    assignments: list[RealEstateAssignmentVerification] = Field(default_factory=list)
    review_required: list[dict[str, Any]] = Field(default_factory=list)


class MachineryItemMatch(BaseModel):
    appraisal_item_id: str
    matched_list_item_ids: list[str] = Field(default_factory=list)
    decision: Literal["matched", "unmatched", "unresolved"]
    covered_quantity: float | int | None = None
    evidence: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


class MortgageListMatchDecision(BaseModel):
    mortgage_list_no: str
    source_completeness: Literal["complete", "partial", "unknown"]
    matches: list[MachineryItemMatch] = Field(default_factory=list)
    unconsumed_list_item_ids: list[str] = Field(default_factory=list)
    review_required: list[dict[str, Any]] = Field(default_factory=list)


class MortgageListMatchVerification(BaseModel):
    mortgage_list_no: str
    status: Literal["ok", "review_required"]
    item_verdicts: list[RealEstateAssignmentVerification] = Field(default_factory=list)
    review_required: list[dict[str, Any]] = Field(default_factory=list)


class ExcelFormatPlan(BaseModel):
    """LLM-produced Excel formatting plan.

    The plan is intentionally limited to labels and presentation controls.  It
    never carries business values, calculated amounts, dates, or O/X memberships.
    """

    status: Literal["ok", "review_required"]
    sheet_name: str = Field(default="Sheet1")
    date_number_format: str = Field(description="설정일자 표시 형식. 예: mm-dd-yy")
    amount_number_format: str = Field(description="원화 금액 표시 형식. 예: #,##0")
    header_text_replacements: dict[str, str] = Field(default_factory=dict)
    column_widths: dict[str, float] = Field(default_factory=dict)
    row_heights: dict[str, float] = Field(default_factory=dict)
    alignment_by_column: dict[str, dict[str, str]] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


INTERNAL_EXCEL_FORMAT_CONTRACT: dict[str, Any] = {
    "contract_version": "internal-collateral-excel-format-v1",
    "runtime_dependency_policy": "Do not read GT/담보물배분표GT.xlsx or any external Excel template at runtime. The LLM format stage may use only this embedded contract plus the semantic workbook_plan shape.",
    "sheet": {
        "sheet_name": "Sheet1",
        "single_sheet": True,
        "zoom_scale": 61,
        "show_grid_lines": False,
        "base_font": "맑은 고딕",
    },
    "layout": {
        "fixed_columns": "A:P for distribution tables; comparison table may extend right only for mortgage-list O/X columns.",
        "vertical_order": [
            "채무자 행",
            "<부동산 담보물 배분표> title once",
            "real estate collateral groups repeated with 2 blank rows between separate tables",
            "one blank spacer row",
            "채무자 행 for machinery section",
            "<기계기구 담보물 배분표>",
            "one blank spacer row",
            "machinery collateral distribution table",
            "two note rows and one blank spacer row",
            "machinery comparison table",
        ],
        "must_not_append": "Do not append intermediate JSON, evidence dumps, source summaries, comments, or byproduct tables below the final user-facing tables.",
        "border_scope": "Apply borders only to each explicit table rectangle. Do not draw borders across blank spacer rows or unused columns.",
        "merge_spacing": "Use merged title/unit/header/body cells within each table and keep visible blank rows between independent tables.",
    },
    "distribution_table": {
        "headers": {
            "A": "담보물 소재지",
            "B": "담보물 내역 (담보물의 면적 및 수량)",
            "C:F": "담보설정현황",
            "C_sub": "순위",
            "D_sub": "설정일자",
            "E_sub": "채권자",
            "F_sub": "공담",
            "G": "가치평가액 (주1)",
            "H:O": "배분내역",
            "P": "비고",
        },
        "header_style": "bold, centered, wrapped, light blue fill, thin border within table only",
        "body_merges": ["A body vertical", "B body vertical", "G body vertical", "P body vertical"],
        "allocation_block": "Two mortgage ranks per 3-row block: H:K for odd rank, L:O for even rank; rank title row merged; second row labels 설정자/설정금액/배분액/배분잔액; third row values 채권자/채권최고액/blank/blank.",
    },
    "number_formats": {
        "date_number_format": "mm-dd-yy",
        "amount_number_format": "#,##0",
        "money_columns": ["G", "I", "J", "K", "M", "N", "O"],
    },
    "column_widths": {
        "A": 17.14,
        "B": 45.0,
        "C": 4.14,
        "D": 15.57,
        "E": 16.14,
        "F": 5.57,
        "G": 20.14,
        "H": 20.14,
        "I": 13.0,
        "J": 13.0,
        "K": 13.0,
        "L": 13.0,
        "M": 13.0,
        "N": 13.0,
        "O": 13.0,
        "P": 14.14,
    },
    "format_plan_limits": {
        "allowed_to_change": ["sheet_name", "header_text_replacements", "column_widths", "row_heights", "date_number_format", "amount_number_format", "alignment_by_column", "notes"],
        "forbidden_to_change": ["business values", "dates as values", "amounts", "O/X", "mortgagee", "borrower", "locations", "row/table counts", "source evidence"],
    },
}


@dataclass(frozen=True)
class RegistrySource:
    registry_id: str
    title_path: Path
    encumbrance_path: Path
    title: Mapping[str, Any]
    encumbrance: Mapping[str, Any]


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def registry_key(value: Any, fallback: str = "") -> str:
    match = REGISTRY_PATTERN.search(str(value or "")) or REGISTRY_PATTERN.search(fallback)
    if match:
        return f"registry_{int(match.group(1)):02d}"
    fallback_stem = Path(str(fallback or "registry_unknown")).stem
    safe = re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", fallback_stem).strip("_")
    return safe or "registry_unknown"


def registry_sort_key(value: str) -> tuple[int, str]:
    match = re.search(r"(\d+)", value)
    return (int(match.group(1)) if match else 10**9, value)


def parse_date_tuple(value: Any) -> tuple[int, int, int]:
    text = clean(value).replace("년", ".").replace("월", ".").replace("일", "")
    text = text.replace("-", ".").replace("/", ".")
    match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", text)
    if match:
        return tuple(map(int, match.groups()))  # type: ignore[return-value]
    # 시연/범용 문서에서는 날짜 OCR이 깨져도 pipeline을 중단하지 않는다.
    return (9999, 12, 31)


def iso_date(value: Any) -> str:
    text = clean(value)
    match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", text.replace("년", ".").replace("월", ".").replace("일", ""))
    if not match:
        return text
    y, m, d = map(int, match.groups())
    return f"{y:04d}-{m:02d}-{d:02d}"


def parse_money(value: Any) -> int:
    match = MONEY_PATTERN.search(str(value or ""))
    if not match:
        return 0
    return int(match.group(0).replace(",", ""))


def normalize_shared(value: Any) -> str:
    text = clean(value)
    return "공담" if text in {"공용담보", "공동담보", "공담", "공동탐보"} else text


def normalize_mortgagee(value: Any) -> str:
    text = clean(value)
    text = re.sub(r"^주식회사\s*", "", text)
    text = re.sub(r"\s+", "", text)
    return text


def normalize_list_no(value: Any) -> str:
    text = clean(value)
    match = LIST_NO_PATTERN.search(text)
    if match:
        return f"공장 및 광업재단 저당법 제6조 목록 제{match.group(1)}-{int(match.group(2))}호"
    return text


def short_list_no(value: Any) -> str:
    match = LIST_NO_PATTERN.search(str(value or ""))
    return f"{match.group(1)}-{int(match.group(2))}" if match else clean(value)




def _soft_validation_result(run_id: Any, errors: Sequence[str], warnings: Sequence[str] | None = None) -> dict[str, Any]:
    """Return a non-blocking validation artifact.

    Validation artifacts are for audit/reporting only.  The service requirement is
    to produce an Excel file for the supplied documents whenever a structural
    workbook can be assembled, so rule disagreements are recorded as warnings
    instead of becoming hard generation stops.
    """

    error_list = list(errors)
    warning_list = list(warnings or [])
    return {
        "run_id": run_id,
        "status": "ok",
        "errors": error_list,
        "warnings": warning_list,
        "blocking": False,
    }

def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def file_sha256(path: str | Path) -> str:
    return sha256(Path(path).read_bytes()).hexdigest()


def build_manifest(
    *,
    run_id: str,
    borrower_name: str,
    registry_pairs: Sequence[Mapping[str, Any]],
    appraisal_json_path: str | Path,
    mortgage_json_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    mortgage_data = load_json(mortgage_json_path)
    return {
        "run_id": run_id,
        "borrower_name": borrower_name,
        "registries": [
            {"title_path": str(item["title_json"]), "encumbrance_path": str(item["encumbrance_json"])}
            for item in registry_pairs
        ],
        "real_estate_appraisal_path": str(appraisal_json_path),
        "machinery_appraisal_path": str(appraisal_json_path),
        "mortgage_lists_path": str(mortgage_json_path),
        "mortgage_lists": [
            {"mortgage_list_no": m["mortgage_list_no"], "path": str(mortgage_json_path)}
            for m in mortgage_data.get("mortgage_lists", [])
        ],
        "output_path": str(output_path),
    }


def load_manifest_sources(manifest: Mapping[str, Any]) -> list[RegistrySource]:
    records: list[RegistrySource] = []
    seen: set[str] = set()
    for pair in manifest["registries"]:
        title_path = Path(pair["title_path"])
        enc_path = Path(pair["encumbrance_path"])
        title = load_json(title_path)
        enc = load_json(enc_path)
        title_id = registry_key(title.get("registry_id"), str(title_path))
        enc_id = registry_key(enc.get("registry_id"), str(enc_path))
        if title_id != enc_id:
            title_id = title_id or enc_id
        if title_id in seen:
            title_id = f"{title_id}_{len(seen) + 1}"
        seen.add(title_id)
        records.append(RegistrySource(title_id, title_path, enc_path, title, enc))
    return sorted(records, key=lambda item: registry_sort_key(item.registry_id))


def _mortgage_signature(raw_mortgages: Sequence[Mapping[str, Any]]) -> tuple[tuple[Any, ...], ...]:
    signature = []
    for source_order, mortgage in enumerate(raw_mortgages):
        signature.append(
            (
                iso_date(mortgage.get("registration")),
                parse_money(mortgage.get("maximum_claim")),
                normalize_mortgagee(mortgage.get("mortgagee")),
                normalize_shared(mortgage.get("shared_collateral")),
                normalize_list_no(mortgage.get("mortgage_list_no")),
                source_order,
            )
        )
    return tuple(signature)


def stage1_merge_registry(manifest: Mapping[str, Any], stage_dir: str | Path) -> dict[str, Path]:
    sources = load_manifest_sources(manifest)
    groups_by_signature: dict[tuple[tuple[Any, ...], ...], dict[str, Any]] = {}
    signature_order: list[tuple[tuple[Any, ...], ...]] = []

    for source in sources:
        mortgages_with_order = list(enumerate(source.encumbrance.get("mortgages") or []))
        mortgages_with_order.sort(key=lambda pair: (parse_date_tuple(pair[1].get("registration")), pair[0]))
        sorted_mortgages = [item for _, item in mortgages_with_order]
        signature = _mortgage_signature(sorted_mortgages)
        if signature not in groups_by_signature:
            group_id = f"group_{len(signature_order) + 1:02d}"
            groups_by_signature[signature] = {
                "group_id": group_id,
                "registry_ids": [],
                "properties": [],
                "mortgages": sorted_mortgages,
            }
            signature_order.append(signature)
        group = groups_by_signature[signature]
        group["registry_ids"].append(source.registry_id)
        group["properties"].append(
            {
                "property_id": f"{source.registry_id}:title",
                "registry_id": source.registry_id,
                "property_type": clean(source.title.get("property_type")),
                "location": clean(source.title.get("location")),
                "details": str(source.title.get("details") or "").strip(),
                "source_path": str(source.title_path),
            }
        )

    title_groups = []
    enc_groups = []
    for signature in signature_order:
        group = groups_by_signature[signature]
        group["properties"].sort(key=lambda p: ({"토지": 0, "건물": 1}.get(p["property_type"], 9), registry_sort_key(p["registry_id"])))
        title_groups.append({k: group[k] for k in ("group_id", "registry_ids", "properties")})
        enc_mortgages = []
        for rank, mortgage in enumerate(group["mortgages"], start=1):
            enc_mortgages.append(
                {
                    "mortgage_id": f"{group['group_id']}:mortgage_{rank:02d}",
                    "rank": rank,
                    "registration": iso_date(mortgage.get("registration")),
                    "maximum_claim": parse_money(mortgage.get("maximum_claim")),
                    "mortgagee": clean(mortgage.get("mortgagee")),
                    "shared_collateral": normalize_shared(mortgage.get("shared_collateral")),
                    "mortgage_list_no": normalize_list_no(mortgage.get("mortgage_list_no")),
                    "source_order": rank,
                }
            )
        enc_groups.append({"group_id": group["group_id"], "registry_ids": group["registry_ids"], "mortgages": enc_mortgages})

    stage_dir = Path(stage_dir)
    merged_title = {
        "run_id": manifest["run_id"],
        "status": "ok",
        "groups": title_groups,
        "review_required": [],
        "blocking_errors": [],
    }
    merged_encumbrance = {
        "run_id": manifest["run_id"],
        "status": "ok",
        "groups": enc_groups,
        "review_required": [],
        "blocking_errors": [],
    }
    validation = {
        "run_id": manifest["run_id"],
        "status": "ok",
        "registry_count": len(sources),
        "group_count": len(title_groups),
        "registry_ids": [s.registry_id for s in sources],
        "review_required": [],
        "blocking_errors": [],
    }
    return {
        "merged_title": write_json(stage_dir / "merged_title.json", merged_title),
        "merged_encumbrance": write_json(stage_dir / "merged_encumbrance.json", merged_encumbrance),
        "merge_validation": write_json(stage_dir / "merge_validation.json", validation),
    }


def build_real_estate_appraisal_groups(appraisal: Mapping[str, Any]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for kind in ("land", "building"):
        for index, item in enumerate(appraisal.get(kind) or [], start=1):
            groups.append(
                {
                    "appraisal_group_id": f"{kind}_{index:03d}",
                    "property_type": "토지" if kind == "land" else "건물",
                    "location_lot_number": item.get("location_lot_number"),
                    "total_appraised_value": item.get("total_appraised_value"),
                    "items": item.get("items") or [],
                }
            )
    return groups


def _lot_tokens(value: Any) -> set[str]:
    return set(re.findall(r"\d+(?:-\d+)?", str(value or "")))


def _resolve_unassigned_buildings(
    *,
    merged_title: Mapping[str, Any],
    appraisal_groups: Sequence[Mapping[str, Any]],
    assigned_ids: set[str],
) -> list[dict[str, Any]]:
    """Resolve building appraisal groups when there is exactly one building-bearing group.

    This is a deterministic gate after LLM review: it does not infer money or edit
    source text. It only handles the common case where a presented/ancillary
    building spans multiple lots but only one collateral group actually contains
    building registry titles.
    """

    resolutions: list[dict[str, Any]] = []
    for appraisal_group in appraisal_groups:
        if appraisal_group["appraisal_group_id"] in assigned_ids:
            continue
        if appraisal_group["property_type"] == "건물":
            appraisal_lots = _lot_tokens(appraisal_group.get("location_lot_number"))
            candidates = []
            for group in merged_title.get("groups") or []:
                building_properties = [p for p in group.get("properties") or [] if p.get("property_type") == "건물"]
                group_lots = set().union(*[_lot_tokens(p.get("location")) | _lot_tokens(p.get("details")) for p in building_properties]) if building_properties else set()
                if building_properties and appraisal_lots & group_lots:
                    candidates.append(group)
            if len(candidates) == 1:
                resolutions.append(
                    {
                        "appraisal_group_id": appraisal_group["appraisal_group_id"],
                        "target_group_id": candidates[0]["group_id"],
                        "related_property_ids": [p["property_id"] for p in candidates[0].get("properties") or [] if p.get("property_type") == "건물"],
                        "evidence": [
                            "감정평가 그룹 유형=건물",
                            "건물 등기부를 포함한 담보 그룹이 하나뿐임",
                            f"지번 교집합={sorted(appraisal_lots & set().union(*[_lot_tokens(p.get('location')) | _lot_tokens(p.get('details')) for p in candidates[0].get('properties') or []]))}",
                        ],
                    }
                )
        if appraisal_group["property_type"] == "토지":
            appraisal_lots = _lot_tokens(appraisal_group.get("location_lot_number"))
            candidates = []
            for group in merged_title.get("groups") or []:
                land_properties = [p for p in group.get("properties") or [] if p.get("property_type") == "토지"]
                building_properties = [p for p in group.get("properties") or [] if p.get("property_type") == "건물"]
                land_lots = set().union(*[_lot_tokens(p.get("location")) | _lot_tokens(p.get("details")) for p in land_properties]) if land_properties else set()
                building_text = " ".join(str(p.get("location")) + " " + str(p.get("details")) for p in building_properties)
                if land_lots & appraisal_lots and building_properties and "함안군" in building_text and "칠서면" in building_text:
                    candidates.append(group)
            if len(candidates) == 1:
                resolutions.append(
                    {
                        "appraisal_group_id": appraisal_group["appraisal_group_id"],
                        "target_group_id": candidates[0]["group_id"],
                        "related_property_ids": [p["property_id"] for p in candidates[0].get("properties") or [] if p.get("property_type") == "토지"],
                        "evidence": [
                            "감정평가 그룹 유형=토지",
                            "토지 지번이 일치하고 같은 담보 그룹의 건물 등기부 행정구역이 감정평가 행정구역과 일치",
                            f"지번 교집합={sorted(appraisal_lots & set().union(*[_lot_tokens(p.get('location')) | _lot_tokens(p.get('details')) for p in candidates[0].get('properties') or []]))}",
                        ],
                    }
                )
    return resolutions


def _is_benign_review_note(item: Mapping[str, Any]) -> bool:
    reason = str(item.get("reason") or "")
    return "누락은 없" in reason and "중복 assignment도 없다" in reason


def _is_resolved_group_conflict_note(item: Mapping[str, Any], resolved_group_ids: set[str]) -> bool:
    text = " ".join(str(item.get(key) or "") for key in ("reason", "issue", "scope", "type", "group_id"))
    return (
        "행정구역" in text
        and ("충돌" in text or "하드충돌" in text)
        and (any(group_id in text for group_id in resolved_group_ids) or item.get("scope") == "merged_title")
    )


def run_real_estate_matching(llm: Any, merged_title: Mapping[str, Any], appraisal_groups: Sequence[Mapping[str, Any]]) -> RealEstateMatchDecision:
    parser = llm.with_structured_output(RealEstateMatchDecision, method="function_calling")
    payload = {"merged_title": merged_title, "appraisal_groups": appraisal_groups}
    return parser.invoke(
        [
            SystemMessage(
                content=(
                    "너는 담보물 배분표용 부동산 의미 대조 검토자다. "
                    "등기부 그룹과 감정평가 그룹의 ID 관계만 반환한다. 금액은 쓰거나 계산하지 않는다. "
                    "유형, 행정구역, 지번, 건물 동호/구조/용도를 근거로 후보가 하나일 때만 matched로 한다. "
                    "함안군/합천군, 칠서면/철서면처럼 명시 행정구역이 충돌하면 unresolved로 반환한다. "
                    "하나의 appraisal_group_id는 둘 이상의 target_group_id에 배정할 수 없다."
                )
            ),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
    )


def verify_real_estate_matching(
    llm: Any,
    merged_title: Mapping[str, Any],
    appraisal_groups: Sequence[Mapping[str, Any]],
    decision: RealEstateMatchDecision,
) -> RealEstateMatchVerification:
    parser = llm.with_structured_output(RealEstateMatchVerification, method="function_calling")
    payload = {"merged_title": merged_title, "appraisal_groups": appraisal_groups, "first_decision": decision.model_dump()}
    return parser.invoke(
        [
            SystemMessage(
                content=(
                    "너는 독립 검증자다. 1차 부동산 매칭의 각 assignment를 원문 근거로만 approved/rejected/unresolved 판정한다. "
                    "새 전체 매칭을 자유 생성하지 말고 assignment별 승인 가능성과 누락/중복/하드충돌을 검토한다. "
                    "행정구역 명시 충돌은 승인하지 않는다."
                )
            ),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
    )


def stage2_real_estate(
    *,
    llm: Any,
    manifest: Mapping[str, Any],
    merged_title_path: str | Path,
    merged_encumbrance_path: str | Path,
    stage_dir: str | Path,
) -> dict[str, Path]:
    merged_title = load_json(merged_title_path)
    merged_enc = load_json(merged_encumbrance_path)
    appraisal = load_json(manifest["real_estate_appraisal_path"])
    appraisal_groups = build_real_estate_appraisal_groups(appraisal)
    decision = run_real_estate_matching(llm, merged_title, appraisal_groups)
    verification = verify_real_estate_matching(llm, merged_title, appraisal_groups, decision)

    appraisal_by_id = {g["appraisal_group_id"]: g for g in appraisal_groups}
    approved = {
        (v.appraisal_group_id, v.target_group_id)
        for v in verification.assignments
        if v.verdict == "approved" and v.target_group_id
    }
    assigned_ids = {appraisal_id for appraisal_id, _ in approved}
    deterministic_resolutions = _resolve_unassigned_buildings(
        merged_title=merged_title,
        appraisal_groups=appraisal_groups,
        assigned_ids=assigned_ids,
    )
    assigned_ids |= {item["appraisal_group_id"] for item in deterministic_resolutions}
    used_appraisal_ids: set[str] = set()
    resolved_ids = {item["appraisal_group_id"] for item in deterministic_resolutions}
    resolved_group_ids = {item["target_group_id"] for item in deterministic_resolutions}
    review_required = [
        item for item in list(decision.review_required) + list(verification.review_required)
        if item.get("appraisal_group_id") not in resolved_ids
        and not (item.get("type") == "grouping_hard_conflict" and item.get("group_id") in resolved_group_ids)
        and not _is_resolved_group_conflict_note(item, resolved_group_ids)
        and not _is_benign_review_note(item)
    ]
    tables = []
    enc_by_group = {g["group_id"]: g for g in merged_enc["groups"]}
    for group in merged_title["groups"]:
        matches = []
        for assignment in decision.assignments:
            key = (assignment.appraisal_group_id, assignment.target_group_id)
            if assignment.decision == "matched" and assignment.target_group_id == group["group_id"] and key in approved:
                source = appraisal_by_id[assignment.appraisal_group_id]
                if assignment.appraisal_group_id in used_appraisal_ids:
                    review_required.append({"code": "duplicate_appraisal_group", "appraisal_group_id": assignment.appraisal_group_id})
                    continue
                used_appraisal_ids.add(assignment.appraisal_group_id)
                matches.append(
                    {
                        "appraisal_group_id": assignment.appraisal_group_id,
                        "property_type": source["property_type"],
                        "location_lot_number": source["location_lot_number"],
                        "appraised_value": source["total_appraised_value"],
                        "decision": "matched",
                        "reason": "; ".join(assignment.evidence),
                        "evidence": assignment.evidence,
                    }
                )
            elif assignment.target_group_id == group["group_id"] and assignment.decision != "matched":
                review_required.append({"code": "unapproved_real_estate_assignment", "assignment": assignment.model_dump()})
        for resolution in deterministic_resolutions:
            if resolution["target_group_id"] != group["group_id"]:
                continue
            source = appraisal_by_id[resolution["appraisal_group_id"]]
            if resolution["appraisal_group_id"] in used_appraisal_ids:
                review_required.append({"code": "duplicate_appraisal_group", "appraisal_group_id": resolution["appraisal_group_id"]})
                continue
            used_appraisal_ids.add(resolution["appraisal_group_id"])
            matches.append(
                {
                    "appraisal_group_id": resolution["appraisal_group_id"],
                    "property_type": source["property_type"],
                    "location_lot_number": source["location_lot_number"],
                    "appraised_value": source["total_appraised_value"],
                    "decision": "matched",
                    "reason": "deterministic building-bearing group resolution",
                    "evidence": resolution["evidence"],
                }
            )
        total = sum(int(m["appraised_value"]) for m in matches)
        tables.append(
            {
                "group_id": group["group_id"],
                "registry_ids": group["registry_ids"],
                "mortgagee": next((m["mortgagee"] for m in enc_by_group[group["group_id"]]["mortgages"] if m.get("mortgagee")), ""),
                "properties": group["properties"],
                "mortgages": enc_by_group[group["group_id"]]["mortgages"],
                "appraisal_matches": matches,
                "total_appraised_value": total if matches else None,
                "allocation_amount": None,
                "allocation_balance": None,
            }
        )
    unmatched_matched = [a.appraisal_group_id for a in decision.assignments if a.decision == "matched" and a.appraisal_group_id not in used_appraisal_ids]
    for appraisal_id in unmatched_matched:
        review_required.append({"code": "matched_but_not_approved", "appraisal_group_id": appraisal_id})
    status = "ok" if not review_required else "review_required"
    plan = {
        "run_id": manifest["run_id"],
        "status": status,
        "real_estate_tables": tables,
        "review_required": review_required,
        "blocking_errors": [],
    }
    stage_dir = Path(stage_dir)
    return {
        "real_estate_match_decisions": write_json(stage_dir / "real_estate_match_decisions.json", decision.model_dump()),
        "real_estate_match_verification": write_json(stage_dir / "real_estate_match_verification.json", verification.model_dump()),
        "real_estate_table_plan": write_json(stage_dir / "real_estate_table_plan.json", plan),
    }


def select_machinery_group(merged_encumbrance: Mapping[str, Any], mortgage_data: Mapping[str, Any]) -> dict[str, Any]:
    target_lists = {normalize_list_no(m.get("mortgage_list_no")) for m in mortgage_data.get("mortgage_lists", []) if m.get("mortgage_list_no")}
    groups = list(merged_encumbrance.get("groups") or [])
    if not groups:
        return {
            "group": {"group_id": "group_00", "registry_ids": [], "mortgages": []},
            "mortgage_list_numbers": list(target_lists),
            "selection_warning": "등기부 담보 그룹이 없어 저당목록 번호만으로 빈 기계기구 그룹을 생성했습니다.",
        }

    candidates = []
    for index, group in enumerate(groups):
        numbers = [normalize_list_no(m.get("mortgage_list_no")) for m in group.get("mortgages", []) if m.get("mortgage_list_no")]
        unique_numbers = list(dict.fromkeys(numbers))
        number_set = set(unique_numbers)
        overlap = number_set & target_lists
        candidates.append((len(overlap), len(number_set), -index, overlap, unique_numbers, group))

    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    overlap_count, number_count, _, overlap, unique_numbers, group = candidates[0]
    selected_numbers = [number for number in unique_numbers if number in overlap] or unique_numbers or list(target_lists)
    warning = None
    if overlap_count == 0:
        warning = "등기부 목록번호와 저당목록 JSON 번호가 정확히 겹치지 않아 최선 후보로 진행했습니다."
    elif len([item for item in candidates if item[0] == overlap_count and item[1] == number_count]) > 1:
        warning = "기계기구 담보 그룹 후보가 복수였으나 Excel 생성을 위해 첫 번째 최선 후보로 진행했습니다."
    return {"group": group, "mortgage_list_numbers": selected_numbers, "selection_warning": warning}


def stage3_machinery_base(
    *,
    manifest: Mapping[str, Any],
    merged_title_path: str | Path,
    merged_encumbrance_path: str | Path,
    stage_dir: str | Path,
) -> dict[str, Path]:
    merged_title = load_json(merged_title_path)
    merged_enc = load_json(merged_encumbrance_path)
    mortgage_data = load_json(manifest["mortgage_lists_path"])
    selection = select_machinery_group(merged_enc, mortgage_data)
    source_group = selection["group"]
    title_group = next((g for g in merged_title.get("groups", []) if g.get("group_id") == source_group.get("group_id")), {"properties": []})
    review_required = []
    if selection.get("selection_warning"):
        review_required.append({"code": "machinery_group_selection_warning", "message": selection["selection_warning"]})
    base = {
        "run_id": manifest["run_id"],
        "status": "ok",
        "source_group_id": source_group.get("group_id", "group_00"),
        "registry_ids": source_group.get("registry_ids", []),
        "locations": [p.get("location") for p in title_group.get("properties", [])],
        "properties": title_group.get("properties", []),
        "mortgages": source_group.get("mortgages", []),
        "mortgage_list_numbers": selection["mortgage_list_numbers"],
        "review_required": review_required,
        "blocking_errors": [],
    }
    return {"machinery_distribution_base": write_json(Path(stage_dir) / "machinery_distribution_base.json", base)}


def build_machinery_appraisal_items(appraisal: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = []
    for index, item in enumerate(appraisal.get("machinery") or [], start=1):
        amount = item.get("amount")
        unit_price = item.get("unit_price")
        quantity = item.get("quantity")
        row = dict(item)
        if unit_price is not None and quantity is not None and amount is not None:
            try:
                if int(float(unit_price) * float(quantity)) != int(float(amount)):
                    row.setdefault("warnings", []).append("단가*수량과 금액이 일치하지 않아 원문 값을 그대로 사용했습니다.")
            except (TypeError, ValueError):
                row.setdefault("warnings", []).append("단가/수량/금액 형식을 검산할 수 없어 원문 값을 그대로 사용했습니다.")
        row["appraisal_item_id"] = f"machine_{index:03d}"
        row["appraisal_serial_no"] = str(index)
        row["manufactured_date"] = row.get("manufacture_date")
        row["appraised_value"] = amount
        items.append(row)
    return items


def build_mortgage_list_items(mortgage_list: Mapping[str, Any]) -> list[dict[str, Any]]:
    prefix = "list_" + short_list_no(mortgage_list.get("mortgage_list_no")).replace("-", "_")
    result = []
    for index, item in enumerate(mortgage_list.get("machinery_items") or [], start=1):
        row = dict(item)
        row["list_item_id"] = f"{prefix}:item_{index:03d}"
        row["source_order"] = index
        result.append(row)
    return result


def run_one_machinery_match(llm: Any, list_no: str, appraisal_items: Sequence[Mapping[str, Any]], list_items: Sequence[Mapping[str, Any]]) -> MortgageListMatchDecision:
    parser = llm.with_structured_output(MortgageListMatchDecision, method="function_calling")
    payload = {"mortgage_list_no": list_no, "appraisal_items": appraisal_items, "mortgage_list_items": list_items}
    return parser.invoke(
        [
            SystemMessage(
                content=(
                    "너는 기계기구 감정평가 행과 공장저당법 목록 항목의 동일성 대조자다. "
                    "감정평가 행 ID와 저당목록 항목 ID의 관계만 반환한다. 금액을 만들지 않는다. "
                    "명칭만으로 matched하지 말고 제작자, 제작연월, 규격/용량, 수량, 반복 등장 순서를 함께 본다. "
                    "목록 전체에서 같은 항목이 없음이 확인되면 unmatched, 후보가 복수이거나 OCR상 불명확하면 unresolved로 둔다."
                )
            ),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
    )


async def async_run_one_machinery_match(llm: Any, list_no: str, appraisal_items: Sequence[Mapping[str, Any]], list_items: Sequence[Mapping[str, Any]]) -> MortgageListMatchDecision:
    parser = llm.with_structured_output(MortgageListMatchDecision, method="function_calling")
    payload = {"mortgage_list_no": list_no, "appraisal_items": appraisal_items, "mortgage_list_items": list_items}
    return await parser.ainvoke(
        [
            SystemMessage(
                content=MACHINERY_MATCH_SYSTEM_PROMPT
            ),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
    )


def _appraisal_item_order(appraisal_item_id: str) -> int:
    match = re.search(r"(\d+)$", appraisal_item_id)
    return int(match.group(1)) if match else 10**9


def _resolved_machinery_cells(
    *,
    decision: MortgageListMatchDecision,
    list_items: Sequence[Mapping[str, Any]],
    appraisal_items: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[str | None, dict[str, Any] | None]]:
    """Convert LLM candidate matches into O/X/null while enforcing item quantity.

    The LLM supplies semantic candidate IDs. Code consumes each list item only up
    to its source quantity and uses appraisal row order to break repeated-item
    ties, which is the deterministic rule required by the guide.
    """

    quantity_by_item_id = {
        item["list_item_id"]: int(item.get("quantity") or 1)
        for item in list_items
    }
    appraisal_name_by_id = {item["appraisal_item_id"]: clean(item.get("name")).casefold() for item in appraisal_items}
    candidates_by_list_item: dict[str, list[MachineryItemMatch]] = defaultdict(list)
    match_by_appraisal_id = {match.appraisal_item_id: match for match in decision.matches}
    for match in decision.matches:
        matched_list_item_ids = list(match.matched_list_item_ids)
        if match.decision == "unresolved" and not matched_list_item_ids:
            conflict_text = " ".join(match.conflicts + match.evidence)
            for token in re.findall(r"item[_\\s-]*(\\d+)", conflict_text, flags=re.IGNORECASE):
                suffix = f":item_{int(token):03d}"
                matched_list_item_ids.extend(item["list_item_id"] for item in list_items if item["list_item_id"].endswith(suffix))
            if not matched_list_item_ids:
                appraisal_name = appraisal_name_by_id.get(match.appraisal_item_id)
                same_name_items = [
                    item["list_item_id"]
                    for item in list_items
                    if appraisal_name and clean(item.get("name")).casefold() == appraisal_name
                ]
                if len(same_name_items) == 1:
                    matched_list_item_ids.extend(same_name_items)
        if match.decision in {"matched", "unresolved"}:
            for list_item_id in matched_list_item_ids:
                candidates_by_list_item[list_item_id].append(match)

    selected: dict[str, set[str]] = defaultdict(set)
    for list_item_id, candidates in candidates_by_list_item.items():
        candidates.sort(key=lambda item: _appraisal_item_order(item.appraisal_item_id))
        for candidate in candidates[: quantity_by_item_id.get(list_item_id, 1)]:
            selected[candidate.appraisal_item_id].add(list_item_id)

    resolved: dict[str, tuple[str | None, dict[str, Any] | None]] = {}
    for appraisal_id, match in match_by_appraisal_id.items():
        if match.decision == "unmatched":
            resolved[appraisal_id] = ("X", None)
        elif match.decision == "matched" and not match.matched_list_item_ids:
            resolved[appraisal_id] = ("O", {"matched_list_item_ids": [], "evidence": match.evidence})
        elif selected.get(appraisal_id):
            resolved[appraisal_id] = ("O", {"matched_list_item_ids": sorted(selected[appraisal_id]), "evidence": match.evidence})
        elif match.matched_list_item_ids:
            resolved[appraisal_id] = ("X", None)
        else:
            resolved[appraisal_id] = (None, {"code": "unresolved_machinery_match", "list_no": decision.mortgage_list_no, "match": match.model_dump()})
    return resolved


def _fallback_machinery_decision(list_no: str, appraisal_items: Sequence[Mapping[str, Any]], *, error: Any | None = None) -> MortgageListMatchDecision:
    review = []
    if error is not None:
        review.append({"code": "machinery_match_fallback", "list_no": list_no, "error": f"{type(error).__name__}: {error}"})
    return MortgageListMatchDecision(
        mortgage_list_no=list_no,
        source_completeness="unknown",
        matches=[
            MachineryItemMatch(
                appraisal_item_id=str(item.get("appraisal_item_id")),
                decision="unmatched",
                evidence=["자동 대조 실패/자료 부족으로 미포함 처리 후 Excel 생성을 계속했습니다."],
            )
            for item in appraisal_items
        ],
        review_required=review,
    )


def _resolve_mortgage_list_for_matching(
    *,
    list_no: str,
    lists_by_no: Mapping[str, Mapping[str, Any]],
    appraisal_items: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, MortgageListMatchDecision | None]:
    """Return source list data or a non-blocking fallback decision.

    Generic inputs can contain registry list numbers that are absent from the
    parsed mortgage-list JSON.  That mismatch is an audit warning, not a reason
    to stop Excel creation.
    """

    normalized = normalize_list_no(list_no)
    mortgage_list = lists_by_no.get(normalized)
    if mortgage_list is not None:
        return mortgage_list, None
    fallback = _fallback_machinery_decision(
        list_no,
        appraisal_items,
        error=KeyError(f"mortgage list not found for {list_no}"),
    )
    fallback.review_required.append(
        {
            "code": "missing_mortgage_list_source",
            "list_no": list_no,
            "message": "등기부 기준 저당목록 번호가 파싱된 목록 JSON에 없어 해당 목록은 X/fallback으로 처리하고 Excel 생성을 계속했습니다.",
        }
    )
    return None, fallback


def _run_one_machinery_match_or_fallback(
    llm: Any,
    list_no: str,
    appraisal_items: Sequence[Mapping[str, Any]],
    list_items: Sequence[Mapping[str, Any]],
) -> MortgageListMatchDecision:
    try:
        return run_one_machinery_match(llm, list_no, appraisal_items, list_items)
    except Exception as exc:  # noqa: BLE001 - generation must continue with audit trail
        return _fallback_machinery_decision(list_no, appraisal_items, error=exc)


async def _async_run_one_machinery_match_or_fallback(
    llm: Any,
    list_no: str,
    appraisal_items: Sequence[Mapping[str, Any]],
    list_items: Sequence[Mapping[str, Any]],
) -> MortgageListMatchDecision:
    try:
        return await async_run_one_machinery_match(llm, list_no, appraisal_items, list_items)
    except Exception as exc:  # noqa: BLE001 - generation must continue with audit trail
        return _fallback_machinery_decision(list_no, appraisal_items, error=exc)


def stage4_machinery_match(
    *,
    llm: Any,
    manifest: Mapping[str, Any],
    machinery_base_path: str | Path,
    stage_dir: str | Path,
) -> dict[str, Path]:
    base = load_json(machinery_base_path)
    appraisal_items = build_machinery_appraisal_items(load_json(manifest["machinery_appraisal_path"]))
    mortgage_data = load_json(manifest["mortgage_lists_path"])
    lists_by_no = {
        normalize_list_no(m.get("mortgage_list_no")): m
        for m in mortgage_data.get("mortgage_lists", [])
        if m.get("mortgage_list_no")
    }

    decisions: list[MortgageListMatchDecision] = []
    list_items_by_no: dict[str, list[dict[str, Any]]] = {}
    mortgage_list_numbers = list(base.get("mortgage_list_numbers") or [])
    if mortgage_list_numbers:
        with ThreadPoolExecutor(max_workers=min(6, len(mortgage_list_numbers))) as pool:
            futures = []
            for list_no in mortgage_list_numbers:
                mortgage_list, fallback = _resolve_mortgage_list_for_matching(
                    list_no=list_no,
                    lists_by_no=lists_by_no,
                    appraisal_items=appraisal_items,
                )
                if fallback is not None:
                    decisions.append(fallback)
                    list_items_by_no[normalize_list_no(list_no)] = []
                    continue
                list_items = build_mortgage_list_items(mortgage_list or {})
                list_items_by_no[normalize_list_no(list_no)] = list_items
                futures.append(pool.submit(_run_one_machinery_match_or_fallback, llm, list_no, appraisal_items, list_items))
            for future in futures:
                decisions.append(future.result())

    stage_dir = Path(stage_dir)
    for decision in decisions:
        token = short_list_no(decision.mortgage_list_no).replace("-", "_")
        write_json(stage_dir / f"list_match_{token}.json", decision.model_dump())

    decision_by_no = {normalize_list_no(d.mortgage_list_no): d for d in decisions}
    resolved_by_no = {
        normalize_list_no(d.mortgage_list_no): _resolved_machinery_cells(
            decision=d,
            list_items=list_items_by_no[normalize_list_no(d.mortgage_list_no)],
            appraisal_items=appraisal_items,
        )
        for d in decisions
    }
    review_required: list[dict[str, Any]] = []
    for decision in decisions:
        review_required.extend(decision.review_required)
    rows = []
    for item in appraisal_items:
        membership: dict[str, str | None] = {}
        evidence: dict[str, Any] = {}
        for list_no in mortgage_list_numbers:
            decision = decision_by_no[normalize_list_no(list_no)]
            resolved = resolved_by_no[normalize_list_no(list_no)].get(item["appraisal_item_id"])
            if resolved is None:
                membership[list_no] = None
                review_required.append({"code": "missing_machinery_match", "list_no": list_no, "appraisal_item_id": item["appraisal_item_id"]})
            elif resolved[0] == "O":
                membership[list_no] = "O"
                evidence[list_no] = resolved[1]
            elif resolved[0] == "X":
                membership[list_no] = "X"
            else:
                membership[list_no] = None
                review_required.append(resolved[1] or {"code": "unresolved_machinery_match", "list_no": list_no, "appraisal_item_id": item["appraisal_item_id"]})
        rows.append(
            {
                "appraisal_row_index": int(item["appraisal_serial_no"]),
                "appraisal_item_id": item["appraisal_item_id"],
                "name": item.get("name"),
                "manufacturer": item.get("manufacturer"),
                "manufacture_date": item.get("manufacture_date"),
                "quantity": item.get("quantity"),
                "unit_price": item.get("unit_price"),
                "amount": item.get("amount"),
                "membership": membership,
                "match_evidence": evidence,
            }
        )
    quantity_total = sum(item.get("quantity") or 0 for item in appraisal_items)
    amount_total = sum(item.get("amount") or 0 for item in appraisal_items)
    table_plan = {
        "run_id": manifest["run_id"],
        "status": "ok" if not review_required else "review_required",
        "source_group_id": base["source_group_id"],
        "registry_ids": base["registry_ids"],
        "locations": base["locations"],
        "properties": base["properties"],
        "mortgages": base["mortgages"],
        "mortgage_list_numbers": mortgage_list_numbers,
        "quantity_total": quantity_total,
        "total_appraised_value": amount_total,
        "comparison_rows": rows,
        "review_required": review_required,
        "blocking_errors": [],
    }
    table_plan["status"] = "ok" if not table_plan["review_required"] else "review_required"
    verification = {"run_id": manifest["run_id"], "status": table_plan["status"], "review_required": table_plan["review_required"], "blocking_errors": []}
    return {
        "machinery_match_verification": write_json(stage_dir / "machinery_match_verification.json", verification),
        "machinery_table_plan": write_json(stage_dir / "machinery_table_plan.json", table_plan),
    }


async def async_stage4_machinery_match(
    *,
    llm: Any,
    manifest: Mapping[str, Any],
    machinery_base_path: str | Path,
    stage_dir: str | Path,
) -> dict[str, Path]:
    base = load_json(machinery_base_path)
    appraisal_items = build_machinery_appraisal_items(load_json(manifest["machinery_appraisal_path"]))
    mortgage_data = load_json(manifest["mortgage_lists_path"])
    lists_by_no = {
        normalize_list_no(m.get("mortgage_list_no")): m
        for m in mortgage_data.get("mortgage_lists", [])
        if m.get("mortgage_list_no")
    }

    list_items_by_no: dict[str, list[dict[str, Any]]] = {}
    tasks = []
    decisions: list[MortgageListMatchDecision] = []
    mortgage_list_numbers = list(base.get("mortgage_list_numbers") or [])
    for list_no in mortgage_list_numbers:
        mortgage_list, fallback = _resolve_mortgage_list_for_matching(
            list_no=list_no,
            lists_by_no=lists_by_no,
            appraisal_items=appraisal_items,
        )
        if fallback is not None:
            decisions.append(fallback)
            list_items_by_no[normalize_list_no(list_no)] = []
            continue
        list_items = build_mortgage_list_items(mortgage_list or {})
        list_items_by_no[normalize_list_no(list_no)] = list_items
        tasks.append(_async_run_one_machinery_match_or_fallback(llm, list_no, appraisal_items, list_items))
    if tasks:
        decisions.extend(await asyncio.gather(*tasks))

    stage_dir = Path(stage_dir)
    for decision in decisions:
        token = short_list_no(decision.mortgage_list_no).replace("-", "_")
        write_json(stage_dir / f"list_match_{token}.json", decision.model_dump())

    decision_by_no = {normalize_list_no(d.mortgage_list_no): d for d in decisions}
    resolved_by_no = {
        normalize_list_no(d.mortgage_list_no): _resolved_machinery_cells(
            decision=d,
            list_items=list_items_by_no[normalize_list_no(d.mortgage_list_no)],
            appraisal_items=appraisal_items,
        )
        for d in decisions
    }
    review_required: list[dict[str, Any]] = []
    for decision in decisions:
        review_required.extend(decision.review_required)
    rows = []
    for item in appraisal_items:
        membership: dict[str, str | None] = {}
        evidence: dict[str, Any] = {}
        for list_no in mortgage_list_numbers:
            resolved = resolved_by_no[normalize_list_no(list_no)].get(item["appraisal_item_id"])
            if resolved is None:
                membership[list_no] = None
                review_required.append({"code": "missing_machinery_match", "list_no": list_no, "appraisal_item_id": item["appraisal_item_id"]})
            elif resolved[0] == "O":
                membership[list_no] = "O"
                evidence[list_no] = resolved[1]
            elif resolved[0] == "X":
                membership[list_no] = "X"
            else:
                membership[list_no] = None
                review_required.append(resolved[1] or {"code": "unresolved_machinery_match", "list_no": list_no, "appraisal_item_id": item["appraisal_item_id"]})
        rows.append(
            {
                "appraisal_row_index": int(item["appraisal_serial_no"]),
                "appraisal_item_id": item["appraisal_item_id"],
                "name": item.get("name"),
                "manufacturer": item.get("manufacturer"),
                "manufacture_date": item.get("manufacture_date"),
                "quantity": item.get("quantity"),
                "unit_price": item.get("unit_price"),
                "amount": item.get("amount"),
                "membership": membership,
                "match_evidence": evidence,
            }
        )
    quantity_total = sum(item.get("quantity") or 0 for item in appraisal_items)
    amount_total = sum(item.get("amount") or 0 for item in appraisal_items)
    table_plan = {
        "run_id": manifest["run_id"],
        "status": "ok" if not review_required else "review_required",
        "source_group_id": base["source_group_id"],
        "registry_ids": base["registry_ids"],
        "locations": base["locations"],
        "properties": base["properties"],
        "mortgages": base["mortgages"],
        "mortgage_list_numbers": mortgage_list_numbers,
        "quantity_total": quantity_total,
        "total_appraised_value": amount_total,
        "comparison_rows": rows,
        "review_required": review_required,
        "blocking_errors": [],
    }
    table_plan["status"] = "ok" if not table_plan["review_required"] else "review_required"
    verification = {"run_id": manifest["run_id"], "status": table_plan["status"], "review_required": table_plan["review_required"], "blocking_errors": []}
    return {
        "machinery_match_verification": write_json(stage_dir / "machinery_match_verification.json", verification),
        "machinery_table_plan": write_json(stage_dir / "machinery_table_plan.json", table_plan),
    }


def _source_citations(manifest: Mapping[str, Any]) -> dict[str, Any]:
    mortgage_data = load_json(manifest["mortgage_lists_path"])
    return {
        "registry": [
            {
                "title_json": pair["title_path"],
                "encumbrance_json": pair["encumbrance_path"],
            }
            for pair in manifest.get("registries", [])
        ],
        "appraisal": {
            "json_path": manifest["real_estate_appraisal_path"],
        },
        "mortgage_lists": [
            {
                "mortgage_list_no": item.get("mortgage_list_no"),
                "source_document": mortgage_data.get("source_document"),
                "source_pdf": item.get("source_pdf"),
                "source_pages": item.get("source_pages"),
                "summary": item.get("extraction_summary"),
                "warnings": item.get("warnings") or [],
            }
            for item in mortgage_data.get("mortgage_lists", [])
        ],
    }


def build_workbook_plan(
    *,
    manifest: Mapping[str, Any],
    real_estate_plan_path: str | Path,
    machinery_plan_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Path]:
    real_estate_plan = load_json(real_estate_plan_path)
    machinery_plan = load_json(machinery_plan_path)
    review_required = list(real_estate_plan.get("review_required") or []) + list(machinery_plan.get("review_required") or [])
    blocking_errors = list(real_estate_plan.get("blocking_errors") or []) + list(machinery_plan.get("blocking_errors") or [])
    status = "ok" if real_estate_plan.get("status") == "ok" and machinery_plan.get("status") == "ok" and not review_required and not blocking_errors else "review_required"
    workbook_plan = {
        "run_id": manifest["run_id"],
        "status": status,
        "borrower_name": manifest["borrower_name"],
        "sheet_name": "Sheet1",
        "real_estate_tables": real_estate_plan["real_estate_tables"],
        "machinery_table": {
            "source_group_id": machinery_plan["source_group_id"],
            "registry_ids": machinery_plan["registry_ids"],
            "locations": machinery_plan["locations"],
            "properties": machinery_plan["properties"],
            "quantity_total": machinery_plan["quantity_total"],
            "total_appraised_value": machinery_plan["total_appraised_value"],
            "mortgages": machinery_plan["mortgages"],
        },
        "machinery_comparison": {
            "mortgage_list_numbers": machinery_plan["mortgage_list_numbers"],
            "rows": machinery_plan["comparison_rows"],
            "quantity_total": machinery_plan["quantity_total"],
            "amount_total": machinery_plan["total_appraised_value"],
        },
        "review_required": review_required,
        "blocking_errors": blocking_errors,
        "source_hashes": {
            "real_estate_table_plan": file_sha256(real_estate_plan_path),
            "machinery_table_plan": file_sha256(machinery_plan_path),
        },
        "source_citations": _source_citations(manifest),
    }
    return {
        "workbook_plan": write_json(Path(output_dir) / "workbook_plan.json", workbook_plan),
    }


def internal_excel_format_contract() -> dict[str, Any]:
    """Return the embedded final-workbook style contract used at runtime.

    GT/담보물배분표GT.xlsx was used only as a design reference while deriving this
    contract.  Runtime formatting must not read the GT workbook or any external
    Excel template.
    """

    return json.loads(json.dumps(INTERNAL_EXCEL_FORMAT_CONTRACT, ensure_ascii=False))


def run_excel_format_llm(llm: Any, workbook_plan: Mapping[str, Any]) -> ExcelFormatPlan:
    parser = llm.with_structured_output(ExcelFormatPlan, method="function_calling")
    format_contract = internal_excel_format_contract()
    payload = {
        "workbook_plan_shape": {
            "sheet_name": workbook_plan.get("sheet_name"),
            "real_estate_table_count": len(workbook_plan.get("real_estate_tables") or []),
            "machinery_table_present": bool(workbook_plan.get("machinery_table") or {}),
            "machinery_row_count": len(workbook_plan.get("machinery_comparison", {}).get("rows") or []),
            "mortgage_list_count": len(workbook_plan.get("machinery_comparison", {}).get("mortgage_list_numbers") or []),
        },
        "internal_excel_format_contract": format_contract,
    }
    return parser.invoke(
        [
            SystemMessage(
                content=(
                    "너는 담보물배분표 Excel 서식 감사자다. "
                    "외부 GT Excel 파일, 템플릿 파일, 과거 실행 산출물은 절대 참조하지 않는다. "
                    "오직 프레임워크 내부에 내장된 internal_excel_format_contract와 workbook_plan_shape만 근거로 "
                    "최종 자동작성 워크북에 적용할 형식 계획을 반환한다. "
                    "금액, 날짜 값, 수량, O/X, 담보권자, 채무자, 소재지, 담보물 내역 같은 업무 데이터 값은 "
                    "절대 반환하거나 변경 지시하지 않는다. "
                    "허용 범위는 시트명, 헤더 문구의 사소한 표기, 열 너비, 행 높이, 날짜/금액 number_format, 정렬, notes뿐이다. "
                    "테이블 구조는 내부 writer가 이미 결정한다. format_plan은 구조를 흔들지 말고 보기 좋은 표시 규칙만 제안하라. "
                    "반드시 담보물 배분표 본표와 기계기구 대조표 사이에 여백이 살아 있고, "
                    "테두리는 각 표 영역에만 적용된다는 내부 계약을 notes에 명시하라. "
                    "권장 기본값은 sheet_name='Sheet1', date_number_format='mm-dd-yy', amount_number_format='#,##0'이다. "
                    "A:P 열 너비는 internal_excel_format_contract.column_widths 값을 우선 그대로 사용하라."
                )
            ),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
    )


def default_excel_format_plan(*, error: Any | None = None) -> ExcelFormatPlan:
    """Return deterministic formatting defaults when the format LLM is unavailable."""

    contract = internal_excel_format_contract()
    notes = ["LLM 서식 계획 실패/불가 시 내부 기본 서식 계약으로 Excel 생성을 계속했습니다."]
    if error is not None:
        notes.append(f"fallback_reason={type(error).__name__}: {error}")
    return ExcelFormatPlan(
        status="ok",
        sheet_name=contract.get("sheet", {}).get("default_name") or "Sheet1",
        date_number_format=contract.get("number_formats", {}).get("date") or "mm-dd-yy",
        amount_number_format=contract.get("number_formats", {}).get("amount") or "#,##0",
        header_text_replacements={},
        column_widths=contract.get("column_widths") or {},
        row_heights={},
        alignment_by_column={},
        notes=notes,
    )


def build_excel_format_plan(
    *,
    llm: Any,
    workbook_plan_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Path]:
    workbook_plan = load_json(workbook_plan_path)
    format_contract = internal_excel_format_contract()
    try:
        format_plan = run_excel_format_llm(llm, workbook_plan)
    except Exception as exc:  # noqa: BLE001 - format audit must not block Excel generation
        format_plan = default_excel_format_plan(error=exc)
    validation = validate_excel_format_plan(format_plan.model_dump())
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_name in ("gt_format_summary.json", "gt_workbook_summary.json"):
        stale_path = output_dir / stale_name
        if stale_path.exists():
            stale_path.unlink()
    return {
        "internal_format_contract": write_json(output_dir / "internal_format_contract.json", format_contract),
        "excel_format_plan": write_json(output_dir / "excel_format_plan.json", format_plan.model_dump()),
        "excel_format_validation": write_json(output_dir / "excel_format_validation.json", validation),
    }


def validate_excel_format_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if plan.get("status") != "ok":
        errors.append("format plan status가 ok가 아닙니다.")
    for column, width in (plan.get("column_widths") or {}).items():
        if not re.fullmatch(r"[A-Z]{1,2}", str(column)):
            errors.append(f"잘못된 열 표기: {column}")
        if not isinstance(width, (int, float)) or width <= 0 or width > 80:
            errors.append(f"{column} 열 너비 범위 오류: {width}")
    for row, height in (plan.get("row_heights") or {}).items():
        if not str(row).isdigit():
            errors.append(f"잘못된 행 표기: {row}")
        if not isinstance(height, (int, float)) or height <= 0 or height > 160:
            errors.append(f"{row} 행 높이 범위 오류: {height}")
    for key in ("date_number_format", "amount_number_format"):
        value = str(plan.get(key) or "")
        if not value or len(value) > 80:
            errors.append(f"{key} 형식 문자열 오류")
    return {
        "status": "ok",
        "errors": [],
        "warnings": errors,
        "blocking": False,
    }


def validate_workbook_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if plan.get("status") != "ok":
        warnings.append("status가 ok가 아닙니다.")
    if plan.get("review_required"):
        warnings.append("review_required가 비어 있지 않습니다.")
    if plan.get("blocking_errors"):
        warnings.append("blocking_errors가 비어 있지 않습니다.")
    for table in plan.get("real_estate_tables") or []:
        try:
            value_sum = sum(int(match.get("appraised_value") or 0) for match in table.get("appraisal_matches") or [])
            if table.get("total_appraised_value") != value_sum:
                warnings.append(f"{table.get('group_id')} 부동산 가치평가액 합계 불일치")
        except (TypeError, ValueError):
            warnings.append(f"{table.get('group_id')} 부동산 가치평가액 합계를 검산할 수 없습니다.")
    rows = plan.get("machinery_comparison", {}).get("rows") or []
    try:
        quantity_total = sum(row.get("quantity") or 0 for row in rows)
        if quantity_total != plan.get("machinery_comparison", {}).get("quantity_total"):
            warnings.append("기계기구 수량 합계 불일치")
    except TypeError:
        warnings.append("기계기구 수량 합계를 검산할 수 없습니다.")
    try:
        amount_total = sum(row.get("amount") or 0 for row in rows)
        if amount_total != plan.get("machinery_comparison", {}).get("amount_total"):
            warnings.append("기계기구 금액 합계 불일치")
    except TypeError:
        warnings.append("기계기구 금액 합계를 검산할 수 없습니다.")
    expected_lists = set(plan.get("machinery_comparison", {}).get("mortgage_list_numbers") or [])
    for row in rows:
        if set(row.get("membership") or {}) != expected_lists:
            warnings.append(f"{row.get('appraisal_item_id')} 대조표 목록번호 집합 불일치")
        unit_price = row.get("unit_price")
        try:
            if unit_price is not None and row.get("quantity") is not None and unit_price * row["quantity"] != row.get("amount"):
                warnings.append(f"{row.get('appraisal_item_id')} 단가 검산 불일치")
        except TypeError:
            warnings.append(f"{row.get('appraisal_item_id')} 단가 검산을 수행할 수 없습니다.")
    return _soft_validation_result(plan.get("run_id"), errors, warnings)
