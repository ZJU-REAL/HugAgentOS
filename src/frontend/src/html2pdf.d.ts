declare module 'html2pdf.js' {
  interface Html2PdfOptions {
    margin?: number | number[];
    filename?: string;
    image?: { type?: string; quality?: number };
    html2canvas?: Record<string, unknown>;
    jsPDF?: Record<string, unknown>;
  }
  interface Html2PdfInstance {
    set(options: Html2PdfOptions): Html2PdfInstance;
    from(element: HTMLElement | string): Html2PdfInstance;
    toPdf(): Html2PdfInstance;
    get(key: string): Promise<unknown>;
    save(): Promise<void>;
  }
  function html2pdf(): Html2PdfInstance;
  export default html2pdf;
}
