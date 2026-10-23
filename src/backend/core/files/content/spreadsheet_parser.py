"""Parse XLSX files into markdown tables and bounded previews."""
from __future__ import annotations
import csv
import io
import os
import subprocess
import tempfile
from typing import Optional
from core.files.content.office import find_libreoffice_binary
from core.infra.proc import no_window_kwargs
from core.files.content import text_parser

def deduplicate_headers(header_raw: list[str]) -> list[str]:
    """Disambiguate empty/duplicate header names so {placeholder} rendering
    downstream is unambiguous (e.g., two ``""`` columns become ``col``/``col_1``).
    """
    seen: dict[str, int] = {}
    cleaned: list[str] = []
    for h in header_raw:
        base = h if h else "col"
        if base in seen:
            seen[base] += 1
            cleaned.append(f"{base}_{seen[base]}")
        else:
            seen[base] = 0
            cleaned.append(base)
    return cleaned


def rows_to_markdown(header: list[str], data_rows: list[list[str]],
                      sheet_title: str | None = None) -> str:
    """Convert header + data rows into a markdown table string."""
    lines: list[str] = []
    if sheet_title:
        lines.append(f"## Sheet: {sheet_title}")
        lines.append("")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join("---" for _ in header) + "|")
    for row in data_rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def open_xlsx(file_bytes: bytes):
    """Open a workbook in read-only mode; raise RuntimeError with a Chinese
    install hint if openpyxl is missing."""
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("openpyxl 未安装，无法解析 XLSX 文件（pip install openpyxl）")
    return openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)


def xlsx_sheet_to_markdown(sheet) -> str | None:
    """Render one openpyxl sheet to a markdown table, or None if empty.
    Returns None (not raise) so callers can decide whether emptiness is an
    error (single-sheet mode) or just a skip (whole-workbook mode)."""
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return None
    header = [str(c or "") for c in rows[0]]
    data_rows = [
        [str(c or "") for c in row]
        for row in rows[1:]
        if not all(c is None for c in row)
    ]
    return rows_to_markdown(header, data_rows, sheet.title)


def parse_xlsx(file_bytes: bytes) -> str:
    """Parse XLSX bytes → markdown tables (one per sheet)."""
    wb = open_xlsx(file_bytes)
    try:
        sections = [md for sheet in wb.worksheets if (md := xlsx_sheet_to_markdown(sheet))]
    finally:
        wb.close()
    if not sections:
        raise RuntimeError("XLSX 文件无有效数据")
    return "\n\n".join(sections)


def parse_xlsx_sheet_names(file_bytes: bytes) -> list[str]:
    """Return the list of sheet names in workbook order. Cheap probe: does
    not iterate any rows, just reads workbook structure."""
    wb = open_xlsx(file_bytes)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def parse_xlsx_single_sheet(file_bytes: bytes, sheet_name: str) -> str:
    """Parse only the named sheet → markdown table. Raises RuntimeError if
    the sheet does not exist or is empty."""
    wb = open_xlsx(file_bytes)
    try:
        if sheet_name not in wb.sheetnames:
            raise RuntimeError(
                f"sheet 不存在: '{sheet_name}'。可选 sheet: {wb.sheetnames}"
            )
        md = xlsx_sheet_to_markdown(wb[sheet_name])
        if md is None:
            raise RuntimeError(f"sheet '{sheet_name}' 无数据")
        return md
    finally:
        wb.close()


def default_xlsx_max_cell_chars() -> int:
    """Bound spreadsheet preview cells; the default remains 2000 characters."""
    try:
        return max(50, int(os.getenv("XLSX_PREVIEW_MAX_CELL_CHARS", "2000")))
    except ValueError:
        return 2000


