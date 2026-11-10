"""등기부 JSON 병합 및 담보물 배분표 Excel 작성.

LLM은 등기부의 title.json / encumbrance.json까지만 추출하고,
이 모듈은 셀 좌표·병합·서식·그룹화를 결정론적으로 처리한다.
"""

from __future__ import annotations

import json
import re
from copy import copy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter


REGISTRY_PATTERN = re.compile(r"registry_(\d+)", re.IGNORECASE)
MONEY_PATTERN = re.compile(r"-?\d[\d,]*")


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


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _registry_key(data: Mapping[str, Any], fallback: str) -> str:
    match = REGISTRY_PATTERN.search(str(data.get("registry_id", "")))
    if match:
        return f"registry_{int(match.group(1)):02d}"
    return fallback


def _registry_order(registry_key: str) -> tuple[int, str]:
    match = re.search(r"(\d+)", registry_key)
    return (int(match.group(1)) if match else 10**9, registry_key)


def _parse_date(value: str) -> datetime:
    normalized = _clean(value).replace("-", ".").replace("/", ".")
    match = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", normalized)
    if not match:
        raise ValueError(f"설정일자를 해석할 수 없습니다: {value!r}")
    return datetime(*map(int, match.groups()))


def _parse_money(value: str) -> int | None:
    match = MONEY_PATTERN.search(str(value or ""))
    if not match:
        return None
    return int(match.group(0).replace(",", ""))


def _short_shared_collateral(value: str) -> str:
    value = _clean(value)
    if value in {"공용담보", "공동담보", "공담"}:
        return "공담"
    return value


def load_registry_records(registry_output_path: str | Path) -> list[RegistryRecord]:
    """폴더 아래의 title/encumbrance JSON을 registry_id 기준으로 정확히 짝짓는다."""

    root = Path(registry_output_path)
    if not root.exists():
        raise FileNotFoundError(f"JSON 결과 경로가 없습니다: {root}")

    titles: dict[str, Mapping[str, Any]] = {}
    encumbrances: dict[str, Mapping[str, Any]] = {}

    for path in sorted(root.rglob("*.json")):
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            continue

        fallback = path.parent.name if path.parent != root else path.stem
        key = _registry_key(data, fallback)
        if {"property_type", "location", "details"} <= data.keys():
            if key in titles:
                raise ValueError(f"{key}의 title JSON이 중복되었습니다.")
            titles[key] = data
        elif "mortgages" in data:
            if key in encumbrances:
                raise ValueError(f"{key}의 encumbrance JSON이 중복되었습니다.")
            encumbrances[key] = data

    missing_title = sorted(set(encumbrances) - set(titles))
    missing_encumbrance = sorted(set(titles) - set(encumbrances))
    if missing_title or missing_encumbrance:
        raise ValueError(
            "title/encumbrance 짝이 맞지 않습니다. "
            f"title 없음={missing_title}, encumbrance 없음={missing_encumbrance}"
        )
    if not titles:
        raise ValueError(f"등기부 JSON을 찾지 못했습니다: {root}")

    records: list[RegistryRecord] = []
    for key in sorted(titles, key=_registry_order):
        title_data = titles[key]
        raw_mortgages = encumbrances[key].get("mortgages", [])
        indexed_mortgages = []
        for index, item in enumerate(raw_mortgages):
            mortgage = Mortgage(
                registration=_clean(item.get("registration")),
                maximum_claim=_clean(item.get("maximum_claim")),
                mortgagee=_clean(item.get("mortgagee")),
                shared_collateral=_clean(item.get("shared_collateral")),
                mortgage_list_no=_clean(item.get("mortgage_list_no")),
            )
            indexed_mortgages.append((index, mortgage))

        # 같은 날짜 안에서는 원본 배열 순서를 유지한다.
        indexed_mortgages.sort(
            key=lambda pair: (_parse_date(pair[1].registration), pair[0])
        )
        mortgages = tuple(mortgage for _, mortgage in indexed_mortgages)
        title = PropertyTitle(
            registry_key=key,
            property_type=_clean(title_data.get("property_type")),
            location=_clean(title_data.get("location")),
            details=str(title_data.get("details") or "").strip(),
        )
        records.append(RegistryRecord(key, title, mortgages))
    return records


