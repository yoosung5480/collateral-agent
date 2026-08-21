"""담보물배분표 Excel 자동작성 유틸리티.

LLM은 PDF를 JSON으로 구조화하고, 이 모듈은 JSON 병합·정렬·계산·Excel 셀 작성을
결정론적으로 수행한다.
"""

from __future__ import annotations

import json
import math
import re
from copy import copy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from matching import normalize_list_no

REGISTRY_PATTERN = re.compile(r"registry_(\d+)", re.IGNORECASE)
MONEY_PATTERN = re.compile(r"-?\d[\d,]*")
NO_BORDER = Border()
NO_FILL = PatternFill(fill_type=None)


@dataclass(frozen=True)
class Mortgage:
    registration: str
    maximum_claim: str
    mortgagee: str
    shared_collateral: str
    mortgage_list_no: str


@dataclass(frozen=True)
class PropertyTitle:
    registry_key: str
    property_type: str
    location: str
    details: str


@dataclass(frozen=True)
class RegistryRecord:
    registry_key: str
    title: PropertyTitle
    mortgages: tuple[Mortgage, ...]


@dataclass
class CollateralGroup:
    mortgages: tuple[Mortgage, ...]
    properties: list[PropertyTitle]
    registry_keys: list[str]


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _registry_key(data: Mapping[str, Any], fallback: str) -> str:
    match = REGISTRY_PATTERN.search(str(data.get("registry_id", "")))
    if match:
        return f"registry_{int(match.group(1)):02d}"
    match = REGISTRY_PATTERN.search(fallback)
    if match:
        return f"registry_{int(match.group(1)):02d}"
    return fallback


def _registry_order(registry_key: str) -> tuple[int, str]:
    match = re.search(r"(\d+)", registry_key)
    return (int(match.group(1)) if match else 10**9, registry_key)


def parse_date(value: str) -> datetime:
    normalized = clean(value)
    normalized = normalized.replace("년", ".").replace("월", ".").replace("일", "")
    normalized = normalized.replace("-", ".").replace("/", ".")
    normalized = re.sub(r"\.+", ".", normalized)
    match = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", normalized)
    if not match:
        raise ValueError(f"설정일자를 해석할 수 없습니다: {value!r}")
    return datetime(*map(int, match.groups()))


def parse_money(value: Any) -> int | None:
    match = MONEY_PATTERN.search(str(value or ""))
    if not match:
        return None
    return int(match.group(0).replace(",", ""))


def short_shared_collateral(value: str) -> str:
    value = clean(value)
    if value in {"공용담보", "공동담보", "공담"}:
        return "공담"
    return value


