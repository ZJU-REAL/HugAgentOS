import type { WorkBook } from 'xlsx';

type XlsxModule = typeof import('xlsx');

/**
 * Recompute every sheet's `!ref` from the actual cells SheetJS parsed.
 *
 * SheetJS reads `!ref` straight from the worksheet's `<dimension>` tag and
 * trusts it verbatim — it only scans real cells when `<dimension>` is absent.
 * Some editors (our excel-editing skill before the dimension fix, and any
 * tool that appends rows without updating `<dimension>`) leave a stale,
 * too-small `<dimension>`. The file is fine — Excel re-derives the used range
 * on open — but our SheetJS-based previews (`sheet_to_html`, the Univer
 * importer) would silently drop every row/column outside the stale range.
 *
 * Scanning the parsed cells and unioning with the declared range makes the
 * preview render exactly what the file contains. Union-only: a deliberately
 * wider `<dimension>` is never shrunk.
 */
export function recomputeSheetRefs(wb: WorkBook, XLSX: XlsxModule): void {
  for (const name of wb.SheetNames || []) {
    const ws = wb.Sheets?.[name];
    if (!ws) continue;

    let minR = Infinity;
    let minC = Infinity;
    let maxR = -1;
    let maxC = -1;
    for (const key of Object.keys(ws)) {
      if (key[0] === '!') continue;
      const cell = XLSX.utils.decode_cell(key);
      if (cell.r < minR) minR = cell.r;
      if (cell.c < minC) minC = cell.c;
      if (cell.r > maxR) maxR = cell.r;
      if (cell.c > maxC) maxC = cell.c;
    }
    if (maxR < 0) continue; // no cells — leave whatever !ref it had

    const range = { s: { r: minR, c: minC }, e: { r: maxR, c: maxC } };
    if (ws['!ref']) {
      const declared = XLSX.utils.decode_range(ws['!ref']);
      range.s.r = Math.min(range.s.r, declared.s.r);
      range.s.c = Math.min(range.s.c, declared.s.c);
      range.e.r = Math.max(range.e.r, declared.e.r);
      range.e.c = Math.max(range.e.c, declared.e.c);
    }
    ws['!ref'] = XLSX.utils.encode_range(range);
  }
}