def _mortgage_signature(mortgages: Sequence[Mortgage]) -> tuple[tuple[str, ...], ...]:
    """담보설정현황 전체가 같은 경우에만 같은 표로 묶는다."""

    return tuple(
        (
            mortgage.registration,
            mortgage.maximum_claim,
            mortgage.mortgagee,
            mortgage.shared_collateral,
            mortgage.mortgage_list_no,
        )
        for mortgage in mortgages
    )


def merge_registry_records(records: Sequence[RegistryRecord]) -> list[CollateralGroup]:
    """등기부를 담보설정현황별로 병합하고 최초 등기부 순서를 보존한다."""

    groups: dict[tuple[tuple[str, ...], ...], CollateralGroup] = {}
    order: list[tuple[tuple[str, ...], ...]] = []

    for record in sorted(records, key=lambda item: _registry_order(item.registry_key)):
        signature = _mortgage_signature(record.mortgages)
        if signature not in groups:
            groups[signature] = CollateralGroup(
                mortgages=record.mortgages,
                properties=[],
                registry_keys=[],
            )
            order.append(signature)
        groups[signature].properties.append(record.title)
        groups[signature].registry_keys.append(record.registry_key)

    property_order = {"토지": 0, "건물": 1}
    merged = [groups[signature] for signature in order]
    for group in merged:
        group.properties.sort(
            key=lambda item: (
                property_order.get(item.property_type, 99),
                _registry_order(item.registry_key),
            )
        )
    return merged


def _copy_style(source, target) -> None:
    target._style = copy(source._style)
    if source.number_format:
        target.number_format = source.number_format
    target.alignment = copy(source.alignment)
    target.protection = copy(source.protection)


def _copy_style_range(
    source_sheet,
    target_sheet,
    source_min_row: int,
    source_max_row: int,
    source_min_col: int,
    source_max_col: int,
    target_min_row: int,
    target_min_col: int,
) -> None:
    for row_offset, source_row in enumerate(
        range(source_min_row, source_max_row + 1)
    ):
        for col_offset, source_col in enumerate(
            range(source_min_col, source_max_col + 1)
        ):
            _copy_style(
                source_sheet.cell(source_row, source_col),
                target_sheet.cell(
                    target_min_row + row_offset,
                    target_min_col + col_offset,
                ),
            )


def _set_row_height(sheet, row: int, height: float | None) -> None:
    if height is not None:
        sheet.row_dimensions[row].height = height


def _write_title_row(prototype, sheet, row: int, title: str) -> None:
    _copy_style_range(prototype, sheet, 2, 2, 1, 16, row, 1)
    sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=16)
    sheet.cell(row, 1).value = title
    _set_row_height(sheet, row, prototype.row_dimensions[2].height)


def _format_property_blocks(
    properties: Sequence[PropertyTitle],
) -> tuple[str, str]:
    by_type: dict[str, list[PropertyTitle]] = {}
    type_order: list[str] = []
    for item in properties:
        if item.property_type not in by_type:
            by_type[item.property_type] = []
            type_order.append(item.property_type)
        by_type[item.property_type].append(item)

    location_lines: list[str] = []
    detail_lines: list[str] = []
    for property_type in type_order:
        label = property_type or "기타"
        location_lines.append(f"<{label}>")
        detail_lines.append(f"<{label}>")
        for index, item in enumerate(by_type[property_type], start=1):
            location_lines.append(item.location)
            detail_lines.append(f"{index}. {item.location}")
            for line in item.details.splitlines() or [""]:
                detail_lines.append(f"- {line.strip()}")
        location_lines.append("")
        detail_lines.append("")
    return "\n".join(location_lines).strip(), "\n".join(detail_lines).strip()


