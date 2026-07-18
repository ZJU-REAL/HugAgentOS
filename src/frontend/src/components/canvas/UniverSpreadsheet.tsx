import { useEffect, useRef, useImperativeHandle, forwardRef, useState, useCallback } from 'react';
import { recomputeSheetRefs } from '../../utils/xlsxRange';
import { t } from '../../i18n';

export interface UniverSpreadsheetHandle {
  exportXlsx: () => Promise<File>;
  resetDirty: () => void;
}

interface UniverSpreadsheetProps {
  url: string;
  onDirty?: (dirty: boolean) => void;
}

/**
 * Converts SheetJS workbook to Univer IWorkbookData format.
 */
function sheetJSToUniverData(wb: any, XLSX: any, fileName: string): any {
  const sheetOrder: string[] = [];
  const sheets: Record<string, any> = {};

  wb.SheetNames.forEach((name: string, idx: number) => {
    const sheetId = `sheet-${idx}`;
    sheetOrder.push(sheetId);
    const ws = wb.Sheets[name];
    const range = XLSX.utils.decode_range(ws['!ref'] || 'A1');
    const rowCount = Math.max(range.e.r + 1, 20);
    const columnCount = Math.max(range.e.c + 1, 10);

    const cellData: Record<number, Record<number, any>> = {};
    for (let r = range.s.r; r <= range.e.r; r++) {
      for (let c = range.s.c; c <= range.e.c; c++) {
        const addr = XLSX.utils.encode_cell({ r, c });
        const cell = ws[addr];
        if (!cell) continue;
        if (!cellData[r]) cellData[r] = {};

        const cellObj: any = {};
        if (cell.t === 'n') {
          cellObj.v = cell.v;
          cellObj.t = 2; // NUMBER
        } else if (cell.t === 'b') {
          cellObj.v = cell.v ? 1 : 0;
          cellObj.t = 1; // BOOLEAN
        } else if (cell.f) {
          cellObj.f = '=' + cell.f;
          if (cell.v !== undefined) cellObj.v = cell.v;
        } else {
          cellObj.v = cell.v != null ? String(cell.v) : '';
          cellObj.t = 1; // STRING
        }
        cellData[r][c] = cellObj;
      }
    }

    const columnData: Record<number, any> = {};
    if (ws['!cols']) {
      ws['!cols'].forEach((col: any, i: number) => {
        if (col && col.wpx) columnData[i] = { w: col.wpx };
      });
    }
    const rowData: Record<number, any> = {};
    if (ws['!rows']) {
      ws['!rows'].forEach((row: any, i: number) => {
        if (row && row.hpx) rowData[i] = { h: row.hpx };
      });
    }
    const mergeData: any[] = [];
    if (ws['!merges']) {
      ws['!merges'].forEach((m: any) => {
        mergeData.push({ startRow: m.s.r, startColumn: m.s.c, endRow: m.e.r, endColumn: m.e.c });
      });
    }

    sheets[sheetId] = {
      id: sheetId, name, cellData,
      rowCount: Math.max(rowCount, 100),
      columnCount: Math.max(columnCount, 26),
      defaultColumnWidth: 88, defaultRowHeight: 24,
      columnData, rowData, mergeData,
    };
  });

  return {
    id: 'workbook-1', name: fileName, appVersion: '1.0.0',
    locale: 'zhCN', sheetOrder, sheets, styles: {},
  };
}