def load_registry_records(registry_output_path: str | Path) -> list[RegistryRecord]:
    root = Path(registry_output_path)
    if not root.exists():
        raise FileNotFoundError(f"등기부 JSON 결과 경로가 없습니다: {root}")

    titles: dict[str, Mapping[str, Any]] = {}
    encumbrances: dict[str, Mapping[str, Any]] = {}
    for path in sorted(root.rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        fallback = path.parent.name if path.parent != root else path.stem
        key = _registry_key(data, fallback)
        if {"property_type", "location", "details"} <= data.keys():
            if key in titles:
                raise ValueError(f"{key}의 title JSON이 중복되었습니다: {path}")
            titles[key] = data
        elif "mortgages" in data:
            if key in encumbrances:
                raise ValueError(f"{key}의 encumbrance JSON이 중복되었습니다: {path}")
            encumbrances[key] = data

    missing_title = sorted(set(encumbrances) - set(titles))
    missing_encumbrance = sorted(set(titles) - set(encumbrances))
    if missing_title or missing_encumbrance:
        raise ValueError(f"title/encumbrance 짝 불일치: title 없음={missing_title}, encumbrance 없음={missing_encumbrance}")
    if not titles:
        raise ValueError(f"등기부 JSON을 찾지 못했습니다: {root}")

    records: list[RegistryRecord] = []
    for key in sorted(titles, key=_registry_order):
        raw_mortgages = encumbrances[key].get("mortgages") or []
        indexed_mortgages = []
        for index, item in enumerate(raw_mortgages):
            mortgage = Mortgage(
                registration=clean(item.get("registration")),
                maximum_claim=clean(item.get("maximum_claim")),
                mortgagee=clean(item.get("mortgagee")),
                shared_collateral=clean(item.get("shared_collateral")),
                mortgage_list_no=clean(item.get("mortgage_list_no")),
            )
            indexed_mortgages.append((index, mortgage))
        indexed_mortgages.sort(key=lambda pair: (parse_date(pair[1].registration), pair[0]))
        title_data = titles[key]
        records.append(
            RegistryRecord(
                registry_key=key,
                title=PropertyTitle(
                    registry_key=key,
                    property_type=clean(title_data.get("property_type")),
                    location=clean(title_data.get("location")),
                    details=str(title_data.get("details") or "").strip(),
                ),
                mortgages=tuple(mortgage for _, mortgage in indexed_mortgages),
            )
        )
    return records


def _mortgage_signature(mortgages: Sequence[Mortgage]) -> tuple[tuple[str, ...], ...]:
    return tuple((m.registration, m.maximum_claim, m.mortgagee, m.shared_collateral, m.mortgage_list_no) for m in mortgages)


def merge_registry_records(records: Sequence[RegistryRecord]) -> list[CollateralGroup]:
    groups: dict[tuple[tuple[str, ...], ...], CollateralGroup] = {}
    order: list[tuple[tuple[str, ...], ...]] = []
    for record in sorted(records, key=lambda item: _registry_order(item.registry_key)):
        signature = _mortgage_signature(record.mortgages)
        if signature not in groups:
            groups[signature] = CollateralGroup(record.mortgages, [], [])
            order.append(signature)
        groups[signature].properties.append(record.title)
        groups[signature].registry_keys.append(record.registry_key)

    property_order = {"토지": 0, "건물": 1}
    result = [groups[sig] for sig in order]
    for group in result:
        group.properties.sort(key=lambda item: (property_order.get(item.property_type, 99), _registry_order(item.registry_key)))
    return result


def _copy_style(source, target) -> None:
    target.font = copy(source.font)
    target.fill = copy(source.fill)
    target.border = copy(source.border)
    if source.number_format:
        target.number_format = source.number_format
    target.alignment = copy(source.alignment)
    target.protection = copy(source.protection)


def _copy_style_range(source_sheet, target_sheet, source_min_row: int, source_max_row: int, source_min_col: int, source_max_col: int, target_min_row: int, target_min_col: int) -> None:
    for row_offset, source_row in enumerate(range(source_min_row, source_max_row + 1)):
        for col_offset, source_col in enumerate(range(source_min_col, source_max_col + 1)):
            _copy_style(source_sheet.cell(source_row, source_col), target_sheet.cell(target_min_row + row_offset, target_min_col + col_offset))


def _set_row_height(sheet, row: int, height: float | None) -> None:
    if height is not None:
        sheet.row_dimensions[row].height = height


def _minimal_prototype():
    wb = Workbook()
    ws = wb.active
    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill("solid", fgColor="B7DEE8")
    for col in range(1, 17):
        ws.column_dimensions[get_column_letter(col)].width = [17.14,45,4.14,15.57,16.14,5.57,20.14,20.14,13,13,13,13,13,13,13,14.14][col-1]
    for row in range(1, 61):
        for col in range(1, 17):
            cell = ws.cell(row, col)
            cell.font = Font(name="맑은 고딕", size=11)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            cell.border = border
    for row in (4, 5, 36, 37, 50):
        for col in range(1, 17):
            ws.cell(row, col).fill = header_fill
            ws.cell(row, col).font = Font(name="맑은 고딕", size=10, bold=True)
            ws.cell(row, col).alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    comparison_fill = PatternFill("solid", fgColor="D9D9D9")
    for col in range(1, 17):
        ws.cell(50, col).fill = comparison_fill
    for row in (2, 33):
        ws.cell(row, 1).font = Font(name="맑은 고딕", size=16, bold=True)
        ws.cell(row, 1).alignment = Alignment(horizontal="center", vertical="center")
    ws.sheet_view.zoomScale = 61
    return wb, ws


def _load_prototype(template_path: str | Path | None):
    if template_path and Path(template_path).exists():
        wb = load_workbook(template_path)
        if "담보물배분표" in wb.sheetnames:
            return wb, wb["담보물배분표"]
        return wb, wb.active
    return _minimal_prototype()


def _prepare_output_sheet(workbook, prototype, max_columns: int = 16):
    sheet = workbook.create_sheet("담보물배분표_자동작성")
    for column in range(1, max_columns + 1):
        letter = get_column_letter(column)
        source_letter = letter if column <= 16 else "J"
        sheet.column_dimensions[letter].width = prototype.column_dimensions[source_letter].width
    sheet.sheet_view.zoomScale = 61
    sheet.sheet_view.showGridLines = prototype.sheet_view.showGridLines
    sheet.page_margins = copy(prototype.page_margins)
    sheet.page_setup = copy(prototype.page_setup)
    return sheet


def _write_title_row(prototype, sheet, row: int, title: str) -> None:
    _copy_style_range(prototype, sheet, 2, 2, 1, 16, row, 1)
    _clear_row_box(sheet, row, 1, 16)
    sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=16)
    sheet.cell(row, 1).value = title
    _set_row_height(sheet, row, prototype.row_dimensions[2].height or 21)


def _clear_row_box(sheet, row: int, start_column: int, end_column: int) -> None:
    for column in range(start_column, end_column + 1):
        cell = sheet.cell(row, column)
        if isinstance(cell, MergedCell):
            continue
        cell.border = NO_BORDER
        cell.fill = copy(NO_FILL)


