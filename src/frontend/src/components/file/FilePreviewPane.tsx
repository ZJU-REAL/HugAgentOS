import type { ReactNode } from 'react';
import type { ResourceItem } from '../../types';
import { ArchiveBrowser } from './ArchiveBrowser';
import { isArchiveFile } from '../../utils/archiveBrowser';
import { isArchiveMember } from '../../utils/artifactAccess';
import { ArtifactFileAction } from './ArtifactFileAction';
import { getFileIconSrc } from '../../utils/fileIcon';
import { exceedsPreviewLimit, getPreviewLimitBytes } from '../../utils/filePreviewSafety';
import { extOf, buildRawUrl, buildOfficePreviewUrl, withPdfViewerParams } from './filePreviewUrls';
import {
  ImageViewer, FrameViewer, VideoViewer, AudioViewer, TextViewer, JsonViewer,
  MarkdownViewer, CsvViewer, XlsxViewer, UnsupportedView, LargeFileView, EmptyState,
} from './FilePreviewViewers';

interface FilePreviewPaneProps {
  item: ResourceItem | null;
}

type ViewKind =
  | 'image'
  | 'pdf'
  | 'video'
  | 'audio'
  | 'markdown'
  | 'json'
  | 'csv'
  | 'text'
  | 'pptx'
  | 'docx'
  | 'xlsx'
  | 'archive'
  | 'unknown';

const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico']);
const PDF_EXTS = new Set(['pdf']);
const VIDEO_EXTS = new Set(['mp4', 'webm', 'mov', 'm4v', 'ogv']);
const AUDIO_EXTS = new Set(['mp3', 'wav', 'ogg', 'flac', 'm4a', 'aac']);
const MD_EXTS = new Set(['md', 'markdown', 'mdx']);
const JSON_EXTS = new Set(['json', 'json5', 'jsonl', 'ndjson']);
const CSV_EXTS = new Set(['csv', 'tsv']);
const PPTX_EXTS = new Set(['ppt', 'pptx']);
const DOCX_EXTS = new Set(['doc', 'docx']);
const XLSX_EXTS = new Set(['xls', 'xlsx', 'xlsm']);
const TEXT_EXTS = new Set([
  'txt', 'log', 'py', 'js', 'ts', 'tsx', 'jsx', 'mjs', 'cjs',
  'html', 'htm', 'css', 'scss', 'sass', 'less',
  'xml', 'yaml', 'yml', 'ini', 'conf', 'env', 'toml', 'lock',
  'sh', 'bash', 'zsh', 'fish', 'ps1',
  'go', 'rs', 'java', 'kt', 'swift', 'c', 'cpp', 'cc', 'h', 'hpp',
  'rb', 'php', 'r', 'sql', 'graphql', 'gql', 'proto',
  'vue', 'svelte', 'astro',
]);

function detectKind(item: ResourceItem): ViewKind {
  if (isArchiveFile(item)) return isArchiveMember(item) ? 'unknown' : 'archive';
  if (isArchiveMember(item) && item.mime_type === 'text/plain') return 'text';
  const mime = (item.mime_type || '').toLowerCase();
  const ext = extOf(item.name || '');
  if (mime.startsWith('image/')) return 'image';
  if (mime === 'application/pdf') return 'pdf';
  if (mime.startsWith('video/')) return 'video';
  if (mime.startsWith('audio/')) return 'audio';
  if (mime === 'text/markdown') return 'markdown';
  if (mime === 'application/json') return 'json';
  if (mime === 'text/csv') return 'csv';
  if (IMAGE_EXTS.has(ext)) return 'image';
  if (PDF_EXTS.has(ext)) return 'pdf';
  if (VIDEO_EXTS.has(ext)) return 'video';
  if (AUDIO_EXTS.has(ext)) return 'audio';
  if (MD_EXTS.has(ext)) return 'markdown';
  if (JSON_EXTS.has(ext)) return 'json';
  if (CSV_EXTS.has(ext)) return 'csv';
  if (PPTX_EXTS.has(ext)) return 'pptx';
  if (DOCX_EXTS.has(ext)) return 'docx';
  if (XLSX_EXTS.has(ext)) return 'xlsx';
  if (TEXT_EXTS.has(ext)) return 'text';
  if (mime.startsWith('text/')) return 'text';
  return 'unknown';
}

// ─── main entry ────────────────────────────────────────────────────

export function FilePreviewPane({ item }: FilePreviewPaneProps) {
  if (!item) return <EmptyState />;
  const kind = detectKind(item);
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
      body = <VideoViewer src={rawUrl} />;
      break;
    case 'audio':
      body = <AudioViewer src={rawUrl} name={item.name} />;
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
    case 'text':
      body = <TextViewer item={item} maxBytes={maxPreviewBytes} />;
      break;
    case 'pptx':
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
