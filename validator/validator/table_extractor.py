from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from openpyxl import load_workbook

TableType = Literal["real_estate_collateral", "machine_collateral", "machine_register"]


@dataclass
class DocumentTable:
    table_id: str
    table_type: TableType
    title: str
    start_row: int
    end_row: int
    headers: list[str]
    rows: list[list[str]]
    narrative: str
    source_summary: dict[str, Any] = field(default_factory=dict)

    def to_prompt_text(self) -> str:
        lines = [
            f"## {self.table_id}: {self.title}",
            f"table_type: {self.table_type}",
            f"source_rows: {self.start_row}-{self.end_row}",
        ]
        if self.narrative:
            lines += ["", "### 주요 본문", self.narrative]
        if self.headers:
            lines += ["", "### 구조화 행"]
            lines.append(" | ".join(self.headers))
            lines.append(" | ".join(["---"] * len(self.headers)))
            for row in self.rows:
                lines.append(" | ".join(row))
        return "\n".join(lines)


@dataclass
class StructuredDocument:
    path: str
    tables: list[DocumentTable]

    def to_prompt_text(self) -> str:
        parts = [f"# Structured collateral document\nsource_file: {self.path}"]
        parts.extend(table.to_prompt_text() for table in self.tables)
        return "\n\n".join(parts)


def extract_document_tables(path: str | Path) -> StructuredDocument:
    """Extract business tables from the worksheet structure before any LLM parsing.

    This is structural preprocessing only. It does not compare GT/Target and does not compute a
    score. The LLM receives separated business tables rather than raw cell-coordinate dumps.
    """
    path = Path(path)
    wb_values = load_workbook(path, data_only=True)
    wb_raw = load_workbook(path, data_only=False)
    ws_values = wb_values.active
    ws_raw = wb_raw.active
    max_row = _max_meaningful_row(ws_values, ws_raw)
    rows = [_read_row(ws_values, ws_raw, row) for row in range(1, max_row + 1)]

    real_headers = _find_collateral_header_rows(rows, machine=False)
    machine_headers = _find_collateral_header_rows(rows, machine=True)
    register_header = _find_machine_register_header(rows)

    tables: list[DocumentTable] = []
    for idx, header_row in enumerate(real_headers, start=1):
        data_start = header_row + 2
        end = _find_table_end(rows, data_start, stop_before_titles=True)
        tables.append(_build_collateral_table(
            table_id=f"real_estate_{idx}",
            table_type="real_estate_collateral",
            title=f"부동산 담보물 배분표 #{idx}",
            rows=rows,
            header_row=header_row,
            start=data_start,
            end=end,
        ))

    if machine_headers:
        header_row = machine_headers[0]
        data_start = header_row + 2
        end = (register_header - 2) if register_header else _find_table_end(rows, data_start, stop_before_titles=False)
        tables.append(_build_collateral_table(
            table_id="machine_collateral_1",
            table_type="machine_collateral",
            title="기계기구 담보물 배분표",
            rows=rows,
            header_row=header_row,
            start=data_start,
            end=end,
        ))

    if register_header:
        end = _find_register_end(rows, register_header + 1)
        tables.append(_build_register_table(rows, register_header, end))

    return StructuredDocument(path=str(path), tables=tables)


def _value(ws_values, ws_raw, row: int, col: int) -> Any:
    calculated = ws_values.cell(row, col).value
    if calculated is not None:
        return calculated
    return ws_raw.cell(row, col).value


def _max_meaningful_row(ws_values, ws_raw) -> int:
    max_row = 1
    for row in range(1, ws_raw.max_row + 1):
        row_has_value = False
        for col in range(1, min(ws_raw.max_column, 16) + 1):
            value = _value(ws_values, ws_raw, row, col)
            if value is not None and str(value).strip():
                row_has_value = True
                break
        if row_has_value:
            max_row = row
    return max_row


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", "\n")
    parts = [part.strip() for part in text.split("\n")]
    return " / ".join(part for part in parts if part)


def _read_row(ws_values, ws_raw, row: int) -> list[str]:
    return [_clean(_value(ws_values, ws_raw, row, col)) for col in range(1, 17)]