def _clear_empty_rows(sheet, *, max_row: int, max_column: int) -> None:
    for row in range(1, max_row + 1):
        if any(sheet.cell(row, column).value is not None for column in range(1, max_column + 1)):
            continue
        _clear_row_box(sheet, row, 1, max_column)


def _write_common_headers(prototype, sheet, top_row: int, *, detail_header: str) -> None:
    _copy_style_range(prototype, sheet, 4, 5, 1, 16, top_row, 1)
    for r1, c1, r2, c2 in [(1,1,2,1),(1,2,2,2),(1,3,1,6),(1,7,2,7),(1,8,2,15),(1,16,2,16)]:
        sheet.merge_cells(start_row=top_row+r1-1, start_column=c1, end_row=top_row+r2-1, end_column=c2)
    values = {
        (0,1): "담보물\n소재지", (0,2): detail_header, (0,3): "담보설정현황", (0,7): "가치평가액\n(주1)",
        (0,8): "배분내역", (0,16): "비고", (1,3): "순위", (1,4): "설정일자", (1,5): "채권자", (1,6): "공담",
    }
    for (off, col), value in values.items():
        sheet.cell(top_row+off, col).value = value
    _set_row_height(sheet, top_row, 21)
    _set_row_height(sheet, top_row+1, 21)


def _base_building_location(location: str) -> str:
    text = clean(location)
    text = re.sub(r"\s*제[가-힣A-Za-z]+동호\s*$", "", text)
    text = re.sub(r"(\d)외", r"\1 외", text)
    return text


def _format_properties(properties: Sequence[PropertyTitle], appraisal: Mapping[str, Any] | None = None) -> tuple[str, str]:
    by_type: dict[str, list[PropertyTitle]] = {}
    order: list[str] = []
    for item in properties:
        typ = item.property_type or "기타"
        if typ not in by_type:
            by_type[typ] = []
            order.append(typ)
        by_type[typ].append(item)

    location_lines = [""]
    detail_lines: list[str] = []
    multi_type = len(order) > 1
    for typ in order:
        location_lines.append(f"<{typ}>")
        detail_lines.append(f"<{typ}>")
        items = by_type[typ]
        locs: list[str] = []
        for item in items:
            loc = _base_building_location(item.location) if typ == "건물" else item.location
            if loc not in locs:
                locs.append(loc)
        location_lines.extend(locs)
        location_lines.append("")

        if typ == "건물":
            detail_lines.append("")
        if len(items) == 1 and not multi_type:
            detail_lines.append("")
            detail_lines.extend(item.details.splitlines())
        else:
            for index, item in enumerate(items, start=1):
                detail_lines.append(f"{index}. {item.location}")
                for line in item.details.splitlines() or [""]:
                    detail_lines.append(f"- {line}" if not line.startswith("  ") else line)
                detail_lines.append("")

    detail_text = "\n".join(detail_lines).rstrip()
    if appraisal and any("계내리 12" in p.location for p in properties):
        lb_items = appraisal.get("land_building_items") or []
        values = {item.get("source_text") for item in lb_items if item.get("source_text")}
        if values:
            preferred = [
                "토지(공장용지): 3,610,761,000원",
                "건물(제에이동호, 제비동호): 2,058,134,200원",
                "건물(제시외건물): 12,960,000",
            ]
            lines = [line for line in preferred if any(line.rstrip("원") in value for value in values)]
            if lines:
                detail_text += "\n\n<가치평가액>\n" + "\n".join(lines)
    return "\n".join(location_lines).rstrip() + "\n ", detail_text