export const UniverSpreadsheet = forwardRef<UniverSpreadsheetHandle, UniverSpreadsheetProps>(
  function UniverSpreadsheet({ url, onDirty }, ref) {
    const containerRef = useRef<HTMLDivElement>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const univerAPIRef = useRef<any>(null);
    const dirtyRef = useRef(false);
    const initDoneRef = useRef(false);
    const fileNameRef = useRef('file.xlsx');

    const resetDirty = useCallback(() => {
      dirtyRef.current = false;
    }, []);

    useImperativeHandle(ref, () => ({
      resetDirty,
      async exportXlsx(): Promise<File> {
        const api = univerAPIRef.current;
        if (!api) throw new Error('Univer not initialised');

        const wb = api.getActiveWorkbook();
        if (!wb) throw new Error('No active workbook');

        const XLSX = await import('xlsx');
        const newWb = XLSX.utils.book_new();
        let exported = false;

        // Strategy 1: Facade API — getSheets + getDataRange().getValues()
        try {
          const sheets = wb.getSheets();
          if (sheets && sheets.length > 0) {
            for (const fSheet of sheets) {
              const name = fSheet.getSheetName();
              try {
                const values = fSheet.getDataRange().getValues();
                XLSX.utils.book_append_sheet(newWb, XLSX.utils.aoa_to_sheet(values || [[]]), name);
              } catch {
                XLSX.utils.book_append_sheet(newWb, XLSX.utils.aoa_to_sheet([[]]), name);
              }
            }
            exported = true;
          }
        } catch { /* fall through */ }

        // Strategy 2: Snapshot cellData — works even if Facade sheets aren't ready
        if (!exported) {
          const snapshot = wb.getSnapshot();
          if (!snapshot?.sheetOrder?.length) throw new Error('No workbook data');

          for (const sheetId of snapshot.sheetOrder) {
            const sd = snapshot.sheets?.[sheetId];
            if (!sd) continue;
            const cellData = sd.cellData || {};
            const rowKeys = Object.keys(cellData).map(Number).sort((a, b) => a - b);
            if (rowKeys.length === 0) {
              XLSX.utils.book_append_sheet(newWb, XLSX.utils.aoa_to_sheet([[]]), sd.name || sheetId);
              continue;
            }
            const maxRow = Math.max(...rowKeys);
            const aoa: any[][] = [];
            for (let r = 0; r <= maxRow; r++) {
              const rowCells = cellData[r] || {};
              const colKeys = Object.keys(rowCells).map(Number);
              const maxCol = colKeys.length > 0 ? Math.max(...colKeys) : 0;
              const row: any[] = [];
              for (let c = 0; c <= maxCol; c++) {
                row.push(rowCells[c]?.v ?? '');
              }
              aoa.push(row);
            }
            XLSX.utils.book_append_sheet(newWb, XLSX.utils.aoa_to_sheet(aoa), sd.name || sheetId);
          }
        }

        const xlsxBuf = XLSX.write(newWb, { bookType: 'xlsx', type: 'array' });
        return new File([xlsxBuf], fileNameRef.current, {
          type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        });
      },
    }));

    useEffect(() => {
      let disposed = false;
      let univerInstance: any = null;

      (async () => {
        try {
          setLoading(true);
          setError(null);

          // 1. Fetch xlsx
          const resp = await fetch(url);
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          const buf = await resp.arrayBuffer();
          const fileName = decodeURIComponent(url.split('/').pop() || 'file.xlsx');
          fileNameRef.current = fileName.endsWith('.xlsx') ? fileName : fileName + '.xlsx';

          if (disposed) return;

          // 2. Parse with SheetJS
          const XLSX = await import('xlsx');
          const wb = XLSX.read(buf, { type: 'array' });
          // Stale <dimension> in the file would make SheetJS report a
          // too-small !ref and drop rows; rebuild it from the real cells.
          recomputeSheetRefs(wb, XLSX);
          const workbookData = sheetJSToUniverData(wb, XLSX, fileNameRef.current);

          if (disposed || !containerRef.current) return;

          // 3. Import Univer + CSS
          const [
            { createUniver },
            { UniverSheetsCorePreset },
            sheetsCoreZhCN,
          ] = await Promise.all([
            import('@univerjs/presets'),
            import('@univerjs/preset-sheets-core'),
            import('@univerjs/preset-sheets-core/locales/zh-CN'),
            import('@univerjs/design/lib/index.css'),
            import('@univerjs/ui/lib/index.css'),
            import('@univerjs/docs-ui/lib/index.css'),
            import('@univerjs/sheets-ui/lib/index.css'),
            import('@univerjs/sheets-formula-ui/lib/index.css'),
            import('@univerjs/sheets-numfmt-ui/lib/index.css'),
          ]);

          if (disposed || !containerRef.current) return;

          // 4. Create Univer
          const { univer, univerAPI } = createUniver({
            locale: 'zhCN' as any,
            locales: { zhCN: sheetsCoreZhCN.default } as any,
            presets: [
              UniverSheetsCorePreset({ container: containerRef.current, header: true }),
            ],
          });

          if (disposed) { univer.dispose(); return; }

          univerInstance = univer;
          univerAPIRef.current = univerAPI;

          // 5. Load workbook
          univerAPI.createWorkbook(workbookData);

          // 6. Dirty detection — delayed to skip init commands
          setTimeout(() => {
            if (disposed) return;
            initDoneRef.current = true;

            const EDIT_PATTERNS = [
              'set-range-values', 'set-range-formatted',
              'set-style', 'insert-row', 'insert-col',
              'remove-row', 'remove-col', 'delete-range', 'insert-range',
              'set-worksheet-name', 'insert-sheet', 'remove-sheet',
              'move-range', 'set-col-width', 'set-row-height',
              'add-worksheet-merge', 'remove-worksheet-merge',
              'paste', 'undo', 'redo',
            ];

            univerAPI.onCommandExecuted?.((cmd: any) => {
              if (!initDoneRef.current || dirtyRef.current) return;
              const id: string = (cmd?.id || '').toLowerCase();
              if (EDIT_PATTERNS.some(p => id.includes(p))) {
                dirtyRef.current = true;
                onDirty?.(true);
              }
            });
          }, 2000);
        } catch (e: any) {
          console.error('[UniverSpreadsheet]', e);
          if (!disposed) setError(e.message || t('电子表格加载失败'));
        } finally {
          if (!disposed) setLoading(false);
        }
      })();

      return () => {
        disposed = true;
        try { univerInstance?.dispose(); } catch { /* */ }
        univerAPIRef.current = null;
        dirtyRef.current = false;
        initDoneRef.current = false;
      };
    }, [url]); // eslint-disable-line react-hooks/exhaustive-deps

    if (error) return <div className="jx-canvas-error">{error}</div>;

    return (
      <div className="jx-canvas-univer">
        {/* 加载蒙层 0.2s 淡出后留挂（opacity:0 + pointer-events:none，无副作用），
            表格以柔和 cross-fade 呈现；禁止对 Univer canvas 本身做 transform。 */}
        <div
          className={`jx-canvas-loading jx-canvas-univerOverlay${loading ? '' : ' jx-canvas-univerOverlay--done'}`}
          style={{ position: 'absolute', inset: 0, zIndex: 10, background: '#fff' }}
        >
          <div className="jx-canvas-spinner" />
          <span>{t('正在加载电子表格...')}</span>
        </div>
        <div ref={containerRef} style={{ position: 'absolute', inset: 0 }} />
      </div>
    );
  },
);
