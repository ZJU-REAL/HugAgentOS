import { useEffect, useRef, useImperativeHandle, forwardRef, useState, useCallback } from 'react';
import { t } from '../../i18n';
import { authFetch } from '../../api';
import { readLimitedArrayBuffer } from '../../utils/filePreviewSafety';

type UniverCreationResult = ReturnType<(typeof import('@univerjs/presets'))['createUniver']>;

export interface UniverSpreadsheetHandle {
  exportXlsx: () => Promise<File>;
  resetDirty: () => void;
}

interface UniverSpreadsheetProps {
  url: string;
  fileName: string;
  maxBytes: number;
  onDirty?: (dirty: boolean) => void;
}

export const UniverSpreadsheet = forwardRef<UniverSpreadsheetHandle, UniverSpreadsheetProps>(
  function UniverSpreadsheet({ url, fileName, maxBytes, onDirty }, ref) {
    const containerRef = useRef<HTMLDivElement>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const univerAPIRef = useRef<UniverCreationResult['univerAPI'] | null>(null);
    const dirtyRef = useRef(false);
    const initDoneRef = useRef(false);
    const fileNameRef = useRef(fileName);

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
            const aoa: unknown[][] = [];
            for (let r = 0; r <= maxRow; r++) {
              const rowCells = cellData[r] || {};
              const colKeys = Object.keys(rowCells).map(Number);
              const maxCol = colKeys.length > 0 ? Math.max(...colKeys) : 0;
              const row: unknown[] = [];
              for (let c = 0; c <= maxCol; c++) {
                row.push(rowCells[c]?.v ?? '');
              }
              aoa.push(row);
            }
            XLSX.utils.book_append_sheet(
              newWb,
              XLSX.utils.aoa_to_sheet(aoa as Parameters<typeof XLSX.utils.aoa_to_sheet>[0]),
              sd.name || sheetId,
            );
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
      let univerInstance: UniverCreationResult['univer'] | null = null;
      let parseWorker: Worker | null = null;
      const controller = new AbortController();

      (async () => {
        try {
          setLoading(true);
          setError(null);

          // 1. Fetch xlsx
          const resp = await authFetch(url, { signal: controller.signal });
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          const buf = await readLimitedArrayBuffer(resp, maxBytes);
          fileNameRef.current = fileName;

          if (disposed) return;

          // 2. Parse and convert off the main thread. Large or malformed
          // worksheets must never block React input/painting.
          parseWorker = new Worker(new URL('./xlsxPreview.worker.ts', import.meta.url), { type: 'module' });
          let workbookData: unknown;
          try {
            workbookData = await new Promise<unknown>((resolve, reject) => {
              if (!parseWorker) {
                reject(new Error(t('电子表格加载失败')));
                return;
              }
              parseWorker.onmessage = (event: MessageEvent<{
                ok: boolean;
                workbookData?: unknown;
                code?: 'cells' | 'range' | 'parse';
                limit?: number;
                message?: string;
              }>) => {
                const result = event.data;
                if (result.ok) {
                  resolve(result.workbookData);
                } else if (result.code === 'cells') {
                  reject(new Error(t(
                    '电子表格内容过多，在线预览最多支持 {n} 个有效单元格，请下载后在本地打开',
                    { n: (result.limit || 0).toLocaleString() },
                  )));
                } else if (result.code === 'range') {
                  reject(new Error(t('电子表格有效范围过大，无法安全在线预览，请下载后在本地打开')));
                } else {
                  reject(new Error(result.message || t('电子表格加载失败')));
                }
              };
              parseWorker.onerror = () => reject(new Error(t('电子表格加载失败')));
              parseWorker.postMessage({ buffer: buf, fileName: fileNameRef.current }, [buf]);
            });
          } finally {
            parseWorker?.terminate();
            parseWorker = null;
          }

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
          type CreateUniverOptions = Parameters<typeof createUniver>[0];
          const { univer, univerAPI } = createUniver({
            locale: 'zhCN' as CreateUniverOptions['locale'],
            locales: { zhCN: sheetsCoreZhCN.default } as CreateUniverOptions['locales'],
            presets: [
              UniverSheetsCorePreset({ container: containerRef.current, header: true }),
            ],
          });

          if (disposed) { univer.dispose(); return; }

          univerInstance = univer;
          univerAPIRef.current = univerAPI;

          // 5. Load workbook
          univerAPI.createWorkbook(
            workbookData as Parameters<typeof univerAPI.createWorkbook>[0],
          );

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

            univerAPI.onCommandExecuted?.((cmd) => {
              if (!initDoneRef.current || dirtyRef.current) return;
              const id: string = (cmd?.id || '').toLowerCase();
              if (EDIT_PATTERNS.some(p => id.includes(p))) {
                dirtyRef.current = true;
                onDirty?.(true);
              }
            });
          }, 2000);
        } catch (error: unknown) {
          console.error('[UniverSpreadsheet]', error);
          if (!disposed) {
            setError(error instanceof Error ? error.message : t('电子表格加载失败'));
          }
        } finally {
          if (!disposed) setLoading(false);
        }
      })();

      return () => {
        disposed = true;
        controller.abort();
        parseWorker?.terminate();
        try { univerInstance?.dispose(); } catch { /* */ }
        univerAPIRef.current = null;
        dirtyRef.current = false;
        initDoneRef.current = false;
      };
    }, [fileName, maxBytes, url]); // eslint-disable-line react-hooks/exhaustive-deps

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