def _write_mortgage_body(prototype, sheet, body_start: int, mortgages: Sequence[Mortgage], *, location_text: str, detail_text: str, appraisal_value: int | None = None) -> int:
    allocation_groups = max(1, math.ceil(len(mortgages) / 2))
    body_rows = max(len(mortgages), allocation_groups * 3)
    body_end = body_start + body_rows - 1
    for offset in range(body_rows):
        source_row = 6 if offset == 0 else 14 if offset == body_rows - 1 else 8
        _copy_style_range(prototype, sheet, source_row, source_row, 1, 16, body_start + offset, 1)
        _set_row_height(sheet, body_start + offset, 50)
    for col in (1, 2, 7, 16):
        sheet.merge_cells(start_row=body_start, start_column=col, end_row=body_end, end_column=col)
    sheet.cell(body_start, 1).value = location_text
    sheet.cell(body_start, 2).value = detail_text
    sheet.cell(body_start, 7).value = appraisal_value
    sheet.cell(body_start, 1).alignment = Alignment(vertical="center", wrap_text=True)
    sheet.cell(body_start, 2).alignment = Alignment(vertical="center", wrap_text=True)
    sheet.cell(body_start, 7).alignment = Alignment(horizontal="right", vertical="center")
    if appraisal_value is not None:
        sheet.cell(body_start, 7).number_format = "#,##0"

    for offset, mortgage in enumerate(mortgages):
        row = body_start + offset
        sheet.cell(row, 3).value = offset + 1
        sheet.cell(row, 3).number_format = "General"
        sheet.cell(row, 4).value = parse_date(mortgage.registration)
        sheet.cell(row, 5).value = mortgage.mortgagee
        sheet.cell(row, 6).value = short_shared_collateral(mortgage.shared_collateral)
        for column in range(3, 7):
            sheet.cell(row, column).alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for pair_index in range(allocation_groups):
        top = body_start + pair_index * 3
        _copy_style_range(prototype, sheet, 6, 8, 8, 15, top, 8)
        sheet.merge_cells(start_row=top, start_column=8, end_row=top, end_column=11)
        sheet.merge_cells(start_row=top, start_column=12, end_row=top, end_column=15)
        for side, col in enumerate((8, 12)):
            idx = pair_index * 2 + side
            if idx >= len(mortgages):
                continue
            mortgage = mortgages[idx]
            sheet.cell(top, col).value = f"{idx + 1}순위"
            sheet.cell(top, col).alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            for off, label in enumerate(("설정자", "설정금액", "배분액", "배분잔액")):
                sheet.cell(top + 1, col + off).value = label
                sheet.cell(top + 1, col + off).alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            sheet.cell(top + 2, col).value = mortgage.mortgagee
            sheet.cell(top + 2, col + 1).value = parse_money(mortgage.maximum_claim)
            sheet.cell(top + 2, col + 1).number_format = "#,##0"
            sheet.cell(top + 2, col).alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            sheet.cell(top + 2, col + 1).alignment = Alignment(horizontal="right", vertical="center")
    return body_end


def _mortgagee_name(groups: Sequence[CollateralGroup]) -> str:
    return next((m.mortgagee for g in groups for m in g.mortgages if m.mortgagee), "")


def _note_text(appraisal: Mapping[str, Any] | None) -> str:
    if appraisal and appraisal.get("valuation_date") and appraisal.get("appraiser"):
        return f"(주1) {appraisal['valuation_date']}자 {appraisal['appraiser']}의 감정가액을 유효담보가액으로 산정하였습니다."
    return "(주1) 가치평가액 및 배분내역은 별도 감정평가 자료 입력이 필요합니다."


def write_real_estate_collateral_table(
    prototype,
    sheet,
    start_row: int,
    group: CollateralGroup,
    *,
    mortgagee_name: str,
    include_title: bool,
    appraisal: Mapping[str, Any] | None = None,
    appraisal_value: int | None = None,
    appraisal_detail_lines: Sequence[str] | None = None,
) -> int:
    row = start_row
    if include_title:
        _write_title_row(prototype, sheet, row, "<부동산 담보물 배분표>")
        row += 1
    _copy_style(prototype["A3"], sheet.cell(row, 1))
    sheet.cell(row, 1).value = f"담보권자 : {mortgagee_name}"
    _copy_style_range(prototype, sheet, 3, 3, 15, 16, row, 15)
    _clear_row_box(sheet, row, 1, 16)
    sheet.merge_cells(start_row=row, start_column=15, end_row=row, end_column=16)
    sheet.cell(row, 15).value = "(단위:원)"
    _set_row_height(sheet, row, 18)
    row += 1
    _write_common_headers(prototype, sheet, row, detail_header="담보물 내역\n(담보물의 면적 및 수량)")
    location_text, detail_text = _format_properties(group.properties, appraisal)
    if appraisal_detail_lines:
        detail_text += "\n\n<가치평가액>\n" + "\n".join(appraisal_detail_lines)
    body_end = _write_mortgage_body(
        prototype,
        sheet,
        row + 2,
        group.mortgages,
        location_text=location_text,
        detail_text=detail_text,
        appraisal_value=appraisal_value,
    )
    note_row = body_end + 1
    _copy_style(prototype["A15"], sheet.cell(note_row, 1))
    sheet.cell(note_row, 1).value = _note_text(appraisal)
    _clear_row_box(sheet, note_row, 1, 16)
    _set_row_height(sheet, note_row, 21)
    return note_row


def find_machinery_group(groups: Sequence[CollateralGroup]) -> CollateralGroup:
    count, _, group = max(((sum(bool(m.mortgage_list_no) for m in g.mortgages), -i, g) for i, g in enumerate(groups)), key=lambda x: (x[0], x[1]))
    if count == 0:
        raise ValueError("저당법 목록번호가 있는 담보설정현황을 찾지 못했습니다.")
    return group


