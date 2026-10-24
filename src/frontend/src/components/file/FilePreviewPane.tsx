import type { ReactNode } from 'react';
import type { ResourceItem } from '../../types';
import { ArchiveBrowser } from './ArchiveBrowser';
import { ArtifactFileAction } from './ArtifactFileAction';
import { getFileIconSrc } from '../../utils/fileIcon';
import { exceedsPreviewLimit, getPreviewLimitBytes } from '../../utils/filePreviewSafety';
import { getFilePreviewKind } from '../../utils/filePreviewKind';
import { buildRawUrl, buildOfficePreviewUrl, withPdfViewerParams } from './filePreviewUrls';
import {
  ImageViewer, FrameViewer, TextViewer, JsonViewer,
  MarkdownViewer, CsvViewer, XlsxViewer, UnsupportedView, LargeFileView, EmptyState,
} from './FilePreviewViewers';
import { MediaPlayer } from './MediaPlayer';

interface FilePreviewPaneProps {
  item: ResourceItem | null;
}

// ─── main entry ────────────────────────────────────────────────────

export function FilePreviewPane({ item }: FilePreviewPaneProps) {
  if (!item) return <EmptyState />;
  const kind = getFilePreviewKind(item);
  const rawUrl = buildRawUrl(item, true);
  const dlUrl = buildRawUrl(item);
  const previewLimitBytes = getPreviewLimitBytes(kind);
  const previewTooLarge = exceedsPreviewLimit(item.size, previewLimitBytes);
  const maxPreviewBytes = previewLimitBytes ?? Number.MAX_SAFE_INTEGER;

  let body: ReactNode;
  if (previewTooLarge && previewLimitBytes !== null) {
    body = <LargeFileView item={item} limitBytes={previewLimitBytes} />;
  } else switch (kind) {
    case 'archive':
      body = <ArchiveBrowser key={item.id} file={item} renderMember={member => <FilePreviewPane item={member} />} />;
      break;
    case 'image':
      body = <ImageViewer src={rawUrl} name={item.name} />;
      break;
    case 'pdf':
      body = <FrameViewer src={withPdfViewerParams(rawUrl)} />;
      break;
    case 'video':
    case 'audio':
      body = <MediaPlayer kind={kind} src={rawUrl} name={item.name} />;
      break;
    case 'markdown':
      body = <MarkdownViewer item={item} maxBytes={maxPreviewBytes} />;
      break;
    case 'json':
      body = <JsonViewer item={item} maxBytes={maxPreviewBytes} />;
      break;
    case 'csv':
      body = <CsvViewer item={item} maxBytes={maxPreviewBytes} />;
      break;
    // HTML is shown as source here: this pane never executes previewed markup.
    case 'html':
    case 'text':
      body = <TextViewer item={item} maxBytes={maxPreviewBytes} />;
      break;
    case 'office':
    case 'docx':
      body = <FrameViewer src={withPdfViewerParams(buildOfficePreviewUrl(item))} />;
      break;
    case 'xlsx':
      body = <XlsxViewer item={item} />;
      break;
    default:
      body = <UnsupportedView item={item} />;
  }

  return (
    <div className="jx-spaceImportPreview">
      <div className="jx-spaceImportPreview-header">
        <img
          className="jx-spaceImportPreview-headerIcon"
          src={getFileIconSrc(item.name)}
          width={20}
          height={20}
          alt=""
        />
        <div className="jx-spaceImportPreview-headerName" title={item.name}>
          {item.name}
        </div>
        {dlUrl && (
          <ArtifactFileAction file={item} className="jx-spaceImportPreview-headerAction" />
        )}
      </div>
      <div className="jx-spaceImportPreview-body">
        {/* Preview switch keyed fade-in: CSS primitive, pure opacity (content may contain an iframe, transform forbidden) */}
        <div
          key={item.id}
          className="jx-filePreview-fade jx-anim-fadeIn"
          style={{ animationDuration: '0.18s' }}
        >
          {body}
        </div>
      </div>
    </div>
  );
}
