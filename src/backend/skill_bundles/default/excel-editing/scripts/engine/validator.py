"""Excel formula validation — thin wrapper around the ported skill script.

The full check logic lives in ``_formula_check_impl.py`` (verbatim copy of
``agent_skills/skills/minimax-xlsx/scripts/formula_check.py``). This wrapper
adapts its file-path interface to the the engine sandbox-cwd convention.

Checks performed (per the upstream module's docstring):
    1. Error-value cells (#REF!, #DIV/0!, etc.)
    2. Broken cross-sheet references
    3. Broken named-range references
    4. Shared formula integrity
    5. Missing <v> on error cells
"""
from __future__ import annotations

from typing import Any

from ._handle import input_path
from . import _formula_check_impl


def validate_formulas(
    *,
    input_filename: str,
    sheet_filter: str | None = None,
) -> dict[str, Any]:
    """Run static formula checks on an xlsx and return the structured report.

    Args:
        input_filename: source .xlsx in cwd
        sheet_filter:   if set, restrict checks to this single sheet

    Returns:
        ``{"file", "sheets_checked", "formula_count", "shared_formula_ranges",
            "error_count", "errors": [{type, message, ...}, ...]}``
    """
    src = str(input_path(input_filename))
    return _formula_check_impl.check(src, sheet_filter=sheet_filter)