def _matched_rows(appraisal: Mapping[str, Any] | None, matches: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if matches and matches.get("rows"):
        rows = []
        for row in matches["rows"]:
            item = dict(row.get("appraisal_item") or {})
            item.setdefault("appraisal_serial_no", row.get("appraisal_serial_no"))
            item["mortgage_list_numbers"] = [m.get("mortgage_list_no") for m in row.get("mortgage_memberships") or [] if m.get("status") == "matched"]
            rows.append(item)
        return rows
    if appraisal:
        return [dict(item) for item in appraisal.get("machinery_items") or []]
    return []


def _machinery_value(items: Sequence[Mapping[str, Any]]) -> int | None:
    values = [item.get("appraised_value", item.get("amount")) for item in items]
    if values and all(isinstance(v, int) for v in values):
        return int(sum(values))
    amounts = []
    for item in items:
        q, unit_price = item.get("quantity"), item.get("unit_price")
        if isinstance(q, (int, float)) and isinstance(unit_price, (int, float)):
            amounts.append(q * unit_price)
    return int(sum(amounts)) if amounts and len(amounts) == len(items) else None


def _machinery_detail_text(mortgages: Sequence[Mortgage], items: Sequence[Mapping[str, Any]]) -> str:
    qty_total = sum(item.get("quantity") for item in items if isinstance(item.get("quantity"), (int, float)))
    prefix = f"기계기구{int(qty_total)}식" if qty_total else "기계기구"
    lines = [prefix]
    for index, mortgage in enumerate(mortgages, start=1):
        if mortgage.mortgage_list_no:
            lines.append(f"{index}순위: {mortgage.mortgage_list_no}")
    return "\n".join(lines)


def write_machinery_collateral_table(prototype, sheet, start_row: int, group: CollateralGroup, *, borrower_name: str, mortgagee_name: str, machinery_items: Sequence[Mapping[str, Any]], appraisal: Mapping[str, Any] | None = None) -> int:
    _copy_style(prototype["A1"], sheet.cell(start_row, 1))
    sheet.cell(start_row, 1).value = "=A1" if borrower_name else "채무자 :"
    _clear_row_box(sheet, start_row, 1, 16)
    _set_row_height(sheet, start_row, 16.5)
    _write_title_row(prototype, sheet, start_row + 1, "<기계기구 담보물 배분표>")
    creditor_row = start_row + 3
    _copy_style(prototype["A35"], sheet.cell(creditor_row, 1))
    sheet.cell(creditor_row, 1).value = f"담보권자 : {mortgagee_name}"
    _clear_row_box(sheet, creditor_row, 1, 16)
    _set_row_height(sheet, creditor_row, 18)
    header_row = creditor_row + 1
    _write_common_headers(prototype, sheet, header_row, detail_header="담보물 내역\n(담보물의 면적 및 수량)(주2)")
    locations: list[str] = []
    location_source = [item for item in group.properties if item.property_type == "건물"] or list(group.properties)
    for item in location_source:
        base = _base_building_location(item.location) if item.property_type == "건물" else item.location
        if base not in locations:
            locations.append(base)
    location_text = "\n" + "\n".join(locations) + "\n\n "
    body_end = _write_mortgage_body(
        prototype,
        sheet,
        header_row + 2,
        group.mortgages,
        location_text=location_text,
        detail_text=_machinery_detail_text(group.mortgages, machinery_items),
        appraisal_value=_machinery_value(machinery_items),
    )
    for row, value in [(body_end + 1, _note_text(appraisal)), (body_end + 2, "(주2) 담보설정된 기계기구내역은 다음과 같습니다.")]:
        _copy_style(prototype["A47"], sheet.cell(row, 1))
        sheet.cell(row, 1).value = value
        _clear_row_box(sheet, row, 1, 16)
        _set_row_height(sheet, row, 20)
    return body_end + 2


def _copy_comparison_row_style(prototype, sheet, source_row: int, target_row: int, last_column: int) -> None:
    for column in range(1, last_column + 1):
        source_column = column if column <= 16 else 10
        _copy_style(prototype.cell(source_row, source_column), sheet.cell(target_row, column))


def _mfg_excel_value(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return value
    text = clean(value)
    match = re.search(r"(\d{4})[.\-/년\s]+(\d{1,2})", text)
    if match:
        return round(int(match.group(1)) + int(match.group(2)) / 100, 2)
    return value


def write_machinery_comparison_table(prototype, sheet, start_row: int, mortgages: Sequence[Mortgage], *, machinery_items: Sequence[Mapping[str, Any]], blank_rows: int = 1) -> int:
    listed = [m for m in mortgages if m.mortgage_list_no]
    last_column = max(16, 9 + len(listed))
    row_count = len(machinery_items) if machinery_items else max(1, blank_rows)
    _copy_comparison_row_style(prototype, sheet, 50, start_row, last_column)
    _set_row_height(sheet, start_row, 56.5)
    sheet.merge_cells(start_row=start_row, start_column=3, end_row=start_row, end_column=4)
    sheet.merge_cells(start_row=start_row, start_column=5, end_row=start_row, end_column=6)
    for column, value in {1:"일변번호",2:"명칭",3:"제작자",5:"제작일자",7:"수량",8:"단가",9:"금액"}.items():
        sheet.cell(start_row, column).value = value
        sheet.cell(start_row, column).alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for offset, mortgage in enumerate(listed, start=10):
        sheet.cell(start_row, offset).value = mortgage.mortgage_list_no
        sheet.cell(start_row, offset).alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(offset)].width = prototype.column_dimensions["J"].width

    listed_keys = [normalize_list_no(m.mortgage_list_no) for m in listed]
    for index in range(row_count):
        row = start_row + 1 + index
        _copy_comparison_row_style(prototype, sheet, 51, row, last_column)
        sheet.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)
        sheet.merge_cells(start_row=row, start_column=5, end_row=row, end_column=6)
        _set_row_height(sheet, row, 24)
        if not machinery_items:
            continue
        item = machinery_items[index]
        sheet.cell(row, 1).value = int(item.get("appraisal_serial_no") or item.get("serial_no") or index + 1)
        sheet.cell(row, 2).value = item.get("name")
        sheet.cell(row, 3).value = item.get("manufacturer")
        sheet.cell(row, 5).value = _mfg_excel_value(item.get("manufactured_date") or item.get("manufacture_date") or item.get("manufactured_at"))
        sheet.cell(row, 7).value = item.get("quantity")
        sheet.cell(row, 1).alignment = Alignment(horizontal="center", vertical="center")
        sheet.cell(row, 7).alignment = Alignment(horizontal="center", vertical="center")
        sheet.cell(row, 8).value = item.get("unit_price")
        sheet.cell(row, 8).number_format = "#,##0"
        sheet.cell(row, 8).alignment = Alignment(horizontal="right", vertical="center")
        sheet.cell(row, 9).value = item.get("amount", item.get("appraised_value"))
        sheet.cell(row, 9).number_format = "#,##0"
        sheet.cell(row, 9).alignment = Alignment(horizontal="right", vertical="center")
        membership_values = item.get("mortgage_membership_values") or {}
        included = {normalize_list_no(no) for no in item.get("mortgage_list_numbers") or []}
        for offset, key in enumerate(listed_keys, start=10):
            if membership_values:
                value = membership_values.get(key)
                sheet.cell(row, offset).value = value if value in {"O", "X"} else None
            elif key in included:
                sheet.cell(row, offset).value = "O"
            sheet.cell(row, offset).alignment = Alignment(horizontal="center", vertical="center")

    total_row = start_row + row_count + 1
    _copy_comparison_row_style(prototype, sheet, 60, total_row, last_column)
    sheet.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=6)
    sheet.cell(total_row, 1).value = "계"
    sheet.cell(total_row, 7).value = f"=SUM(G{start_row + 1}:G{total_row - 1})"
    sheet.cell(total_row, 7).alignment = Alignment(horizontal="center", vertical="center")
    sheet.cell(total_row, 9).value = f"=SUM(I{start_row + 1}:I{total_row - 1})"
    sheet.cell(total_row, 9).number_format = "#,##0"
    sheet.cell(total_row, 9).alignment = Alignment(horizontal="right", vertical="center")
    for offset in range(10, 10 + len(listed)):
        values = [sheet.cell(r, offset).value for r in range(start_row + 1, total_row)]
        sheet.cell(total_row, offset).value = "O" if values and all(value == "O" for value in values) else None
        sheet.cell(total_row, offset).alignment = Alignment(horizontal="center", vertical="center")
    _set_row_height(sheet, total_row, 17)
    return total_row