def _write_common_headers(
    prototype,
    sheet,
    top_row: int,
    *,
    detail_header: str,
) -> None:
    _copy_style_range(prototype, sheet, 4, 5, 1, 16, top_row, 1)
    merges = [
        (1, 1, 2, 1),
        (1, 2, 2, 2),
        (1, 3, 1, 6),
        (1, 7, 2, 7),
        (1, 8, 2, 15),
        (1, 16, 2, 16),
    ]
    for r1, c1, r2, c2 in merges:
        sheet.merge_cells(
            start_row=top_row + r1 - 1,
            start_column=c1,
            end_row=top_row + r2 - 1,
            end_column=c2,
        )

    values = {
        (0, 1): "담보물\n소재지",
        (0, 2): detail_header,
        (0, 3): "담보설정현황",
        (0, 7): "가치평가액\n(주1)",
        (0, 8): "배분내역",
        (0, 16): "비고",
        (1, 3): "순위",
        (1, 4): "설정일자",
        (1, 5): "채권자",
        (1, 6): "공담",
    }
    for (row_offset, column), value in values.items():
        sheet.cell(top_row + row_offset, column).value = value
    _set_row_height(sheet, top_row, 21)
    _set_row_height(sheet, top_row + 1, 21)


def _write_mortgage_body(
    prototype,
    sheet,
    body_start: int,
    mortgages: Sequence[Mortgage],
    *,
    location_text: str,
    detail_text: str,
    appraisal_value: int | None = None,
) -> int:
    allocation_groups = max(1, (len(mortgages) + 1) // 2)
    body_rows = max(len(mortgages), allocation_groups * 3)
    body_end = body_start + body_rows - 1

    for offset in range(body_rows):
        source_row = 6 if offset == 0 else 14 if offset == body_rows - 1 else 8
        _copy_style_range(prototype, sheet, source_row, source_row, 1, 16, body_start + offset, 1)
        _set_row_height(sheet, body_start + offset, 50)

    for column in (1, 2, 7, 16):
        sheet.merge_cells(
            start_row=body_start,
            start_column=column,
            end_row=body_end,
            end_column=column,
        )
    sheet.cell(body_start, 1).value = location_text
    sheet.cell(body_start, 2).value = detail_text
    sheet.cell(body_start, 7).value = appraisal_value

    for offset, mortgage in enumerate(mortgages):
        row = body_start + offset
        sheet.cell(row, 3).value = offset + 1
        sheet.cell(row, 4).value = _parse_date(mortgage.registration)
        sheet.cell(row, 5).value = mortgage.mortgagee
        sheet.cell(row, 6).value = _short_shared_collateral(
            mortgage.shared_collateral
        )

    for pair_index in range(allocation_groups):
        top = body_start + pair_index * 3
        _copy_style_range(prototype, sheet, 6, 8, 8, 15, top, 8)
        sheet.merge_cells(
            start_row=top, start_column=8, end_row=top, end_column=11
        )
        sheet.merge_cells(
            start_row=top, start_column=12, end_row=top, end_column=15
        )

        for side, column in enumerate((8, 12)):
            mortgage_index = pair_index * 2 + side
            if mortgage_index >= len(mortgages):
                continue
            mortgage = mortgages[mortgage_index]
            sheet.cell(top, column).value = f"{mortgage_index + 1}순위"
            for col_offset, label in enumerate(
                ("설정자", "설정금액", "배분액", "배분잔액")
            ):
                sheet.cell(top + 1, column + col_offset).value = label
            sheet.cell(top + 2, column).value = mortgage.mortgagee
            sheet.cell(top + 2, column + 1).value = _parse_money(
                mortgage.maximum_claim
            )
            sheet.cell(top + 2, column + 1).number_format = "#,##0"

    return body_end


def write_real_estate_collateral_table(
    prototype,
    sheet,
    start_row: int,
    group: CollateralGroup,
    *,
    mortgagee_name: str,
    include_title: bool,
) -> int:
    """부동산 담보물 배분표 1개를 쓰고 마지막 사용 행을 반환한다."""

    row = start_row
    if include_title:
        _write_title_row(prototype, sheet, row, "<부동산 담보물 배분표>")
        row += 1

    _copy_style(prototype["A3"], sheet.cell(row, 1))
    sheet.cell(row, 1).value = f"담보권자 : {mortgagee_name}"
    _copy_style_range(prototype, sheet, 3, 3, 15, 16, row, 15)
    sheet.merge_cells(start_row=row, start_column=15, end_row=row, end_column=16)
    sheet.cell(row, 15).value = "(단위:원)"
    _set_row_height(sheet, row, 18)
    row += 1

    _write_common_headers(
        prototype,
        sheet,
        row,
        detail_header="담보물 내역\n(담보물의 면적 및 수량)",
    )
    body_start = row + 2
    location_text, detail_text = _format_property_blocks(group.properties)
    body_end = _write_mortgage_body(
        prototype,
        sheet,
        body_start,
        group.mortgages,
        location_text=location_text,
        detail_text=detail_text,
    )
    note_row = body_end + 1
    _copy_style(prototype["A15"], sheet.cell(note_row, 1))
    sheet.cell(note_row, 1).value = (
        "(주1) 가치평가액 및 배분내역은 별도 감정평가 자료 입력이 필요합니다."
    )
    _set_row_height(sheet, note_row, 21)
    return note_row


def _find_machinery_group(groups: Sequence[CollateralGroup]) -> CollateralGroup:
    candidates = [
        (sum(bool(item.mortgage_list_no) for item in group.mortgages), index, group)
        for index, group in enumerate(groups)
    ]
    count, _, group = max(candidates, key=lambda item: (item[0], -item[1]))
    if count == 0:
        raise ValueError("저당법 목록번호가 있는 담보설정현황을 찾지 못했습니다.")
    return group


def _machinery_detail_text(
    mortgages: Sequence[Mortgage],
    machinery_items: Sequence[Mapping[str, Any]] | None,
) -> str:
    prefix = (
        f"기계기구 {len(machinery_items)}식"
        if machinery_items
        else "기계기구"
    )
    lines = [prefix]
    for index, mortgage in enumerate(mortgages, start=1):
        if mortgage.mortgage_list_no:
            lines.append(f"{index}순위: {mortgage.mortgage_list_no}")
    return "\n".join(lines)


def write_machinery_collateral_table(
    prototype,
    sheet,
    start_row: int,
    group: CollateralGroup,
    *,
    borrower_name: str,
    mortgagee_name: str,
    machinery_items: Sequence[Mapping[str, Any]] | None,
) -> int:
    """기계기구 담보물 배분표를 쓰고 마지막 사용 행을 반환한다."""

    _copy_style(prototype["A1"], sheet.cell(start_row, 1))
    sheet.cell(start_row, 1).value = f"채무자 : {borrower_name}"
    _set_row_height(sheet, start_row, 16.5)
    _write_title_row(
        prototype, sheet, start_row + 1, "<기계기구 담보물 배분표>"
    )

    creditor_row = start_row + 3
    _copy_style(prototype["A35"], sheet.cell(creditor_row, 1))
    sheet.cell(creditor_row, 1).value = f"담보권자 : {mortgagee_name}"
    _set_row_height(sheet, creditor_row, 18)

    header_row = creditor_row + 1
    _write_common_headers(
        prototype,
        sheet,
        header_row,
        detail_header="담보물 내역\n(담보물의 면적 및 수량)(주2)",
    )

    locations = []
    for item in group.properties:
        if item.location not in locations:
            locations.append(item.location)
    location_text = "\n".join(locations)
    detail_text = _machinery_detail_text(group.mortgages, machinery_items)

    appraisal_value = None
    if machinery_items:
        amounts = []
        for item in machinery_items:
            quantity = item.get("quantity")
            unit_price = item.get("unit_price")
            if isinstance(quantity, (int, float)) and isinstance(
                unit_price, (int, float)
            ):
                amounts.append(quantity * unit_price)
        if len(amounts) == len(machinery_items):
            appraisal_value = int(sum(amounts))

    body_end = _write_mortgage_body(
        prototype,
        sheet,
        header_row + 2,
        group.mortgages,
        location_text=location_text,
        detail_text=detail_text,
        appraisal_value=appraisal_value,
    )
    note1 = body_end + 1
    note2 = body_end + 2
    for row, value in (
        (
            note1,
            "(주1) 가치평가액 및 배분내역은 별도 감정평가 자료 입력이 필요합니다.",
        ),
        (note2, "(주2) 담보설정된 기계기구 목록번호는 다음과 같습니다."),
    ):
        _copy_style(prototype["A47"], sheet.cell(row, 1))
        sheet.cell(row, 1).value = value
        _set_row_height(sheet, row, 20)
    return note2


def _copy_comparison_row_style(
    prototype, sheet, source_row: int, target_row: int, last_column: int
) -> None:
    for column in range(1, last_column + 1):
        source_column = column if column <= 16 else 10
        _copy_style(
            prototype.cell(source_row, source_column),
            sheet.cell(target_row, column),
        )


def write_machinery_comparison_table(
    prototype,
    sheet,
    start_row: int,
    mortgages: Sequence[Mortgage],
    *,
    machinery_items: Sequence[Mapping[str, Any]] | None,
    blank_rows: int,
) -> int:
    """기계기구 대조표를 작성한다. JSON으로 모르는 항목은 공란으로 둔다."""

    listed_mortgages = [item for item in mortgages if item.mortgage_list_no]
    last_column = 9 + len(listed_mortgages)
    row_count = len(machinery_items) if machinery_items else max(1, blank_rows)

    _copy_comparison_row_style(prototype, sheet, 50, start_row, last_column)
    _set_row_height(sheet, start_row, 56.5)
    sheet.merge_cells(
        start_row=start_row, start_column=3, end_row=start_row, end_column=4
    )
    sheet.merge_cells(
        start_row=start_row, start_column=5, end_row=start_row, end_column=6
    )
    headers = {
        1: "일련번호",
        2: "명칭",
        3: "제작자",
        5: "제작일자",
        7: "수량",
        8: "단가",
        9: "금액",
    }
    for column, value in headers.items():
        sheet.cell(start_row, column).value = value
    for offset, mortgage in enumerate(listed_mortgages, start=10):
        sheet.cell(start_row, offset).value = mortgage.mortgage_list_no
        sheet.column_dimensions[get_column_letter(offset)].width = (
            prototype.column_dimensions["J"].width
        )

    for index in range(row_count):
        row = start_row + 1 + index
        _copy_comparison_row_style(prototype, sheet, 51, row, last_column)
        sheet.merge_cells(
            start_row=row, start_column=3, end_row=row, end_column=4
        )
        sheet.merge_cells(
            start_row=row, start_column=5, end_row=row, end_column=6
        )
        _set_row_height(sheet, row, 24)

        if not machinery_items:
            continue
        item = machinery_items[index]
        sheet.cell(row, 1).value = item.get("serial_no", index + 1)
        sheet.cell(row, 2).value = item.get("name")
        sheet.cell(row, 3).value = item.get("manufacturer")
        sheet.cell(row, 5).value = item.get("manufactured_at")
        sheet.cell(row, 7).value = item.get("quantity")
        sheet.cell(row, 8).value = item.get("unit_price")
        sheet.cell(row, 8).number_format = "#,##0"
        if item.get("quantity") is not None and item.get("unit_price") is not None:
            sheet.cell(row, 9).value = f"=H{row}*G{row}"
            sheet.cell(row, 9).number_format = "#,##0"
        included = set(item.get("mortgage_list_numbers") or [])
        for offset, mortgage in enumerate(listed_mortgages, start=10):
            if mortgage.mortgage_list_no in included:
                sheet.cell(row, offset).value = "O"

    total_row = start_row + row_count + 1
    _copy_comparison_row_style(prototype, sheet, 60, total_row, last_column)
    sheet.merge_cells(
        start_row=total_row, start_column=1, end_row=total_row, end_column=6
    )
    sheet.cell(total_row, 1).value = "계"
    _set_row_height(sheet, total_row, 17)
    if machinery_items:
        first_data_row = start_row + 1
        last_data_row = total_row - 1
        sheet.cell(total_row, 7).value = (
            f"=SUM(G{first_data_row}:G{last_data_row})"
        )
        sheet.cell(total_row, 9).value = (
            f"=SUM(I{first_data_row}:I{last_data_row})"
        )
        sheet.cell(total_row, 9).number_format = "#,##0"
    return total_row


def _prepare_output_sheet(workbook, prototype):
    sheet = workbook.create_sheet("담보물배분표_자동작성")
    for column in range(1, 17):
        letter = get_column_letter(column)
        sheet.column_dimensions[letter].width = (
            prototype.column_dimensions[letter].width
        )
        sheet.column_dimensions[letter].hidden = (
            prototype.column_dimensions[letter].hidden
        )
    sheet.sheet_view.zoomScale = prototype.sheet_view.zoomScale
    sheet.sheet_view.showGridLines = prototype.sheet_view.showGridLines
    sheet.page_margins = copy(prototype.page_margins)
    sheet.page_setup = copy(prototype.page_setup)
    sheet.sheet_properties = copy(prototype.sheet_properties)
    return sheet


def create_collateral_distribution_workbook(
    registry_output_path: str | Path,
    template_path: str | Path,
    output_path: str | Path,
    *,
    borrower_name: str = "",
    mortgagee_name: str | None = None,
    machinery_items: Sequence[Mapping[str, Any]] | None = None,
    comparison_blank_rows: int = 1,
) -> Path:
    """JSON 병합부터 세 종류의 표 작성까지 한 번에 실행한다."""

    records = load_registry_records(registry_output_path)
    groups = merge_registry_records(records)
    if mortgagee_name is None:
        mortgagee_name = next(
            (
                item.mortgagee
                for group in groups
                for item in group.mortgages
                if item.mortgagee
            ),
            "",
        )

    workbook = load_workbook(Path(template_path))
    prototype = workbook.active
    sheet = _prepare_output_sheet(workbook, prototype)

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
        )
        row = last_row + 2
        if index + 1 < len(groups):
            _write_title_row(
                prototype, sheet, row, "<부동산 담보물 배분표>"
            )
            row += 1

    machinery_group = _find_machinery_group(groups)
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
        blank_rows=comparison_blank_rows,
    )

    sheet.print_area = f"A1:{get_column_letter(sheet.max_column)}{comparison_end}"
    sheet.freeze_panes = "A4"
    workbook.remove(prototype)
    workbook.active = workbook.sheetnames.index(sheet.title)
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


