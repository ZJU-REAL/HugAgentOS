import type { ResourceItem } from '../../types';
import { artifactUrl } from '../../utils/artifactAccess';

export function buildRawUrl(item: ResourceItem, inline = false): string {
  return artifactUrl(item, { params: inline ? { inline: 'true' } : {} });
}

export function buildOfficePreviewUrl(item: ResourceItem): string {
  return artifactUrl(item, { preview: true });
}

// Hide the browser's built-in PDF viewer chrome (toolbar / sidebar / scrollbar).
// Adobe PDF "Open Parameters" — supported by Chrome, Edge, Firefox.
const PDF_VIEWER_PARAMS = '#toolbar=0&navpanes=0&scrollbar=0&view=FitH';

export function withPdfViewerParams(url: string): string {
  if (!url) return url;
  const [base, fragment = ''] = url.split('#', 2);
  const params = new URLSearchParams(fragment);
  for (const [key, value] of new URLSearchParams(PDF_VIEWER_PARAMS.slice(1))) params.set(key, value);
  return `${base}#${params}`;
}