def _row_text(row: list[str]) -> str:
    return " ".join(cell for cell in row if cell)


def _has_machine_title_before(rows: list[list[str]], one_based_row: int) -> bool:
    for r in range(one_based_row - 1, 0, -1):
        text = _row_text(rows[r - 1])
        if "<기계기구 담보물 배분표>" in text:
            return True
        if "<부동산 담보물 배분표>" in text:
            return False
    return False


def _find_collateral_header_rows(rows: list[list[str]], *, machine: bool) -> list[int]:
    result: list[int] = []
    for idx, row in enumerate(rows, start=1):
        text = _row_text(row)
        is_header = "담보물" in text and "소재지" in text and "담보설정현황" in text and "가치평가액" in text
        if not is_header:
            continue
        in_machine = _has_machine_title_before(rows, idx)
        if in_machine == machine:
            result.append(idx)
    return result


def _find_machine_register_header(rows: list[list[str]]) -> int | None:
    for idx, row in enumerate(rows, start=1):
        text = _row_text(row)
        if ("일련번호" in text or "일변번호" in text) and "명칭" in text and "제작자" in text and "금액" in text:
            return idx
    return None


def _find_table_end(rows: list[list[str]], start: int, *, stop_before_titles: bool) -> int:
    end = start
    for idx in range(start, len(rows) + 1):
        text = _row_text(rows[idx - 1])
        if idx > start and stop_before_titles and ("담보권자" in text or "<기계기구 담보물 배분표>" in text):
            return idx - 1
        if idx > start and text.startswith("(주1)"):
            return idx
        if text:
            end = idx
    return end


def _find_register_end(rows: list[list[str]], start: int) -> int:
    end = start
    for idx in range(start, len(rows) + 1):
        text = _row_text(rows[idx - 1])
        if not text:
            continue
        end = idx
        if rows[idx - 1][0] == "계":
            return idx
    return end


def _build_collateral_table(table_id: str, table_type: TableType, title: str, rows: list[list[str]], header_row: int, start: int, end: int) -> DocumentTable:
    headers = [
        "담보물 소재지",
        "담보물 내역",
        "순위",
        "설정일자",
        "채권자",
        "공담",
        "가치평가액",
        "배분내역_1",
        "설정금액_1",
        "배분액_1",
        "배분잔액_1",
        "배분내역_2",
        "설정금액_2",
        "배분액_2",
        "배분잔액_2",
        "비고",
    ]
    body = [row for row in rows[start - 1:end] if _row_text(row)]
    narrative_lines = []
    if body:
        first = body[0]
        if first[0]: narrative_lines.append(f"담보물 소재지: {first[0]}")
        if first[1]: narrative_lines.append(f"담보물 내역: {first[1]}")
        if first[6]: narrative_lines.append(f"가치평가액: {first[6]}")
    allocation_lines = []
    for row in body:
        if row[2] or row[3] or row[4] or row[7] or row[11]:
            allocation_lines.append(" / ".join(f"{headers[i]}={cell}" for i, cell in enumerate(row) if cell and i < len(headers)))
    if allocation_lines:
        narrative_lines.append("담보설정 및 배분행:\n" + "\n".join(allocation_lines))
    return DocumentTable(table_id=table_id, table_type=table_type, title=title, start_row=header_row, end_row=end, headers=headers, rows=body, narrative="\n".join(narrative_lines))


def _build_register_table(rows: list[list[str]], header_row: int, end: int) -> DocumentTable:
    raw_headers = rows[header_row - 1]
    headers = [h or f"열{i + 1}" for i, h in enumerate(raw_headers)]
    body = [row for row in rows[header_row:end] if _row_text(row)]
    narrative = "기계기구 대조표. 각 행은 일련번호별 기계와 목록 제2012-399/2012-514/2012-515/2012-720/2017-309/2018-234 포함 여부를 나타낸다. 공란은 Target에서는 unknown, GT에서는 설정 정책에 따른다."
    return DocumentTable(table_id="machine_register_1", table_type="machine_register", title="기계기구 대조표", start_row=header_row, end_row=end, headers=headers, rows=body, narrative=narrative)
