import * as XLSX from 'xlsx';
import { sheetJSToUniverData, XlsxPreviewLimitError } from '../../utils/xlsxPreview';

interface XlsxPreviewWorkerRequest {
  buffer: ArrayBuffer;
  fileName: string;
}

type XlsxPreviewWorkerResponse =
  | { ok: true; workbookData: ReturnType<typeof sheetJSToUniverData> }
  | { ok: false; code: 'cells' | 'range' | 'parse'; limit?: number; message?: string };

self.onmessage = (event: MessageEvent<XlsxPreviewWorkerRequest>) => {
  let response: XlsxPreviewWorkerResponse;
  try {
    const workbook = XLSX.read(event.data.buffer, { type: 'array' });
    response = {
      ok: true,
      workbookData: sheetJSToUniverData(workbook, XLSX, event.data.fileName),
    };
  } catch (error) {
    if (error instanceof XlsxPreviewLimitError) {
      response = { ok: false, code: error.kind, limit: error.limit };
    } else {
      response = {
        ok: false,
        code: 'parse',
        message: error instanceof Error ? error.message : String(error),
      };
    }
  }
  self.postMessage(response);
};
