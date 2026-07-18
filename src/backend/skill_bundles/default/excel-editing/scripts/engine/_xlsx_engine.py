#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Internal byte-true xlsx engine (ported from the former minimax-xlsx skill).

Not a public entrypoint. Driven in-process by
``engine.model_builder`` (``cmd_create``) and
``engine.patch_editor`` (``cmd_edit``); the legacy ``main()`` /
argparse CLI is kept only for standalone debugging. Produces or mutates a
complete .xlsx via unpack → XML edit → repack — never an openpyxl round-trip,
so VBA / pivots / sparklines / conditional formatting survive.

- ``cmd_create`` — build a new workbook from a ``workbook`` JSON spec.
- ``cmd_edit``   — mutate an existing ``.xlsx`` using a ``patches`` JSON spec.

Workbook JSON schema (short form)::

    {
      "sheets": [
        {
          "name": "<=31 chars, no / \\ ? * [ ] :>",
          "freeze_header": true,
          "columns": [{"width": 26}, {"width": 14}, ...],
          "rows": [
            {
              "role": "header",                       # row-level default role
              "cells": ["label", "FY2024", "FY2025"]  # shorthand strings
            },
            {
              "cells": [
                "Revenue",                            # string (role inherits row)
                {"value": 1000, "role": "input_int"}, # number with style
                {"formula": "B2*1.1", "role": "formula_int"}  # formula
              ]
            }
          ]
        }
      ]
    }

Role → cellXfs index (pre-built in templates/minimal_xlsx/xl/styles.xml):

    default=0   input=1  formula=2  xref=3  header=4
    input_currency=5   formula_currency=6
    input_pct=7        formula_pct=8
    input_int=9        formula_int=10
    year=11            highlight=12

Formula-First rule is ENFORCED: cells with ``formula`` never get a cached value.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET


SCRIPT_DIR = Path(__file__).resolve().parent  # the engine/ dir (sibling modules + templates/ live here)

# XML comment stripper. lxml (used by openpyxl/Excel) treats comments as real
# children — the template's styles.xml has inline docs like
# "<!-- fills[0] required ... -->" which make openpyxl see 4 fill children
# instead of 3 and die with "Fill() takes no arguments". Stdlib ElementTree
# (used by xlsx_pack.py's validator) drops comments silently so this only
# surfaces at read time, not at pack time.
_XML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

ROLE_TO_STYLE: Dict[str, int] = {
    "default": 0,
    "text": 0,
    "input": 1,
    "formula": 2,
    "xref": 3,
    "header": 4,
    "input_currency": 5,
    "formula_currency": 6,
    "input_pct": 7,
    "formula_pct": 8,
    "input_int": 9,
    "formula_int": 10,
    "year": 11,
    "highlight": 12,
}

FORBIDDEN_SHEET_CHARS = set(r"/\?*[]:")


# ── XML helpers ──────────────────────────────────────────────────────────────

