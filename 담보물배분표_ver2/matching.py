"""Deterministic machinery matching utilities."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


def normalize_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    return re.sub(r"[\(\)\[\],._\-/·]", "", text)


def normalize_list_no(value: Any) -> str:
    match = re.search(r"(\d{4})\s*-\s*(\d+)", str(value or ""))
    if match:
        return f"{match.group(1)}-{int(match.group(2))}"
    return normalize_text(value)


def _score(appraisal_item: Mapping[str, Any], mortgage_item: Mapping[str, Any]) -> tuple[int, list[str], list[str]]:
    matched: list[str] = []
    conflicts: list[str] = []
    fields = ["name", "manufacturer", "manufactured_date", "quantity"]
    for field in fields:
        left = appraisal_item.get(field)
        right = mortgage_item.get(field)
        if left in (None, "") or right in (None, ""):
            continue
        if field == "quantity":
            same = str(left) == str(right)
        else:
            same = normalize_text(left) == normalize_text(right)
        if same:
            matched.append(field)
        else:
            conflicts.append(field)
    return len(matched) * 2 - len(conflicts) * 3, matched, conflicts


def match_machinery(
    appraisal: Mapping[str, Any],
    mortgage_assets: Mapping[str, Any],
    mortgage_list_order: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Create the intermediate machinery_matches.json payload.

    Direct `appraisal_serial_no` links from the mortgage-list extraction are used
    first. Otherwise, conservative normalized field matching is applied.
    """

    appraisal_items = list(appraisal.get("machinery_items") or [])
    lists = list(mortgage_assets.get("mortgage_lists") or [])
    if mortgage_list_order:
        order = {normalize_list_no(no): idx for idx, no in enumerate(mortgage_list_order)}
        lists.sort(key=lambda item: order.get(normalize_list_no(item.get("mortgage_list_no")), 10**9))

    rows = []
    matched_mortgage_keys: set[tuple[str, str]] = set()
    warnings: list[str] = []

    for app_item in appraisal_items:
        serial = str(app_item.get("appraisal_serial_no") or app_item.get("serial_no") or "")
        memberships = []
        for mlist in lists:
            list_no = mlist.get("mortgage_list_no", "")
            found = None
            reason = ""
            matched_fields: list[str] = []
            conflicts: list[str] = []
            for m_item in mlist.get("machinery_items") or []:
                if str(m_item.get("appraisal_serial_no") or "") == serial and serial:
                    found = m_item
                    matched_fields = ["appraisal_serial_no"]
                    reason = "저당목록 추출 결과의 감정평가서 일련번호 연결값 일치"
                    break
            if found is None:
                candidates = []
                for m_item in mlist.get("machinery_items") or []:
                    score, fields, bad = _score(app_item, m_item)
                    if score >= 4 and not bad:
                        candidates.append((score, fields, bad, m_item))
                if len(candidates) == 1:
                    _, matched_fields, conflicts, found = candidates[0]
                    reason = "명칭·제작자 등 정규화 필드 일치"
                elif len(candidates) > 1:
                    warnings.append(f"{serial}번 품목: {list_no}에서 복수 후보로 자동 확정하지 않음")
            if found is not None:
                matched_mortgage_keys.add((normalize_list_no(list_no), str(found.get("list_serial_no") or found.get("appraisal_serial_no") or "")))
                memberships.append(
                    {
                        "mortgage_list_no": list_no,
                        "status": "matched",
                        "list_serial_no": found.get("list_serial_no"),
                        "matched_fields": matched_fields,
                        "conflicting_fields": conflicts,
                        "reason": reason,
                    }
                )
        rows.append(
            {
                "appraisal_serial_no": serial,
                "appraisal_item": app_item,
                "mortgage_memberships": memberships,
            }
        )

    unmatched_appraisal_items = [row["appraisal_item"] for row in rows if not row["mortgage_memberships"]]
    unmatched_mortgage_items = []
    for mlist in lists:
        list_key = normalize_list_no(mlist.get("mortgage_list_no"))
        for item in mlist.get("machinery_items") or []:
            item_key = str(item.get("list_serial_no") or item.get("appraisal_serial_no") or "")
            if (list_key, item_key) not in matched_mortgage_keys:
                unmatched_mortgage_items.append({"mortgage_list_no": mlist.get("mortgage_list_no"), **item})

    return {
        "schema_version": 1,
        "rows": rows,
        "unmatched_appraisal_items": unmatched_appraisal_items,
        "unmatched_mortgage_items": unmatched_mortgage_items,
        "warnings": warnings,
    }


def save_matches(matches: Mapping[str, Any], output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(matches, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path
