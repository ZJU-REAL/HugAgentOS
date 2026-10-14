/** Both fetched HTML and draft previews retain base URLs inside opaque iframes. */
export function htmlPreviewDocument(content: string, sourceUrl: string): string {
  const document = new DOMParser().parseFromString(content, 'text/html');
  const existingBase = document.head.querySelector('base[href]');
  const base = existingBase ?? document.createElement('base');
  base.setAttribute('href', new URL(existingBase?.getAttribute('href') || sourceUrl, sourceUrl).href);
  if (!existingBase) document.head.prepend(base);
  const doctype = document.doctype ? new XMLSerializer().serializeToString(document.doctype) : '';
  return doctype + document.documentElement.outerHTML;
}
