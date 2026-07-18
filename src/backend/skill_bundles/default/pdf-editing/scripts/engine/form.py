"""PDF form-field inspection and filling.

Ported from ``agent_skills/skills/minimax-pdf/scripts/{fill_inspect,fill_write}.py``.
The original CLI scripts are kept as-is for the legacy skill path; this module
preserves their core algorithms with the same field-type detection and
checkbox/radio handling, adapted for the the engine sandbox-cwd convention.

Field types recognized:
    - text       (``/FT == /Tx``)
    - checkbox   (``/FT == /Btn`` without push-button bit)
    - radio      (``/FT == /Btn`` with bit 15 set)
    - dropdown   (``/FT == /Ch`` with combo bit 17 set)
    - listbox    (``/FT == /Ch`` without combo bit)
    - signature  (``/FT == /Sig``)  — read-only
"""
from __future__ import annotations

from typing import Any

from ._handle import input_path, output_path


# ── Field-type helpers (shared between inspect and fill) ────────────────────


def _field_type(field) -> str:
    ft = field.get("/FT")
    if ft is None:
        return "unknown"
    ft = str(ft)
    if ft == "/Tx":
        return "text"
    if ft == "/Btn":
        ff = int(field.get("/Ff", 0))
        return "radio" if ff & (1 << 15) else "checkbox"
    if ft == "/Ch":
        ff = int(field.get("/Ff", 0))
        return "dropdown" if ff & (1 << 17) else "listbox"
    if ft == "/Sig":
        return "signature"
    return "unknown"


def _field_value(field) -> str | None:
    v = field.get("/V")
    return str(v) if v is not None else None


def _checkbox_on_value(field) -> str:
    """The /AP /N key that means 'checked' (anything except /Off)."""
    ap = field.get("/AP")
    if ap and "/N" in ap:
        for k in ap["/N"]:
            if str(k) != "/Off":
                return str(k)
    return "/Yes"


def _dropdown_choices(field) -> list[dict[str, str]]:
    from pypdf.generic import ArrayObject

    opt = field.get("/Opt")
    if not opt:
        return []
    out = []
    for item in opt:
        if isinstance(item, (list, ArrayObject)) and len(item) >= 2:
            out.append({"value": str(item[0]), "label": str(item[1])})
        else:
            out.append({"value": str(item), "label": str(item)})
    return out


def _radio_values(field) -> list[str]:
    kids = field.get("/Kids") or []
    values: list[str] = []
    for kid in kids:
        ap = kid.get("/AP")
        if ap and "/N" in ap:
            for k in ap["/N"]:
                if str(k) != "/Off":
                    values.append(str(k))
    return values


# ── inspect ─────────────────────────────────────────────────────────────────


