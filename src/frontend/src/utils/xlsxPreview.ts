import type { CellObject, WorkBook, WorkSheet } from 'xlsx';

type XlsxModule = typeof import('xlsx');

export const MAX_XLSX_PREVIEW_CELLS = 100_000;
export const MAX_XLSX_PREVIEW_ROWS = 100_000;
export const MAX_XLSX_PREVIEW_COLUMNS = 512;
const MAX_XLSX_PREVIEW_MERGES = 10_000;

export type XlsxPreviewLimitKind = 'cells' | 'range';

export class XlsxPreviewLimitError extends Error {
  readonly kind: XlsxPreviewLimitKind;
  readonly limit: number;

  constructor(kind: XlsxPreviewLimitKind, limit: number) {
    super(`Spreadsheet preview ${kind} limit exceeded: ${limit}`);
    this.name = 'XlsxPreviewLimitError';
    this.kind = kind;
    this.limit = limit;
  }
}

function asCell(value: unknown): CellObject | null {
  return value && typeof value === 'object' ? value as CellObject : null;
}

function buildSheetData(
  ws: WorkSheet,
  sheetId: string,
  name: string,
  XLSX: XlsxModule,
  cellCounter: { value: number },
) {
  const cellData: Record<number, Record<number, Record<string, unknown>>> = {};
  let maxRow = -1;
  let maxColumn = -1;

  // Iterate sparse, real cells only. Walking every coordinate inside !ref is
  // catastrophic for sheets with a large or stale declared dimension.
  for (const address in ws) {
    if (!Object.prototype.hasOwnProperty.call(ws, address) || address[0] === '!') continue;

    let position;
    try {
      position = XLSX.utils.decode_cell(address);
    } catch {
      continue;
    }

    if (position.r >= MAX_XLSX_PREVIEW_ROWS || position.c >= MAX_XLSX_PREVIEW_COLUMNS) {
      throw new XlsxPreviewLimitError('range', MAX_XLSX_PREVIEW_ROWS);
    }

    const cell = asCell(ws[address]);
    if (!cell) continue;

    cellCounter.value += 1;
    if (cellCounter.value > MAX_XLSX_PREVIEW_CELLS) {
      throw new XlsxPreviewLimitError('cells', MAX_XLSX_PREVIEW_CELLS);
    }

    maxRow = Math.max(maxRow, position.r);
    maxColumn = Math.max(maxColumn, position.c);
    if (!cellData[position.r]) cellData[position.r] = {};

    const cellObject: Record<string, unknown> = {};
    if (cell.f) {
      cellObject.f = `=${cell.f}`;
      if (cell.v !== undefined) cellObject.v = cell.v;
    } else if (cell.t === 'n') {
      cellObject.v = cell.v;
      cellObject.t = 2;
    } else if (cell.t === 'b') {
      cellObject.v = cell.v ? 1 : 0;
      cellObject.t = 1;
    } else {
      cellObject.v = cell.v != null ? String(cell.v) : '';
      cellObject.t = 1;
    }
    cellData[position.r][position.c] = cellObject;
  }

  const columnData: Record<number, { w: number }> = {};
  const columns = ws['!cols'];
  if (columns) {
    for (const key in columns) {
      const index = Number(key);
      const column = columns[index];
      if (Number.isInteger(index) && index < MAX_XLSX_PREVIEW_COLUMNS && column?.wpx) {
        columnData[index] = { w: column.wpx };
      }
    }
  }

  const rowData: Record<number, { h: number }> = {};
  const rows = ws['!rows'];
  if (rows) {
    for (const key in rows) {
      const index = Number(key);
      const row = rows[index];
      if (Number.isInteger(index) && index < MAX_XLSX_PREVIEW_ROWS && row?.hpx) {
        rowData[index] = { h: row.hpx };
      }
    }
  }

  const mergeData = (ws['!merges'] || [])
    .slice(0, MAX_XLSX_PREVIEW_MERGES)
    .filter((merge) => (
      merge.e.r < MAX_XLSX_PREVIEW_ROWS
      && merge.e.c < MAX_XLSX_PREVIEW_COLUMNS
    ))
    .map((merge) => ({
      startRow: merge.s.r,
      startColumn: merge.s.c,
      endRow: merge.e.r,
      endColumn: merge.e.c,
    }));

  return {
    id: sheetId,
    name,
    cellData,
    rowCount: Math.max(maxRow + 1, 100),
    columnCount: Math.max(maxColumn + 1, 26),
    defaultColumnWidth: 88,
    defaultRowHeight: 24,
    columnData,
    rowData,
    mergeData,
  };
}

/** Convert a SheetJS workbook into Univer's sparse IWorkbookData shape. */
export function sheetJSToUniverData(wb: WorkBook, XLSX: XlsxModule, fileName: string) {
  const sheetOrder: string[] = [];
  const sheets: Record<string, ReturnType<typeof buildSheetData>> = {};
  const cellCounter = { value: 0 };

  wb.SheetNames.forEach((name, index) => {
    const worksheet = wb.Sheets[name];
    if (!worksheet) return;
    const sheetId = `sheet-${index}`;
    sheetOrder.push(sheetId);
    sheets[sheetId] = buildSheetData(worksheet, sheetId, name, XLSX, cellCounter);
  });

  return {
    id: 'workbook-1',
    name: fileName,
    appVersion: '1.0.0',
    locale: 'zhCN',
    sheetOrder,
    sheets,
    styles: {},
  };
}