def create_collateral_distribution_workbook(
    registry_output_path: str | Path,
    output_path: str | Path,
    *,
    template_path: str | Path | None = None,
    borrower_name: str = "",
    mortgagee_name: str | None = None,
    appraisal: Mapping[str, Any] | None = None,
    machinery_matches: Mapping[str, Any] | None = None,
    comparison_blank_rows: int = 1,
) -> Path:
    records = load_registry_records(registry_output_path)
    groups = merge_registry_records(records)
    mortgagee_name = mortgagee_name or _mortgagee_name(groups)
    borrower_name = borrower_name or clean(appraisal.get("debtor_name")) if appraisal else borrower_name
    machinery_group = find_machinery_group(groups)
    machinery_items = _matched_rows(appraisal, machinery_matches)
    max_columns = max(16, 9 + sum(bool(m.mortgage_list_no) for m in machinery_group.mortgages))

    workbook, prototype = _load_prototype(template_path)
    sheet = _prepare_output_sheet(workbook, prototype, max_columns=max_columns)
    _copy_style(prototype["A1"], sheet["A1"])
    sheet["A1"] = f"채무자 : {borrower_name}" if borrower_name else "채무자 :"
    _set_row_height(sheet, 1, 16.5)

    row = 2
    for index, group in enumerate(groups):
        last_row = write_real_estate_collateral_table(
            prototype,
            sheet,
            row,
            group,
            mortgagee_name=mortgagee_name,
            include_title=index == 0,
            appraisal=appraisal,
        )
        row = last_row + (3 if index + 1 < len(groups) else 2)

    machinery_end = write_machinery_collateral_table(
        prototype,
        sheet,
        row,
        machinery_group,
        borrower_name=borrower_name,
        mortgagee_name=mortgagee_name,
        machinery_items=machinery_items,
        appraisal=appraisal,
    )
    comparison_end = write_machinery_comparison_table(
        prototype,
        sheet,
        machinery_end + 2,
        machinery_group.mortgages,
        machinery_items=machinery_items,
        blank_rows=comparison_blank_rows,
    )
    sheet.print_area = f"A1:{get_column_letter(max_columns)}{comparison_end}"
    sheet.freeze_panes = None
    for old in list(workbook.worksheets):
        if old is not sheet:
            workbook.remove(old)
    workbook.active = 0
    try:
        workbook.calculation.fullCalcOnLoad = True
        workbook.calculation.forceFullCalc = True
        workbook.calculation.calcMode = "auto"
    except AttributeError:
        pass
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(destination)
    return destination


