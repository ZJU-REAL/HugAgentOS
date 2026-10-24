import type * as PdfJs from 'pdfjs-dist';

export type PdfJsModule = typeof PdfJs;
export type PdfDocument = PdfJs.PDFDocumentProxy;
export type PdfPage = PdfJs.PDFPageProxy;

// pdf.js resolves CMaps (CJK text), standard fonts and image-codec wasm by
// "baseUrl + filename". Vite hashes each asset, so map filenames to emitted
// URLs instead; the files are fetched only when a document needs them.
const BINARY_DATA = {
  cMapUrl: import.meta.glob('../../../../node_modules/pdfjs-dist/cmaps/*.bcmap', { query: '?url', import: 'default' }),
  standardFontDataUrl: import.meta.glob('../../../../node_modules/pdfjs-dist/standard_fonts/*.{pfb,ttf}', { query: '?url', import: 'default' }),
  // Image codecs only: ICC wasm is disabled with useWorkerFetch:false, and the
  // *_nowasm_fallback.js / quickjs files can never be loaded from here.
  wasmUrl: import.meta.glob(['../../../../node_modules/pdfjs-dist/wasm/jbig2.wasm', '../../../../node_modules/pdfjs-dist/wasm/openjpeg.wasm'], { query: '?url', import: 'default' }),
} as const;
type BinaryKind = keyof typeof BINARY_DATA;

class BundledBinaryDataFactory {
  async fetch({ kind, filename }: { kind: BinaryKind; filename: string }): Promise<Uint8Array> {
    const entry = Object.entries(BINARY_DATA[kind]).find(([path]) => path.endsWith(`/${filename}`));
    if (!entry) throw new Error(`pdf.js asset not bundled: ${filename}`);
    const url = await entry[1]() as string;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP ${response.status}: ${url}`);
    return new Uint8Array(await response.arrayBuffer());
  }
}

let loading: Promise<{ lib: PdfJsModule; worker: PdfJs.PDFWorker }> | undefined;

/**
 * One worker shared by every open document. It is owned here, not by any
 * loading task: a worker set through GlobalWorkerOptions is torn down by the
 * first `task.destroy()`, breaking documents opened while that is pending.
 */
function loadPdfJs() {
  loading ??= Promise.all([
    import('pdfjs-dist'),
    // Bundled by Vite as a .js worker (an emitted .mjs would need a JS MIME type
    // from every static server).
    import('pdfjs-dist/build/pdf.worker.min.mjs?worker'),
  ]).then(([lib, worker]) => ({ lib, worker: lib.PDFWorker.create({ port: new worker.default() }) }));
  loading.catch(() => { loading = undefined; });
  return loading;
}

export async function openPdfDocument(data: Uint8Array): Promise<{ lib: PdfJsModule; doc: PdfDocument; destroy: () => void }> {
  const { lib, worker } = await loadPdfJs();
  const task = lib.getDocument({
    worker,
    data: data.slice(), // the worker takes ownership of (detaches) the buffer it is given
    enableXfa: false,
    useWorkerFetch: false,
    // Placeholder bases: the factory above ignores them, pdf.js only checks presence.
    cMapUrl: 'bundled:/',
    standardFontDataUrl: 'bundled:/',
    wasmUrl: 'bundled:/',
    BinaryDataFactory: BundledBinaryDataFactory,
  });
  const destroy = () => { void task.destroy(); };
  try {
    return { lib, doc: await task.promise, destroy };
  } catch (error) {
    destroy();
    throw error;
  }
}