def inspect_fields(*, input_filename: str) -> dict[str, Any]:
    """Walk the AcroForm field tree and return one entry per leaf field.

    Returns:
        ``{"has_fields", "field_count",
            "fields": [{"name", "type", "value"?, "page"?, ...type_specific}, ...]}``
        or ``{"has_fields": False, "field_count": 0, "fields": [], "note": ...}``
        for unfilled PDFs.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(input_path(input_filename)))

    page_map: dict[int, int] = {}
    for i, page in enumerate(reader.pages):
        ref = getattr(page, "indirect_reference", None)
        if ref is not None:
            page_map[ref.idnum] = i + 1  # 1-based page index

    acroform = reader.trailer.get("/Root", {}).get("/AcroForm")
    if acroform is None or "/Fields" not in acroform:
        return {
            "has_fields": False,
            "field_count": 0,
            "fields": [],
            "note": "PDF has no fillable form fields",
        }

    def walk(fields, parent: str = "") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for field in fields:
            name = str(field.get("/T", ""))
            full = f"{parent}.{name}" if parent else name

            # Named-group nodes (have kids that are themselves fields, not widgets)
            kids = field.get("/Kids")
            if kids:
                named = [k for k in kids if "/T" in k]
                if named:
                    out.extend(walk(named, full))
                    continue

            ftype = _field_type(field)
            if ftype == "unknown":
                continue

            entry: dict[str, Any] = {
                "name": full,
                "type": ftype,
                "value": _field_value(field),
            }

            if ftype == "checkbox":
                ap = field.get("/AP")
                if ap and "/N" in ap:
                    states = [str(k) for k in ap["/N"]]
                    entry["states"] = states
                    on = next((s for s in states if s != "/Off"), None)
                    if on:
                        entry["checked_value"] = on
            elif ftype in ("dropdown", "listbox"):
                choices = _dropdown_choices(field)
                if choices:
                    entry["choices"] = choices
            elif ftype == "radio":
                rv = _radio_values(field)
                if rv:
                    entry["radio_values"] = rv

            p_ref = field.get("/P")
            if p_ref is not None and hasattr(p_ref, "idnum"):
                entry["page"] = page_map.get(p_ref.idnum)

            out.append(entry)
        return out

    fields = walk(list(acroform["/Fields"]))
    return {
        "has_fields": bool(fields),
        "field_count": len(fields),
        "fields": fields,
    }


# ── fill ────────────────────────────────────────────────────────────────────


def fill_fields(
    *,
    input_filename: str,
    output_filename: str,
    field_values: dict[str, str],
) -> dict[str, Any]:
    """Fill form fields with values; write a new PDF.

    Args:
        input_filename:  source PDF (must have AcroForm fields)
        output_filename: destination PDF
        field_values:    ``{field_name: value}`` mapping. Names use dot notation
            for nested groups (e.g. ``"Address.City"``). Value semantics:
              text       — any string
              checkbox   — ``"true" / "false" / "1" / "0" / "yes" / "no" / "on" / "off"``
              dropdown   — must match a value from the field's choices
              radio      — must match a radio_value (with leading ``/`` optional)

    Returns:
        ``{"output_filename", "filled_count", "filled_fields": [...],
            "validation_errors"?: [...], "not_found"?: [...]}``
    """
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import BooleanObject, NameObject, TextStringObject

    reader = PdfReader(str(input_path(input_filename)))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)

    acroform = writer._root_object.get("/AcroForm")  # type: ignore[attr-defined]
    if acroform is None or "/Fields" not in acroform:
        raise ValueError("PDF has no fillable form fields; run inspect_fields first to confirm")

    # Tell viewers to regenerate appearance for changed values
    acroform.update({NameObject("/NeedAppearances"): BooleanObject(True)})

    filled: list[str] = []
    errors: list[dict[str, Any]] = []

    def walk_and_fill(fields, parent: str = "") -> None:
        for field in fields:
            name = str(field.get("/T", ""))
            full = f"{parent}.{name}" if parent else name

            kids = field.get("/Kids")
            if kids:
                named = [k for k in kids if "/T" in k]
                if named:
                    walk_and_fill(named, full)
                    continue

            if full not in field_values:
                continue

            value = field_values[full]
            ftype = _field_type(field)

            if ftype == "text":
                field.update({
                    NameObject("/V"): TextStringObject(str(value)),
                    NameObject("/DV"): TextStringObject(str(value)),
                })
                filled.append(full)
            elif ftype == "checkbox":
                truthy = str(value).lower() in ("true", "1", "yes", "on")
                on_val = _checkbox_on_value(field)
                pdf_val = on_val if truthy else "/Off"
                field.update({
                    NameObject("/V"): NameObject(pdf_val),
                    NameObject("/AS"): NameObject(pdf_val),
                })
                filled.append(full)
            elif ftype in ("dropdown", "listbox"):
                allowed = [c["value"] for c in _dropdown_choices(field)]
                if allowed and str(value) not in allowed:
                    errors.append({
                        "field": full,
                        "error": f"Value {value!r} not in allowed choices",
                        "allowed": allowed,
                    })
                    continue
                field.update({NameObject("/V"): TextStringObject(str(value))})
                filled.append(full)
            elif ftype == "radio":
                pdf_val = str(value) if str(value).startswith("/") else f"/{value}"
                field.update({
                    NameObject("/V"): NameObject(pdf_val),
                    NameObject("/AS"): NameObject(pdf_val),
                })
                filled.append(full)
            else:
                errors.append({
                    "field": full,
                    "error": f"Unsupported field type: {ftype}",
                })

    walk_and_fill(list(acroform["/Fields"]))

    not_found = [
        k for k in field_values
        if k not in filled and not any(e["field"] == k for e in errors)
    ]

    out = output_path(output_filename)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as fh:
        writer.write(fh)

    result: dict[str, Any] = {
        "output_filename": output_filename,
        "filled_count": len(filled),
        "filled_fields": filled,
        "size_bytes": out.stat().st_size,
    }
    if errors:
        result["validation_errors"] = errors
    if not_found:
        result["not_found"] = not_found
        result["hint"] = "use inspect_fields to see all available field names"
    return result