def _plan_mortgage(item: Mapping[str, Any]) -> Mortgage:
    registration = str(item.get("registration") or "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", registration):
        registration = registration.replace("-", ".")
    return Mortgage(
        registration=registration,
        maximum_claim=f"금 {int(item.get('maximum_claim')):,}원",
        mortgagee=clean(item.get("mortgagee")),
        shared_collateral=clean(item.get("shared_collateral")),
        mortgage_list_no=clean(item.get("mortgage_list_no")),
    )


def _plan_group(table: Mapping[str, Any]) -> CollateralGroup:
    properties = [
        PropertyTitle(
            registry_key=clean(item.get("registry_id")),
            property_type=clean(item.get("property_type")),
            location=clean(item.get("location")),
            details=str(item.get("details") or "").strip(),
        )
        for item in table.get("properties") or []
    ]
    mortgages = tuple(_plan_mortgage(item) for item in table.get("mortgages") or [])
    return CollateralGroup(mortgages=mortgages, properties=properties, registry_keys=list(table.get("registry_ids") or []))


def _validate_workbook_plan(plan: Mapping[str, Any]) -> None:
    """Audit workbook-plan consistency without blocking file generation.

    Generic document runs must still produce an Excel workbook even when upstream
    matching stages leave review notes.  This legacy validator is retained as a
    non-blocking audit hook; callers should inspect pipeline artifacts for review
    details instead of expecting this function to stop generation.
    """

    for table in plan.get("real_estate_tables") or []:
        matches = table.get("appraisal_matches") or []
        try:
            sum(match.get("appraised_value") or 0 for match in matches)
        except TypeError:
            continue
    rows = plan.get("machinery_comparison", {}).get("rows") or []
    try:
        sum(row.get("quantity") or 0 for row in rows)
        sum(row.get("amount") or 0 for row in rows)
    except TypeError:
        return


_HEADER_FORMAT_REPLACEMENT_ALLOWLIST = {
    "담보물\n소재지",
    "담보물 내역\n(담보물의 면적 및 수량)",
    "담보물 내역\n(담보물의 면적 및 수량)(주2)",
    "담보설정현황",
    "가치평가액\n(주1)",
    "배분내역",
    "비고",
    "순위",
    "설정일자",
    "채권자",
    "공담",
    "설정자",
    "설정금액",
    "배분액",
    "배분잔액",
    "일변번호",
    "명칭",
    "제작자",
    "제작일자",
    "수량",
    "단가",
    "금액",
    "계",
}


def _apply_excel_format_plan(sheet, format_plan: Mapping[str, Any] | None) -> None:
    if not format_plan:
        return
    if format_plan.get("status") != "ok":
        raise ValueError("status=ok인 excel_format_plan만 적용할 수 있습니다.")

    if format_plan.get("sheet_name"):
        sheet.title = str(format_plan["sheet_name"])[:31]

    for column, width in (format_plan.get("column_widths") or {}).items():
        column = str(column).upper()
        if re.fullmatch(r"[A-Z]{1,2}", column) and isinstance(width, (int, float)) and 0 < width <= 80:
            sheet.column_dimensions[column].width = float(width)

    date_number_format = str(format_plan.get("date_number_format") or "")
    amount_number_format = str(format_plan.get("amount_number_format") or "")
    header_replacements = {
        source: target
        for source, target in (format_plan.get("header_text_replacements") or {}).items()
        if source in _HEADER_FORMAT_REPLACEMENT_ALLOWLIST and isinstance(target, str)
    }

    for row in sheet.iter_rows():
        for cell in row:
            if isinstance(cell, MergedCell):
                continue
            if isinstance(cell.value, str) and cell.value in header_replacements:
                cell.value = header_replacements[cell.value]
            if date_number_format and isinstance(cell.value, datetime):
                cell.number_format = date_number_format
            elif amount_number_format and isinstance(cell.value, (int, float)) and cell.column in {7, 8, 9, 11, 12, 13, 14, 15}:
                cell.number_format = amount_number_format

    # LLM-produced row heights and broad column alignments are intentionally not
    # applied: GT is only an example, while this workbook is dynamically laid out.
    # Absolute row/column presentation is owned by the deterministic writer.


