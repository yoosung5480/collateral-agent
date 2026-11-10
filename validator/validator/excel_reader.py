from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter


@dataclass
class CellSnapshot:
    coordinate: str
    row: int
    column: int
    value: Any
    formula: str | None
    calculated_value: Any
    merged_value: Any
    hidden_row: bool
    hidden_column: bool


@dataclass
class SheetSnapshot:
    name: str
    max_row: int
    max_column: int
    merged_ranges: list[str]
    cells: list[CellSnapshot] = field(default_factory=list)


@dataclass
class WorkbookSnapshot:
    path: str
    sheets: list[SheetSnapshot]

    def compact_text(self, max_chars: int = 60000) -> str:
        parts: list[str] = []
        for sheet in self.sheets:
            parts.append(f"# Sheet: {sheet.name}")
            current_row = None
            row_parts: list[str] = []
            for cell in sorted(sheet.cells, key=lambda c: (c.row, c.column)):
                if current_row is None:
                    current_row = cell.row
                if cell.row != current_row:
                    if row_parts:
                        parts.append(f"R{current_row}: " + " | ".join(row_parts))
                    current_row = cell.row
                    row_parts = []
                value = cell.calculated_value if cell.calculated_value is not None else cell.value
                if value is not None and str(value).strip():
                    row_parts.append(f"{cell.coordinate}={str(value).replace(chr(10), ' / ')}")
            if row_parts and current_row is not None:
                parts.append(f"R{current_row}: " + " | ".join(row_parts))
        return "\n".join(parts)[:max_chars]


def _meaningful_coords(ws_formula, ws_values) -> set[tuple[int, int]]:
    coords = set(ws_formula._cells.keys()) | set(ws_values._cells.keys())
    meaningful: set[tuple[int, int]] = set()
    for row, col in coords:
        vf = ws_formula.cell(row, col).value
        vv = ws_values.cell(row, col).value
        if (vf is not None and str(vf).strip()) or (vv is not None and str(vv).strip()):
            meaningful.add((row, col))
    for rng in ws_formula.merged_cells.ranges:
        top = ws_formula.cell(rng.min_row, rng.min_col).value
        if top is not None and str(top).strip():
            for row in range(rng.min_row, rng.max_row + 1):
                for col in range(rng.min_col, rng.max_col + 1):
                    meaningful.add((row, col))
    return meaningful


def _merged_lookup(ws) -> dict[tuple[int, int], Any]:
    lookup: dict[tuple[int, int], Any] = {}
    for rng in ws.merged_cells.ranges:
        top = ws.cell(rng.min_row, rng.min_col).value
        for row in range(rng.min_row, rng.max_row + 1):
            for col in range(rng.min_col, rng.max_col + 1):
                lookup[(row, col)] = top
    return lookup


def read_excel(path: str | Path) -> WorkbookSnapshot:
    path = Path(path)
    wb_formula = load_workbook(path, data_only=False)
    wb_values = load_workbook(path, data_only=True)
    sheets: list[SheetSnapshot] = []
    for ws_formula in wb_formula.worksheets:
        ws_values = wb_values[ws_formula.title]
        coords = _meaningful_coords(ws_formula, ws_values)
        max_row = max((r for r, _ in coords), default=1)
        max_col = max((c for _, c in coords), default=1)
        merged = _merged_lookup(ws_formula)
        sheet = SheetSnapshot(
            name=ws_formula.title,
            max_row=max_row,
            max_column=max_col,
            merged_ranges=[str(r) for r in ws_formula.merged_cells.ranges],
        )
        for row, col in sorted(coords):
            col_letter = get_column_letter(col)
            raw = ws_formula.cell(row, col).value
            calc = ws_values.cell(row, col).value
            formula = raw if isinstance(raw, str) and raw.startswith("=") else None
            sheet.cells.append(CellSnapshot(
                coordinate=f"{col_letter}{row}", row=row, column=col,
                value=raw, formula=formula, calculated_value=calc,
                merged_value=merged.get((row, col), raw),
                hidden_row=bool(ws_formula.row_dimensions[row].hidden),
                hidden_column=bool(ws_formula.column_dimensions[col_letter].hidden),
            ))
        sheets.append(sheet)
    return WorkbookSnapshot(path=str(path), sheets=sheets)