def create_collateral_excel_tool_call(
    registry_output_path: str,
    template_path: str,
    output_path: str,
    borrower_name: str = "",
    mortgagee_name: str = "",
    comparison_blank_rows: int = 1,
) -> str:
    """LangChain tool-call 등록에 적합한 문자열 인자 전용 래퍼."""

    result = create_collateral_distribution_workbook(
        registry_output_path=registry_output_path,
        template_path=template_path,
        output_path=output_path,
        borrower_name=borrower_name,
        mortgagee_name=mortgagee_name or None,
        comparison_blank_rows=comparison_blank_rows,
    )
    return str(result)


def get_collateral_excel_tool():
    """필요할 때만 LangChain 의존성을 불러와 StructuredTool로 등록한다."""

    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        func=create_collateral_excel_tool_call,
        name="create_collateral_distribution_excel",
        description=(
            "등기부 title/encumbrance JSON들을 담보설정현황별로 병합한 뒤, "
            "부동산 담보물 배분표·기계기구 담보물 배분표·기계기구 대조표를 "
            "기존 Excel 양식으로 생성한다."
        ),
    )


__all__ = [
    "CollateralGroup",
    "RegistryRecord",
    "create_collateral_distribution_workbook",
    "create_collateral_excel_tool_call",
    "get_collateral_excel_tool",
    "load_registry_records",
    "merge_registry_records",
    "write_machinery_collateral_table",
    "write_machinery_comparison_table",
    "write_real_estate_collateral_table",
]