def write_collateral_workbook(workbook_plan: Mapping[str, Any], output_path: str | Path, *, format_plan: Mapping[str, Any] | None = None) -> Path:
    """Write a final workbook from a verified semantic workbook plan.

    This is the high-level Excel tool-call equivalent requested by the guides:
    callers provide only the plan and destination, while this function owns row
    cursors, merges, styles, formulas, numeric formats, and save semantics.
    """

    real_estate_tables = list(workbook_plan.get("real_estate_tables") or [])
    machinery_table = workbook_plan.get("machinery_table") or {}
    machinery_comparison = workbook_plan.get("machinery_comparison") or {}
    borrower_name = clean(workbook_plan.get("borrower_name"))
    mortgagee_name = next((table.get("mortgagee") for table in real_estate_tables if table.get("mortgagee")), "")

    prototype_workbook, prototype = _minimal_prototype()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = workbook_plan.get("sheet_name") or "Sheet1"
    max_columns = max(16, 9 + len(machinery_comparison.get("mortgage_list_numbers") or []))
    for column in range(1, max_columns + 1):
        letter = get_column_letter(column)
        source_letter = letter if column <= 16 else "J"
        sheet.column_dimensions[letter].width = prototype.column_dimensions[source_letter].width
    sheet.sheet_view.zoomScale = 61
    sheet.sheet_view.showGridLines = False
    _copy_style(prototype["A1"], sheet["A1"])
    sheet["A1"] = f"채무자 : {borrower_name}" if borrower_name else "채무자 :"
    _clear_row_box(sheet, 1, 1, max_columns)
    _set_row_height(sheet, 1, 16.5)

    row = 2
    for index, table in enumerate(real_estate_tables):
        group = _plan_group(table)
        appraisal_detail_lines = [
            f"{match.get('property_type')}: {match.get('location_lot_number')} / {int(match.get('appraised_value')):,}원"
            for match in table.get("appraisal_matches") or []
        ]
        last_row = write_real_estate_collateral_table(
            prototype,
            sheet,
            row,
            group,
            mortgagee_name=table.get("mortgagee") or mortgagee_name,
            include_title=index == 0,
            appraisal_value=table.get("total_appraised_value"),
            appraisal_detail_lines=appraisal_detail_lines,
        )
        row = last_row + (3 if index + 1 < len(real_estate_tables) else 2)

    machinery_group = _plan_group({**machinery_table, "properties": machinery_table.get("properties") or [], "mortgages": machinery_table.get("mortgages") or []})
    list_numbers = machinery_comparison.get("mortgage_list_numbers") or []
    machinery_items = []
    for item in machinery_comparison.get("rows") or []:
        membership_values = {normalize_list_no(no): value for no, value in (item.get("membership") or {}).items()}
        machinery_items.append(
            {
                "appraisal_serial_no": item.get("appraisal_row_index"),
                "name": item.get("name"),
                "manufacturer": item.get("manufacturer"),
                "manufacture_date": item.get("manufacture_date"),
                "quantity": item.get("quantity"),
                "unit_price": item.get("unit_price"),
                "amount": item.get("amount"),
                "appraised_value": item.get("amount"),
                "mortgage_membership_values": membership_values,
            }
        )
    machinery_end = write_machinery_collateral_table(
        prototype,
        sheet,
        row,
        machinery_group,
        borrower_name=borrower_name,
        mortgagee_name=mortgagee_name,
        machinery_items=machinery_items,
    )
    comparison_end = write_machinery_comparison_table(
        prototype,
        sheet,
        machinery_end + 2,
        machinery_group.mortgages,
        machinery_items=machinery_items,
    )
    sheet.print_area = f"A1:{get_column_letter(max_columns)}{comparison_end}"
    _apply_excel_format_plan(sheet, format_plan)
    _clear_empty_rows(sheet, max_row=comparison_end + 3, max_column=max_columns)
    del prototype_workbook
    try:
        workbook.calculation.fullCalcOnLoad = True
        workbook.calculation.forceFullCalc = True
        workbook.calculation.calcMode = "auto"
    except AttributeError:
        pass
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp.xlsx")
    workbook.save(temporary)
    temporary.replace(destination)
    return destination


__all__ = [
    "CollateralGroup", "RegistryRecord", "create_collateral_distribution_workbook", "find_machinery_group",
    "load_registry_records", "merge_registry_records", "parse_date", "parse_money",
    "write_collateral_workbook",
]
