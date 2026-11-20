"""PDF read / edit / generate operations.

Read uses ``pdfplumber`` (better text extraction than pypdf for many fonts);
write/manipulate uses ``pypdf``. Print-quality generation from a spec lives in
``engine.creator`` (ported from the former minimax-pdf skill: palette →
cover.html → Playwright → reportlab body → merge). ``creator`` imports ``_body`` /
``_cover_html`` / ``_merge`` / ``_palette`` / ``_reformat`` (pulling in
reportlab/matplotlib), so it is intentionally NOT imported here.
"""

from . import form, merger, reader, splitter  # noqa: F401

PDF_MIME = "application/pdf"