def _xml_escape(text: str) -> str:
    """XML-escape cell text for <t> and attribute values."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def col_letter(n: int) -> str:
    """1-based column index → Excel letter (1→A, 27→AA)."""
    if n < 1:
        raise ValueError(f"column index must be >= 1, got {n}")
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _resolve_style(cell: Any, row_role: Optional[str], default_role: str) -> int:
    """Pick a style index from (cell.role → row.role → default)."""
    role: Optional[str] = None
    if isinstance(cell, dict):
        role = cell.get("role")
    role = role or row_role or default_role
    if role not in ROLE_TO_STYLE:
        raise ValueError(
            f"unknown role {role!r}; allowed: {sorted(ROLE_TO_STYLE)}"
        )
    return ROLE_TO_STYLE[role]


# ── Spec validation ──────────────────────────────────────────────────────────

def _validate_sheet_name(name: str) -> None:
    if not name or not isinstance(name, str):
        raise ValueError(f"sheet name must be a non-empty string, got {name!r}")
    if len(name) > 31:
        raise ValueError(f"sheet name too long (>31 chars): {name!r}")
    bad = FORBIDDEN_SHEET_CHARS & set(name)
    if bad:
        raise ValueError(f"sheet name contains forbidden chars {bad}: {name!r}")


def _validate_workbook(wb: Dict[str, Any]) -> List[Dict[str, Any]]:
    sheets = wb.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        raise ValueError("workbook.sheets must be a non-empty list")
    seen_names: set = set()
    for i, sh in enumerate(sheets):
        if not isinstance(sh, dict):
            raise ValueError(f"sheets[{i}] must be an object")
        name = sh.get("name") or f"Sheet{i + 1}"
        _validate_sheet_name(name)
        if name in seen_names:
            raise ValueError(f"duplicate sheet name: {name!r}")
        seen_names.add(name)
        sh["name"] = name
        rows = sh.get("rows") or []
        if not isinstance(rows, list):
            raise ValueError(f"sheets[{i}].rows must be a list")
    return sheets


# ── Shared strings ───────────────────────────────────────────────────────────

class SharedStrings:
    """Accumulate strings across all sheets, return 0-based index on demand."""

    def __init__(self) -> None:
        self._index: Dict[str, int] = {}
        self._order: List[str] = []
        self.total_refs: int = 0

    def intern(self, text: str) -> int:
        self.total_refs += 1
        if text in self._index:
            return self._index[text]
        idx = len(self._order)
        self._index[text] = idx
        self._order.append(text)
        return idx

    def to_xml(self) -> str:
        if not self._order:
            # Keep a valid empty sst so sharedStrings.xml is always well-formed.
            return (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
                ' count="0" uniqueCount="0"/>\n'
            )
        parts: List[str] = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            (
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
                f' count="{self.total_refs}" uniqueCount="{len(self._order)}">'
            ),
        ]
        for s in self._order:
            esc = _xml_escape(s)
            # Preserve leading/trailing whitespace per OOXML.
            space_attr = ' xml:space="preserve"' if s != s.strip() else ""
            parts.append(f"<si><t{space_attr}>{esc}</t></si>")
        parts.append("</sst>\n")
        return "\n".join(parts)


# ── Cell / row emission ──────────────────────────────────────────────────────

_FORMULA_LEADING_EQ = re.compile(r"^\s*=")


def _emit_cell(
    addr: str,
    cell: Any,
    sst: SharedStrings,
    row_role: Optional[str],
) -> str:
    """Render one <c> element from a cell spec."""
    # Shorthand: bare string → text cell (inherits role)
    if isinstance(cell, (str, int, float, bool)) or cell is None:
        if cell is None:
            return ""  # skip empty — xlsx tolerates gaps
        if isinstance(cell, bool):
            style = _resolve_style({}, row_role, "default")
            return f'<c r="{addr}" t="b" s="{style}"><v>{1 if cell else 0}</v></c>'
        if isinstance(cell, (int, float)):
            style = _resolve_style({}, row_role, "default")
            return f'<c r="{addr}" s="{style}"><v>{cell}</v></c>'
        # string
        idx = sst.intern(cell)
        style = _resolve_style({}, row_role, "default")
        return f'<c r="{addr}" t="s" s="{style}"><v>{idx}</v></c>'

    if not isinstance(cell, dict):
        raise ValueError(f"cell {addr} must be str/number/bool/dict/None, got {type(cell).__name__}")

    # Dict form: formula takes priority, then value.
    if "formula" in cell:
        formula = cell["formula"]
        if not isinstance(formula, str) or not formula.strip():
            raise ValueError(f"cell {addr}.formula must be a non-empty string")
        # Agents often write "=SUM(...)" out of habit. Strip it silently.
        formula = _FORMULA_LEADING_EQ.sub("", formula)
        style = _resolve_style(cell, row_role, "formula")
        return (
            f'<c r="{addr}" s="{style}">'
            f"<f>{_xml_escape(formula)}</f><v></v></c>"
        )

    if "value" in cell:
        val = cell["value"]
        if val is None:
            return ""
        if isinstance(val, bool):
            style = _resolve_style(cell, row_role, "default")
            return f'<c r="{addr}" t="b" s="{style}"><v>{1 if val else 0}</v></c>'
        if isinstance(val, (int, float)):
            style = _resolve_style(cell, row_role, "default")
            return f'<c r="{addr}" s="{style}"><v>{val}</v></c>'
        if isinstance(val, str):
            idx = sst.intern(val)
            style = _resolve_style(cell, row_role, "default")
            return f'<c r="{addr}" t="s" s="{style}"><v>{idx}</v></c>'
        raise ValueError(f"cell {addr}.value must be str/number/bool/null, got {type(val).__name__}")

    raise ValueError(f"cell {addr} dict must contain 'value' or 'formula'")


def _emit_row(row_num: int, row_spec: Dict[str, Any], sst: SharedStrings) -> str:
    cells = row_spec.get("cells") or []
    if not isinstance(cells, list):
        raise ValueError(f"row {row_num} cells must be a list")
    row_role = row_spec.get("role")
    ht_attr = ""
    if "height" in row_spec:
        ht_attr = f' ht="{float(row_spec["height"])}" customHeight="1"'
    parts = [f'<row r="{row_num}"{ht_attr}>']
    for col_idx, cell in enumerate(cells, start=1):
        addr = f"{col_letter(col_idx)}{row_num}"
        xml = _emit_cell(addr, cell, sst, row_role)
        if xml:
            parts.append(xml)
    parts.append("</row>")
    return "".join(parts)


def _emit_cols(columns: Optional[List[Dict[str, Any]]]) -> str:
    if not columns:
        return ""
    parts = ["<cols>"]
    for i, c in enumerate(columns, start=1):
        if not isinstance(c, dict):
            raise ValueError(f"columns[{i - 1}] must be an object")
        width = c.get("width")
        if width is None:
            continue
        parts.append(
            f'<col min="{i}" max="{i}" width="{float(width)}" customWidth="1"/>'
        )
    parts.append("</cols>")
    return "" if len(parts) == 2 else "".join(parts)


def _emit_sheet_xml(sheet: Dict[str, Any], sst: SharedStrings) -> str:
    rows = sheet.get("rows") or []
    cols_xml = _emit_cols(sheet.get("columns"))
    freeze_xml = ""
    if sheet.get("freeze_header"):
        freeze_xml = (
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        )

    row_xmls = [_emit_row(i + 1, r, sst) for i, r in enumerate(rows)]

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheetViews>"
        '<sheetView tabSelected="1" workbookViewId="0">'
        f"{freeze_xml}"
        "</sheetView>"
        "</sheetViews>"
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"{cols_xml}"
        "<sheetData>"
        + "".join(row_xmls)
        + "</sheetData>"
        '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75"'
        ' header="0.3" footer="0.3"/>'
        "</worksheet>\n"
    )


# ── Workbook-level file rewriting ────────────────────────────────────────────

def _write_workbook_xml(work_dir: Path, sheets: List[Dict[str, Any]]) -> None:
    sheet_entries = []
    for i, sh in enumerate(sheets, start=1):
        rid = 1 if i == 1 else i + 2  # rId1=sheet1, rId2=styles, rId3=sst, rId4+=sheet2+
        name_esc = _xml_escape(sh["name"])
        sheet_entries.append(
            f'<sheet name="{name_esc}" sheetId="{i}" r:id="rId{rid}"/>'
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<fileVersion appName="xl" lastEdited="7" lowestEdited="7"/>'
        '<workbookPr defaultThemeVersion="166925"/>'
        "<bookViews>"
        '<workbookView xWindow="0" yWindow="0" windowWidth="20140" windowHeight="10960"/>'
        "</bookViews>"
        "<sheets>"
        + "".join(sheet_entries)
        + "</sheets>"
        '<calcPr calcId="191029"/>'
        "</workbook>\n"
    )
    (work_dir / "xl" / "workbook.xml").write_text(xml, encoding="utf-8")


def _write_workbook_rels(work_dir: Path, n_sheets: int) -> None:
    ns_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    rels: List[str] = [
        f'<Relationship Id="rId1" Type="{ns_rel}/worksheet" Target="worksheets/sheet1.xml"/>',
        f'<Relationship Id="rId2" Type="{ns_rel}/styles" Target="styles.xml"/>',
        f'<Relationship Id="rId3" Type="{ns_rel}/sharedStrings" Target="sharedStrings.xml"/>',
    ]
    for i in range(2, n_sheets + 1):
        rels.append(
            f'<Relationship Id="rId{i + 2}" Type="{ns_rel}/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels)
        + "</Relationships>\n"
    )
    (work_dir / "xl" / "_rels" / "workbook.xml.rels").write_text(xml, encoding="utf-8")


def _write_content_types(work_dir: Path, n_sheets: int) -> None:
    ns_ooxml = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    overrides: List[str] = [
        f'<Override PartName="/xl/workbook.xml" ContentType="{ns_ooxml}.sheet.main+xml"/>',
        f'<Override PartName="/xl/styles.xml" ContentType="{ns_ooxml}.styles+xml"/>',
        f'<Override PartName="/xl/sharedStrings.xml" ContentType="{ns_ooxml}.sharedStrings+xml"/>',
    ]
    for i in range(1, n_sheets + 1):
        overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            f'ContentType="{ns_ooxml}.worksheet+xml"/>'
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + "".join(overrides)
        + "</Types>\n"
    )
    (work_dir / "[Content_Types].xml").write_text(xml, encoding="utf-8")


# ── EDIT primitives ──────────────────────────────────────────────────────────
#
# EDIT is fundamentally "unpack XML → mutate → pack XML". To keep the contract
# with callers simple (one CLI call, one output file), we do the whole pipeline
# (unpack, apply every patch, pack) inside ONE invocation. That's what cmd_edit
# does. The individual _apply_<op>_patch functions below each do one XML-surgery
# step on the already-unpacked working directory.
#
# The skill's own scripts (xlsx_insert_row.py / xlsx_add_column.py /
# xlsx_shift_rows.py) are battle-tested; we shell out to them for the ops they
# cover and only hand-roll the XML mutations they don't (set_cell,
# replace_text, rename_sheet, delete_row).

# Register the main OOXML namespace so ElementTree emits bare tag names on
# write instead of ns0:foo. Also expose the namespace dict for findall calls.
_OOXML_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OOXML_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS = {"main": _OOXML_MAIN, "r": _OOXML_RELS}
ET.register_namespace("", _OOXML_MAIN)
ET.register_namespace("r", _OOXML_RELS)


def _col_letter_to_num(letters: str) -> int:
    """Inverse of col_letter — 'A'→1, 'AA'→27. Raises ValueError on bad input."""
    if not letters or not letters.isalpha():
        raise ValueError(f"invalid column letter: {letters!r}")
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _col_num_to_letter(num: int) -> str:
    """Inverse of _col_letter_to_num — 1→'A', 27→'AA'. Raises on bad input."""
    if num < 1:
        raise ValueError(f"invalid column number: {num!r}")
    letters = ""
    while num > 0:
        num, rem = divmod(num - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


_CELL_ADDR_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _split_cell_addr(addr: str) -> Tuple[str, int]:
    """'B3' → ('B', 3). Raises on bad input."""
    m = _CELL_ADDR_RE.match(addr.strip())
    if not m:
        raise ValueError(f"invalid cell address: {addr!r}")
    return m.group(1).upper(), int(m.group(2))


def _resolve_sheet_map(work: Path) -> Dict[str, Path]:
    """Map sheet display name → worksheet XML path (by walking workbook.xml + rels)."""
    wb = ET.parse(work / "xl" / "workbook.xml").getroot()
    rels = ET.parse(work / "xl" / "_rels" / "workbook.xml.rels").getroot()
    rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    rid_to_target = {
        r.get("Id"): r.get("Target")
        for r in rels.findall(f"{rel_ns}Relationship")
    }
    out: Dict[str, Path] = {}
    for sh in wb.findall("main:sheets/main:sheet", _NS):
        name = sh.get("name")
        rid = sh.get(f"{{{_OOXML_RELS}}}id")
        target = rid_to_target.get(rid)
        if not target:
            continue
        # Target can be package-absolute ("/xl/worksheets/sheet1.xml")
        # or relative to xl/ ("worksheets/sheet1.xml").
        if target.startswith("/"):
            out[name] = work / target.lstrip("/")
        else:
            out[name] = work / "xl" / target
    return out


class _SharedStringsMutator:
    """Load an existing xl/sharedStrings.xml, allow intern() to append new
    strings, bump count/uniqueCount on flush.

    For EDIT we must preserve existing indices — all existing cells with
    ``t="s"`` reference them by position.

    If the source workbook had no ``xl/sharedStrings.xml`` and a patch
    introduces strings (add_column ``header`` / set_cell with a ``value`` of
    type str), ``flush()`` will create the part — but the workbook manifests
    (``[Content_Types].xml`` and ``xl/_rels/workbook.xml.rels``) must also
    declare the new part, otherwise Excel/LibreOffice silently render the
    string cells as blank and openpyxl raises IndexError trying to look up
    the missing shared-strings table. Pass ``workbook_root`` so we can patch
    those two manifests idempotently on first flush.
    """

    def __init__(self, path: Path, *, workbook_root: Path | None = None) -> None:
        self._path = path
        self._workbook_root = workbook_root
        self._existed_at_load = path.is_file()
        self._dirty = False
        self._order: List[str] = []
        self._index: Dict[str, int] = {}
        self._total_refs = 0  # count attribute — total cell references to strings

        if self._existed_at_load:
            tree = ET.parse(path)
            root = tree.getroot()
            count_attr = root.get("count")
            self._total_refs = int(count_attr) if count_attr and count_attr.isdigit() else 0
            for si in root.findall("main:si", _NS):
                # Standard case: <si><t>text</t></si>. Rich-text <si> has multiple <r>
                # children; we preserve them by serializing the full inner XML as the key.
                t = si.find("main:t", _NS)
                if t is not None and len(list(si)) == 1:
                    text = t.text or ""
                else:
                    # Rich-text or unusual <si> — keep as-is, key by serialized form.
                    text = ET.tostring(si, encoding="unicode")
                self._order.append(text)
                # First occurrence wins for dedup lookup
                self._index.setdefault(text, len(self._order) - 1)
        else:
            # No sharedStrings.xml yet — create an empty one on flush.
            self._dirty = True

    def intern(self, text: str) -> int:
        """Return 0-based index of ``text``, appending a new entry if needed.
        Does NOT increment the total-refs counter — call bump_ref() after
        actually writing the cell that references it."""
        if text in self._index:
            return self._index[text]
        idx = len(self._order)
        self._order.append(text)
        self._index[text] = idx
        self._dirty = True
        return idx

    def bump_ref(self, delta: int = 1) -> None:
        """Increment the ``count`` attribute to reflect another cell reference."""
        self._total_refs += delta
        self._dirty = True

    def replace_text(self, search: str, replace: str) -> int:
        """Apply text replacement to every stored string. Returns hit count."""
        hits = 0
        for i, s in enumerate(self._order):
            # Only touch plain-text entries (no angle brackets from tostring()).
            if "<" not in s and search in s:
                new = s.replace(search, replace)
                self._order[i] = new
                hits += new.count(replace) - s.count(replace)
        if hits:
            # Rebuild the index; old keys may now collide — last-write wins is fine
            # because existing cell references still point by position, not key.
            self._index = {s: i for i, s in enumerate(self._order)}
            self._dirty = True
        return hits

    def flush(self) -> None:
        if not self._dirty:
            return
        parts: List[str] = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            (
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
                f' count="{max(self._total_refs, len(self._order))}"'
                f' uniqueCount="{len(self._order)}">'
            ),
        ]
        for s in self._order:
            if s.startswith("<si"):
                # Serialized rich-text <si> — emit as-is.
                parts.append(s)
            else:
                space_attr = ' xml:space="preserve"' if s != s.strip() else ""
                parts.append(f"<si><t{space_attr}>{_xml_escape(s)}</t></si>")
        parts.append("</sst>\n")
        self._path.write_text("\n".join(parts), encoding="utf-8")
        self._dirty = False

        # If this is the first time we materialized sharedStrings.xml, the
        # workbook manifests don't reference it yet — patch them.
        if not self._existed_at_load and self._workbook_root is not None:
            self._register_sharedstrings_in_manifests()
            # Only register once; subsequent flushes are pure rewrites of
            # the part itself.
            self._existed_at_load = True

    def _register_sharedstrings_in_manifests(self) -> None:
        """Idempotently ensure [Content_Types].xml + workbook.xml.rels both
        declare xl/sharedStrings.xml.

        Without this, applications that read the workbook by walking the
        Open Packaging Convention manifests (Excel, LibreOffice Calc,
        openpyxl) never load the shared-strings table, so every cell with
        ``t="s"`` renders blank or raises IndexError. The pack/unpack
        scripts blindly copy what's on disk, so the workbook manifests are
        the authoritative source.
        """
        if self._workbook_root is None:
            return

        # ── [Content_Types].xml ──
        ct_path = self._workbook_root / "[Content_Types].xml"
        if ct_path.is_file():
            ct = ct_path.read_text(encoding="utf-8")
            if "sharedStrings.xml" not in ct:
                override = (
                    '<Override PartName="/xl/sharedStrings.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.'
                    'spreadsheetml.sharedStrings+xml"/>'
                )
                # Insert before the closing </Types>. The exact serialization of
                # the closing tag varies (with/without leading whitespace), so be
                # tolerant.
                if "</Types>" in ct:
                    ct = ct.replace("</Types>", f"{override}</Types>")
                    ct_path.write_text(ct, encoding="utf-8")

        # ── xl/_rels/workbook.xml.rels ──
        rels_path = self._workbook_root / "xl" / "_rels" / "workbook.xml.rels"
        if rels_path.is_file():
            rels = rels_path.read_text(encoding="utf-8")
            if "sharedStrings.xml" not in rels:
                # Pick the next free rId so we don't collide with existing
                # relationships (styles / theme / sheets all consume rIds).
                existing_ids = [int(m) for m in re.findall(r'Id="rId(\d+)"', rels)]
                next_id = (max(existing_ids) + 1) if existing_ids else 1
                rel = (
                    f'<Relationship Id="rId{next_id}" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    'relationships/sharedStrings" Target="sharedStrings.xml"/>'
                )
                if "</Relationships>" in rels:
                    rels = rels.replace("</Relationships>", f"{rel}</Relationships>")
                    rels_path.write_text(rels, encoding="utf-8")


def _find_sheet_xml(sheet_map: Dict[str, Path], requested: Optional[str]) -> Path:
    """Pick the target sheet: explicit name, or first sheet if None."""
    if requested is None:
        if not sheet_map:
            raise ValueError("workbook has no sheets")
        # dict order == workbook.xml declaration order
        return next(iter(sheet_map.values()))
    if requested not in sheet_map:
        raise ValueError(
            f"sheet {requested!r} not found; available: {sorted(sheet_map)}"
        )
    return sheet_map[requested]


def _get_or_create_row(sheet_data: ET.Element, row_num: int) -> ET.Element:
    """Find <row r="N"> inside <sheetData>, creating it (in sorted position)
    if absent."""
    existing = sheet_data.find(f"main:row[@r='{row_num}']", _NS)
    if existing is not None:
        return existing
    new_row = ET.Element(f"{{{_OOXML_MAIN}}}row", {"r": str(row_num)})
    # Insert in row-number order so Excel is happy.
    insert_at = len(sheet_data)
    for i, sibling in enumerate(list(sheet_data)):
        sib_r = sibling.get("r")
        if sib_r and sib_r.isdigit() and int(sib_r) > row_num:
            insert_at = i
            break
    sheet_data.insert(insert_at, new_row)
    return new_row


def _get_or_create_cell(row_el: ET.Element, addr: str) -> ET.Element:
    """Find <c r="addr"> inside a <row>, creating it in column-sorted position."""
    existing = row_el.find(f"main:c[@r='{addr}']", _NS)
    if existing is not None:
        return existing
    col_letters, _ = _split_cell_addr(addr)
    col_num = _col_letter_to_num(col_letters)
    new_c = ET.Element(f"{{{_OOXML_MAIN}}}c", {"r": addr})
    insert_at = len(row_el)
    for i, sibling in enumerate(list(row_el)):
        sib_addr = sibling.get("r") or ""
        m = _CELL_ADDR_RE.match(sib_addr)
        if m and _col_letter_to_num(m.group(1)) > col_num:
            insert_at = i
            break
    row_el.insert(insert_at, new_c)
    return new_c


# Sentinel for "argument was not provided" (distinguishes from explicit None).
class _Missing:
    pass
_MISSING: Any = _Missing()


def _set_cell_payload(
    c_el: ET.Element,
    *,
    sst: "_SharedStringsMutator",
    value: Any = _MISSING,
    formula: Optional[str] = None,
    style_idx: Optional[int] = None,
) -> None:
    """Rewrite the children (and type attribute) of a <c> element."""
    # Strip existing children and the t attribute; we're replacing them.
    for child in list(c_el):
        c_el.remove(child)
    if "t" in c_el.attrib:
        del c_el.attrib["t"]

    if style_idx is not None:
        c_el.set("s", str(style_idx))

    if formula is not None:
        # Formula path: <f>expr</f><v/>
        f_el = ET.SubElement(c_el, f"{{{_OOXML_MAIN}}}f")
        f_el.text = _FORMULA_LEADING_EQ.sub("", formula)
        ET.SubElement(c_el, f"{{{_OOXML_MAIN}}}v").text = ""
        return

    # Value path — dispatch by type.
    if value is _MISSING or value is None:
        # Clear cell contents but keep the <c> shell with its style.
        return
    if isinstance(value, bool):
        c_el.set("t", "b")
        ET.SubElement(c_el, f"{{{_OOXML_MAIN}}}v").text = "1" if value else "0"
        return
    if isinstance(value, (int, float)):
        ET.SubElement(c_el, f"{{{_OOXML_MAIN}}}v").text = str(value)
        return
    if isinstance(value, str):
        idx = sst.intern(value)
        sst.bump_ref(1)
        c_el.set("t", "s")
        ET.SubElement(c_el, f"{{{_OOXML_MAIN}}}v").text = str(idx)
        return
    raise ValueError(f"unsupported cell value type: {type(value).__name__}")


def _extend_dimension(root: ET.Element, col_num: int, row_num: int) -> None:
    """Grow the worksheet's ``<dimension ref>`` so it covers (col_num, row_num).

    ``set_cell`` can create a ``<row>``/``<c>`` past the stored used-range
    marker. Excel re-derives the range when it opens the file, but
    SheetJS-based viewers (the in-app xlsx preview) trust ``<dimension>``
    verbatim and silently drop any cell outside it. If the sheet has no
    ``<dimension>`` we leave it alone — SheetJS then scans the actual cells,
    which is already correct. Extend-only: a deliberately wider range is kept.
    """
    dim = root.find("main:dimension", _NS)
    if dim is None:
        return
    old_ref = dim.get("ref", "")
    if not old_ref:
        return
    start_ref, _, end_ref = old_ref.partition(":")
    sm = _CELL_ADDR_RE.match(start_ref)
    em = _CELL_ADDR_RE.match(end_ref or start_ref)
    if not sm or not em:
        return
    s_col, s_row = _col_letter_to_num(sm.group(1)), int(sm.group(2))
    e_col, e_row = _col_letter_to_num(em.group(1)), int(em.group(2))
    new_ref = (
        f"{_col_num_to_letter(min(s_col, col_num))}{min(s_row, row_num)}"
        f":{_col_num_to_letter(max(e_col, col_num))}{max(e_row, row_num)}"
    )
    if new_ref != old_ref:
        dim.set("ref", new_ref)


def _apply_set_cell(
    patch: Dict[str, Any],
    sheet_map: Dict[str, Path],
    sst: _SharedStringsMutator,
) -> str:
    """Handle op=set_cell and op=fix_formula.

    fix_formula is just set_cell where you're pointing at an existing <f>.
    Both require ``cell``; one of ``value`` or ``formula`` must be provided.
    """
    sheet_xml = _find_sheet_xml(sheet_map, patch.get("sheet"))
    addr = patch.get("cell")
    if not addr:
        raise ValueError("set_cell requires 'cell' (e.g. 'B3')")
    col_letters, row_num = _split_cell_addr(addr)

    has_formula = "formula" in patch
    has_value = "value" in patch
    if not has_formula and not has_value:
        raise ValueError(f"set_cell at {addr} requires 'value' or 'formula'")
    if has_formula and has_value:
        raise ValueError(f"set_cell at {addr} cannot have both 'value' and 'formula'")

    role = patch.get("role")
    style_idx = ROLE_TO_STYLE[role] if role is not None else None
    if role is not None and role not in ROLE_TO_STYLE:
        raise ValueError(f"unknown role {role!r}; allowed: {sorted(ROLE_TO_STYLE)}")

    tree = ET.parse(sheet_xml)
    root = tree.getroot()
    sheet_data = root.find("main:sheetData", _NS)
    if sheet_data is None:
        raise ValueError(f"sheet xml {sheet_xml.name} has no <sheetData>")

    row = _get_or_create_row(sheet_data, row_num)
    cell = _get_or_create_cell(row, addr)
    _set_cell_payload(
        cell,
        value=patch["value"] if has_value else _MISSING,
        formula=patch["formula"] if has_formula else None,
        style_idx=style_idx,
        sst=sst,
    )
    _extend_dimension(root, _col_letter_to_num(col_letters), row_num)
    tree.write(sheet_xml, xml_declaration=True, encoding="utf-8")
    return f"set {addr} in {sheet_xml.stem}"


def _apply_replace_text(
    patch: Dict[str, Any],
    sheet_map: Dict[str, Path],
    sst: _SharedStringsMutator,
) -> str:
    """Replace all occurrences of ``search`` with ``replace`` in sharedStrings
    (and inline <is> strings if any). By default affects the whole workbook —
    pass ``sheet`` to limit to one sheet's inline strings; shared strings are
    always global."""
    search = patch.get("search")
    replace = patch.get("replace")
    if not isinstance(search, str) or not search:
        raise ValueError("replace_text requires a non-empty 'search' string")
    if not isinstance(replace, str):
        raise ValueError("replace_text requires a 'replace' string")

    sst_hits = sst.replace_text(search, replace)

    # Inline strings (<is><t>…</t></is>) are rare but possible; handle them too.
    inline_hits = 0
    targets = (
        [sheet_map[patch["sheet"]]] if patch.get("sheet") else list(sheet_map.values())
    )
    for sheet_xml in targets:
        tree = ET.parse(sheet_xml)
        dirty = False
        for t in tree.getroot().iter(f"{{{_OOXML_MAIN}}}t"):
            # Only rewrite inline-string <t> nodes (parent is <is>), not shared-string ones.
            # ElementTree doesn't expose parent, but <t> under <is> is always inline; <t>
            # under <si> lives in sharedStrings.xml which we already handled.
            if t.text and search in t.text:
                t.text = t.text.replace(search, replace)
                inline_hits += 1
                dirty = True
        if dirty:
            tree.write(sheet_xml, xml_declaration=True, encoding="utf-8")
    return f"replace_text: {sst_hits} in sharedStrings, {inline_hits} inline"


def _apply_rename_sheet(
    patch: Dict[str, Any],
    work: Path,
) -> str:
    """Rename a sheet in workbook.xml. If ``update_formulas`` (default True),
    also rewrite SheetName! references in every <f> across the workbook."""
    old = patch.get("from")
    new = patch.get("to")
    if not isinstance(old, str) or not isinstance(new, str):
        raise ValueError("rename_sheet requires string 'from' and 'to'")
    _validate_sheet_name(new)

    wb_path = work / "xl" / "workbook.xml"
    tree = ET.parse(wb_path)
    root = tree.getroot()
    target = None
    for sh in root.findall("main:sheets/main:sheet", _NS):
        if sh.get("name") == old:
            target = sh
            break
    if target is None:
        raise ValueError(f"rename_sheet: source {old!r} not found")
    target.set("name", new)
    tree.write(wb_path, xml_declaration=True, encoding="utf-8")

    if not patch.get("update_formulas", True):
        return f"renamed sheet {old!r} → {new!r} (formulas NOT updated)"

    # Rewrite formula references. Two forms to handle:
    #   Unquoted:  OldName!A1      → NewName!A1   (or 'NewName'!A1 if new needs quoting)
    #   Quoted:    'Old Name'!A1   → 'New Name'!A1 (and 'It''s'!A1 escapes)
    # Whether the NEW name needs to be quoted in a formula: any char outside
    # [A-Za-z0-9_] forces quoting (space, punctuation, CJK-safe so long as the
    # char class doesn't explicitly exclude it — but Excel is conservative, so
    # use a strict safe set).
    _SAFE_SHEET = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    needs_quote = not _SAFE_SHEET.match(new)
    quoted_new = new.replace("'", "''")
    new_ref = f"'{quoted_new}'!" if needs_quote else f"{new}!"

    escaped_old = re.escape(old)
    unquoted_re = re.compile(
        rf"(?:(?<=^)|(?<=[^A-Za-z0-9_\']))({escaped_old})!"
    )
    quoted_old = old.replace("'", "''")
    quoted_re = re.compile(rf"'({re.escape(quoted_old)})'!")

    formula_hits = 0
    for sheet_xml in (work / "xl" / "worksheets").glob("*.xml"):
        tree = ET.parse(sheet_xml)
        dirty = False
        for f in tree.getroot().iter(f"{{{_OOXML_MAIN}}}f"):
            if not f.text:
                continue
            original = f.text
            # Quoted references first so we don't double-mangle.
            updated = quoted_re.sub(new_ref, original)
            updated = unquoted_re.sub(new_ref, updated)
            if updated != original:
                f.text = updated
                formula_hits += 1
                dirty = True
        if dirty:
            tree.write(sheet_xml, xml_declaration=True, encoding="utf-8")
    return f"renamed sheet {old!r} → {new!r}, rewrote {formula_hits} formulas"


def _apply_delete_row(
    patch: Dict[str, Any],
    work: Path,
    sheet_map: Dict[str, Path],
) -> str:
    """Delete a row at ``at`` in the given sheet, then shift all subsequent
    row references up by one."""
    at = patch.get("at")
    if not isinstance(at, int) or at < 1:
        raise ValueError("delete_row requires integer 'at' >= 1")
    sheet_xml = _find_sheet_xml(sheet_map, patch.get("sheet"))

    tree = ET.parse(sheet_xml)
    root = tree.getroot()
    sheet_data = root.find("main:sheetData", _NS)
    if sheet_data is None:
        raise ValueError(f"sheet {sheet_xml.name} has no <sheetData>")

    target = sheet_data.find(f"main:row[@r='{at}']", _NS)
    if target is None:
        raise ValueError(f"delete_row: row {at} not found in {sheet_xml.stem}")
    sheet_data.remove(target)
    tree.write(sheet_xml, xml_declaration=True, encoding="utf-8")

    shift_script = _find_sibling_script("xlsx_shift_rows.py")
    r = subprocess.run(
        [sys.executable, str(shift_script), str(work), "delete", str(at), "1"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"xlsx_shift_rows.py failed: {r.stderr or r.stdout}")
    return f"deleted row {at} in {sheet_xml.stem}"


def _apply_insert_row(patch: Dict[str, Any], work: Path) -> str:
    """Wrap xlsx_insert_row.py. patch fields: sheet, at, text{col:str},
    values{col:num}, formulas{col:str}, copy_style_from."""
    at = patch.get("at")
    if not isinstance(at, int) or at < 1:
        raise ValueError("insert_row requires integer 'at' >= 1")

    cli_args: List[str] = ["--at", str(at)]
    if patch.get("sheet"):
        cli_args += ["--sheet", str(patch["sheet"])]

    for key, flag in (("text", "--text"), ("values", "--values"), ("formulas", "--formula")):
        payload = patch.get(key)
        if payload is None:
            continue
        if not isinstance(payload, dict):
            raise ValueError(f"insert_row.{key} must be an object (col→value)")
        kv = [f"{col}={val}" for col, val in payload.items()]
        if kv:
            cli_args += [flag, *kv]

    if "copy_style_from" in patch:
        cli_args += ["--copy-style-from", str(patch["copy_style_from"])]

    script = _find_sibling_script("xlsx_insert_row.py")
    r = subprocess.run(
        [sys.executable, str(script), str(work), *cli_args],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"xlsx_insert_row.py failed: {r.stderr or r.stdout}")
    return f"inserted row at {at}"


def _apply_add_column(patch: Dict[str, Any], work: Path) -> str:
    """Wrap xlsx_add_column.py. patch fields: sheet, col, header, formula,
    formula_rows, total_row, total_formula, numfmt, border_row, border_style."""
    col = patch.get("col")
    if not isinstance(col, str) or not col.isalpha():
        raise ValueError("add_column requires 'col' as a letter (e.g. 'G')")

    cli_args: List[str] = ["--col", col.upper()]
    flag_map = {
        "sheet": "--sheet",
        "header": "--header",
        "formula": "--formula",
        "formula_rows": "--formula-rows",
        "total_row": "--total-row",
        "total_formula": "--total-formula",
        "numfmt": "--numfmt",
        "border_row": "--border-row",
        "border_style": "--border-style",
    }
    for key, flag in flag_map.items():
        if key in patch and patch[key] is not None:
            cli_args += [flag, str(patch[key])]

    script = _find_sibling_script("xlsx_add_column.py")
    r = subprocess.run(
        [sys.executable, str(script), str(work), *cli_args],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"xlsx_add_column.py failed: {r.stderr or r.stdout}")
    return f"added column {col.upper()}"


def _find_sibling_script(name: str) -> Path:
    """Locate a peer script (e.g. xlsx_unpack.py). Same search rule as
    _find_pack_script — same dir as xlsx_cli.py, or cwd/scripts/."""
    for c in (SCRIPT_DIR / name, Path.cwd() / "scripts" / name):
        if c.is_file():
            return c
    raise FileNotFoundError(f"cannot locate {name}")


# ── Template locator ─────────────────────────────────────────────────────────

def _find_template(explicit: Optional[str]) -> Path:
    """Locate templates/minimal_xlsx directory.

    Search order:
      1. --template CLI flag
      2. ./templates/minimal_xlsx (sandbox cwd — resource_files seeded by sidecar)
      3. <engine>/templates/minimal_xlsx (vendored alongside the engine)
    """
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path.cwd() / "templates" / "minimal_xlsx")
    candidates.append(SCRIPT_DIR / "templates" / "minimal_xlsx")
    for c in candidates:
        if c.is_dir() and (c / "[Content_Types].xml").is_file():
            return c
    raise FileNotFoundError(
        "cannot locate templates/minimal_xlsx; searched: "
        + ", ".join(str(c) for c in candidates)
    )


def _find_pack_script() -> Path:
    """Locate xlsx_pack.py (same dir as this script, or cwd/scripts/)."""
    for c in (SCRIPT_DIR / "xlsx_pack.py", Path.cwd() / "scripts" / "xlsx_pack.py"):
        if c.is_file():
            return c
    raise FileNotFoundError("cannot locate xlsx_pack.py")


# ── Main create pipeline ─────────────────────────────────────────────────────

def cmd_create(args: argparse.Namespace, stdin_params: Dict[str, Any]) -> int:
    workbook = stdin_params.get("workbook")
    if workbook is None:
        # also accept --workbook-json file path for local use
        if args.workbook_json:
            workbook = json.loads(Path(args.workbook_json).read_text(encoding="utf-8"))
        else:
            print(
                "ERROR: stdin JSON must contain 'workbook' key (or pass --workbook-json FILE)",
                file=sys.stderr,
            )
            return 2

    sheets = _validate_workbook(workbook)

    template_dir = _find_template(args.template)
    pack_script = _find_pack_script()

    with tempfile.TemporaryDirectory(prefix="xlsx_work_") as tmp:
        work = Path(tmp) / "book"
        shutil.copytree(template_dir, work)

        # Strip XML comments from every file we inherited from the template.
        # See _XML_COMMENT_RE for why.
        for xml_path in list(work.rglob("*.xml")) + list(work.rglob("*.rels")):
            text = xml_path.read_text(encoding="utf-8")
            stripped = _XML_COMMENT_RE.sub("", text)
            if stripped != text:
                xml_path.write_text(stripped, encoding="utf-8")

        # Wipe the template's placeholder sheet1.xml — we regenerate all sheets.
        for existing in (work / "xl" / "worksheets").glob("*.xml"):
            existing.unlink()

        # Build shared strings while emitting each sheet.
        sst = SharedStrings()
        for i, sh in enumerate(sheets, start=1):
            sheet_xml = _emit_sheet_xml(sh, sst)
            (work / "xl" / "worksheets" / f"sheet{i}.xml").write_text(
                sheet_xml, encoding="utf-8"
            )

        (work / "xl" / "sharedStrings.xml").write_text(sst.to_xml(), encoding="utf-8")
        _write_workbook_xml(work, sheets)
        _write_workbook_rels(work, len(sheets))
        _write_content_types(work, len(sheets))

        # Pack using the canonical pack script (it re-validates XML well-formedness).
        out_path = Path(args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [sys.executable, str(pack_script), str(work), str(out_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            sys.stderr.write(result.stderr or result.stdout)
            return result.returncode
        sys.stdout.write(result.stdout)

    summary = {
        "output": str(out_path),
        "sheets": [sh["name"] for sh in sheets],
        "shared_strings": len(sst._order),  # noqa: SLF001 — internal but fine for reporting
    }
    print(f"\n[xlsx_cli] {json.dumps(summary, ensure_ascii=False)}")
    return 0


def cmd_edit(args: argparse.Namespace, stdin_params: Dict[str, Any]) -> int:
    """Apply a list of patches to an existing .xlsx in a single call.

    patches[*].op ∈ {set_cell, fix_formula, replace_text,
                     insert_row, add_column, rename_sheet, delete_row}
    """
    patches = stdin_params.get("patches")
    if patches is None:
        if args.patches_json:
            patches = json.loads(Path(args.patches_json).read_text(encoding="utf-8"))
            if isinstance(patches, dict) and "patches" in patches:
                patches = patches["patches"]
        else:
            print(
                "ERROR: stdin JSON must contain 'patches' list (or pass --patches-json FILE)",
                file=sys.stderr,
            )
            return 2
    if not isinstance(patches, list):
        print("ERROR: 'patches' must be a list", file=sys.stderr)
        return 2

    input_path = Path(args.input).resolve()
    if not input_path.is_file():
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        return 2

    unpack_script = _find_sibling_script("xlsx_unpack.py")
    pack_script = _find_pack_script()

    applied_log: List[str] = []
    with tempfile.TemporaryDirectory(prefix="xlsx_edit_") as tmp:
        work = Path(tmp) / "book"
        # 1. Unpack
        r = subprocess.run(
            [sys.executable, str(unpack_script), str(input_path), str(work)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            sys.stderr.write(r.stderr or r.stdout)
            return r.returncode

        # 2. Apply patches.
        sst_path = work / "xl" / "sharedStrings.xml"
        sst = _SharedStringsMutator(sst_path, workbook_root=work)
        sheet_map = _resolve_sheet_map(work)

        # Some ops (rename_sheet, add_column, insert_row, delete_row) may change
        # sheet names / add rows. After each such op we invalidate sst/sheet_map
        # so the next patch sees the fresh state.
        invalidates = {"rename_sheet", "add_column", "insert_row", "delete_row"}

        for i, patch in enumerate(patches):
            if not isinstance(patch, dict):
                raise ValueError(f"patches[{i}] must be an object")
            op = patch.get("op")
            if not op:
                raise ValueError(f"patches[{i}] missing 'op'")

            if op in ("set_cell", "fix_formula"):
                msg = _apply_set_cell(patch, sheet_map, sst)
            elif op == "replace_text":
                msg = _apply_replace_text(patch, sheet_map, sst)
            elif op == "rename_sheet":
                sst.flush()  # flush before external / cross-file mutation
                msg = _apply_rename_sheet(patch, work)
            elif op == "insert_row":
                sst.flush()
                msg = _apply_insert_row(patch, work)
            elif op == "add_column":
                sst.flush()
                msg = _apply_add_column(patch, work)
            elif op == "delete_row":
                sst.flush()
                msg = _apply_delete_row(patch, work, sheet_map)
            else:
                raise ValueError(
                    f"patches[{i}].op {op!r} is not supported; "
                    "choose from: set_cell, fix_formula, replace_text, "
                    "insert_row, add_column, rename_sheet, delete_row"
                )

            applied_log.append(f"[{i}] {op}: {msg}")

            if op in invalidates:
                # Reload state from disk for the next patch.
                sst = _SharedStringsMutator(sst_path, workbook_root=work)
                sheet_map = _resolve_sheet_map(work)

        sst.flush()

        # 3. Pack.
        out_path = Path(args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [sys.executable, str(pack_script), str(work), str(out_path)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            sys.stderr.write(r.stderr or r.stdout)
            return r.returncode
        sys.stdout.write(r.stdout)

    summary = {"output": str(out_path), "patches_applied": len(patches)}
    for line in applied_log:
        print(line)
    print(f"\n[xlsx_cli] {json.dumps(summary, ensure_ascii=False)}")
    return 0


def _read_stdin_params() -> Dict[str, Any]:
    """Read stdin JSON; return {} if stdin is a TTY or empty."""
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: stdin is not valid JSON: {e}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(obj, dict):
        print("ERROR: stdin JSON must be an object", file=sys.stderr)
        sys.exit(2)
    return obj


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="xlsx_cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Create a new xlsx from a workbook JSON spec")
    p_create.add_argument("--output", required=True, help="output .xlsx path")
    p_create.add_argument(
        "--template",
        help="path to minimal_xlsx template dir (defaults to sandbox-seeded or skill dir)",
    )
    p_create.add_argument(
        "--workbook-json",
        help="(local use) read workbook spec from this JSON file instead of stdin",
    )

    p_edit = sub.add_parser("edit", help="Apply a patches JSON to an existing xlsx")
    p_edit.add_argument("--input", required=True, help="input .xlsx path")
    p_edit.add_argument("--output", required=True, help="output .xlsx path")
    p_edit.add_argument(
        "--patches-json",
        help="(local use) read patches spec from this JSON file instead of stdin",
    )

    args = parser.parse_args(argv)
    stdin_params = _read_stdin_params()

    try:
        if args.command == "create":
            return cmd_create(args, stdin_params)
        if args.command == "edit":
            return cmd_edit(args, stdin_params)
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