def parse_xlsx_preview(
    file_bytes: bytes,
    *,
    char_budget: int = 4000,
    max_columns: int = 40,
    max_cell_chars: Optional[int] = None,
) -> dict:
    """Parse XLSX into a bounded markdown preview + accurate total counts.

    Produces what the main agent's file_context hook should inject for an
    uploaded spreadsheet: a markdown table containing as many rows as fit
    within ``char_budget``, plus the *real* total_rows / total_columns so
    the agent knows the full size without seeing every row.

    The total_rows count walks the entire sheet (cheap — read_only mode
    streams the first column only). The preview stops as soon as the
    cumulative markdown length exceeds ``char_budget``.

    Returns:
        ``{
            "header": list[str],          # column names (post-dedup)
            "preview_md": str,            # markdown table, ≤ char_budget chars
            "total_rows": int,            # all data rows in sheet 1
            "total_columns": int,         # raw column count (before max_columns)
            "preview_rows": int,          # rows actually included in preview_md
            "truncated_columns": int,     # cols dropped to fit max_columns
            "truncated_cells": int,       # cells truncated to max_cell_chars
        }``

    Reads only the first sheet.
    """
    if max_cell_chars is None:
        max_cell_chars = default_xlsx_max_cell_chars()

    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("openpyxl 未安装，无法解析 XLSX 文件（pip install openpyxl）")

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    try:
        sheet = wb.worksheets[0] if wb.worksheets else None
        if sheet is None:
            raise RuntimeError("XLSX 文件无 sheet")

        rows_iter = sheet.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            raise RuntimeError("XLSX 文件无数据行") from None

        total_columns = len(header_row)
        truncated_columns = max(0, total_columns - max_columns)
        usable_cols = min(total_columns, max_columns)

        header = deduplicate_headers([
            str(c).strip() if c is not None else f"col{idx + 1}"
            for idx, c in enumerate(header_row[:usable_cols])
        ])

        # Markdown header + separator (always present, counts toward budget)
        md_lines: list[str] = [
            "| " + " | ".join(header) + " |",
            "|" + "|".join("---" for _ in header) + "|",
        ]
        running_chars = sum(len(ln) + 1 for ln in md_lines)  # +1 for newline

        preview_rows = 0
        truncated_cells = 0
        total_rows_seen = 0
        budget_reached = False

        for row in rows_iter:
            total_rows_seen += 1
            if all(c is None for c in row):
                continue

            if budget_reached:
                continue  # keep counting total_rows accurately

            cells: list[str] = []
            for val in row[:usable_cols]:
                if val is None:
                    cells.append("")
                    continue
                s = str(val).replace("|", "\\|").replace("\n", " ")
                if len(s) > max_cell_chars:
                    s = s[:max_cell_chars] + "…"
                    truncated_cells += 1
                cells.append(s)

            # Pad short rows so the markdown table stays well-formed
            while len(cells) < len(header):
                cells.append("")

            line = "| " + " | ".join(cells) + " |"
            line_cost = len(line) + 1
            if running_chars + line_cost > char_budget and preview_rows > 0:
                # We have at least one row already; respect the budget.
                budget_reached = True
                continue
            md_lines.append(line)
            running_chars += line_cost
            preview_rows += 1

        return {
            "header": header,
            "preview_md": "\n".join(md_lines),
            "total_rows": total_rows_seen,
            "total_columns": total_columns,
            "preview_rows": preview_rows,
            "truncated_columns": truncated_columns,
            "truncated_cells": truncated_cells,
        }
    finally:
        wb.close()


def parse_xls(file_bytes: bytes) -> str:
    """Parse legacy XLS bytes → markdown tables.

    Tries xlrd first; falls back to LibreOffice → XLSX → openpyxl.
    """
    try:
        import xlrd
        wb = xlrd.open_workbook(file_contents=file_bytes)
        sections: list[str] = []
        for sheet in wb.sheets():
            if sheet.nrows == 0:
                continue
            header = [str(sheet.cell_value(0, c) or "") for c in range(sheet.ncols)]
            data_rows = []
            for r in range(1, sheet.nrows):
                row = [str(sheet.cell_value(r, c) or "") for c in range(sheet.ncols)]
                if all(v == "" for v in row):
                    continue
                data_rows.append(row)
            sections.append(rows_to_markdown(header, data_rows, sheet.name))
        if not sections:
            raise RuntimeError("XLS 文件无有效数据")
        return "\n\n".join(sections)
    except ImportError:
        pass  # xlrd not installed, try LibreOffice fallback

    # Fallback: LibreOffice → XLSX → openpyxl
    xlsx_bytes = convert_xls_to_xlsx(file_bytes)
    return parse_xlsx(xlsx_bytes)


def convert_xls_to_xlsx(file_bytes: bytes) -> bytes:
    """Use LibreOffice headless to convert XLS → XLSX."""
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, "input.xls")
        with open(input_path, "wb") as f:
            f.write(file_bytes)

        try:
            result = subprocess.run(
                [
                    find_libreoffice_binary() or "libreoffice",
                    "--headless",
                    "--convert-to", "xlsx",
                    "--outdir", tmpdir,
                    input_path,
                ],
                capture_output=True,
                text=True,
                timeout=120,
                **no_window_kwargs(),
            )
        except FileNotFoundError:
            raise RuntimeError(
                "xlrd 和 LibreOffice 均不可用，无法解析 XLS 文件"
                "（pip install xlrd 或安装 LibreOffice）"
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("LibreOffice 转换 XLS → XLSX 超时")

        output_path = os.path.join(tmpdir, "input.xlsx")
        if not os.path.exists(output_path):
            raise RuntimeError(f"LibreOffice 转换 XLS 失败: {result.stderr[:300]}")

        with open(output_path, "rb") as f:
            return f.read()


def parse_csv(file_bytes: bytes) -> str:
    """Parse CSV bytes → markdown table."""
    # Decode with encoding detection
    text = text_parser.parse_txt(file_bytes)

    # Sniff dialect
    try:
        sample = text[:8192]
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)

    if not rows:
        raise RuntimeError("CSV 文件无有效数据")

    header = [c.strip() for c in rows[0]]
    data_rows = []
    for row in rows[1:]:
        if all(c.strip() == "" for c in row):
            continue
        # Pad or trim to match header length
        padded = [c.strip() for c in row]
        while len(padded) < len(header):
            padded.append("")
        data_rows.append(padded[:len(header)])

    return rows_to_markdown(header, data_rows)
