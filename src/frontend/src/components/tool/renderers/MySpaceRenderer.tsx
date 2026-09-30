import { useMemo } from 'react';
import {
  FileTextOutlined, PictureOutlined, FileOutlined,
} from '@ant-design/icons';
import type { ToolCall } from '../../../types';
import { t } from '../../../i18n';

function formatBytes(b: number): string {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / 1024 / 1024).toFixed(1)} MB`;
}

function FileIcon({ mimeType }: { mimeType?: string }) {
  if (!mimeType) return <FileOutlined />;
  if (mimeType.startsWith('image/')) return <PictureOutlined style={{ color: '#6c8ebf' }} />;
  if (mimeType.includes('pdf')) return <FileTextOutlined style={{ color: '#e05c5c' }} />;
  if (mimeType.includes('word') || mimeType.includes('document'))
    return <FileTextOutlined style={{ color: 'var(--color-primary)' }} />;
  if (mimeType.includes('sheet') || mimeType.includes('excel') || mimeType.includes('csv'))
    return <FileTextOutlined style={{ color: 'var(--color-success)' }} />;
  return <FileTextOutlined style={{ color: '#888' }} />;
}

/** Parse tool output JSON, unwrapping a `{result: "..."}` envelope if present. */
function parseOutput(output: unknown): unknown {
  if (!output) return null;
  if (typeof output === 'object') return output;
  if (typeof output === 'string') {
    try {
      const parsed = JSON.parse(output);
      if (parsed && typeof parsed === 'object' && 'result' in parsed) {
        const inner = (parsed as { result: unknown }).result;
        if (typeof inner === 'string') {
          try { return JSON.parse(inner); } catch { return inner; }
        }
        return inner;
      }
      return parsed;
    } catch { return output; }
  }
  return output;
}

function FilesBody({ data }: { data: { total: number; items: Array<{
  artifact_id: string; name: string; mime_type: string;
  size_bytes: number; source: string; chat_title?: string;
}> } }) {
  if (!data.items?.length) {
    return <div className="jx-ce-empty">{t('我的空间暂无文件')}</div>;
  }
  return (
    <div className="jx-ms-list">
      {data.total > data.items.length && (
        <div className="jx-ms-listMeta">{t('共 {total} 个文件，显示前 {n} 项', { total: data.total, n: data.items.length })}</div>
      )}
      {data.items.map((item) => (
        <div key={item.artifact_id} className="jx-ms-listItem">
          <span className="jx-ms-fileIcon"><FileIcon mimeType={item.mime_type} /></span>
          <span className="jx-ms-fileName">{item.name}</span>
          <span className="jx-ms-fileMeta">
            {formatBytes(item.size_bytes)}
            {item.chat_title && <> · {item.chat_title}</>}
          </span>
        </div>
      ))}
    </div>
  );
}

function StageFileBody({ data }: { data: unknown }) {
  const info = (typeof data === 'object' && data !== null)
    ? data as { path?: string; name?: string; size_bytes?: number; mime_type?: string }
    : {};
  return (
    <div className="jx-ms-list">
      <div className="jx-ms-listItem" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 4 }}>
        <span className="jx-ms-fileName">{info.name ?? t('未知文件')}</span>
        <code style={{ fontSize: 11, color: 'var(--color-text-tertiary)', wordBreak: 'break-all' }}>{info.path ?? ''}</code>
        {info.size_bytes != null && (
          <span className="jx-ms-fileMeta">{formatBytes(info.size_bytes)}{info.mime_type ? ` · ${info.mime_type}` : ''}</span>
        )}
      </div>
    </div>
  );
}

function MySpaceBody({ toolName, data }: { toolName: string; data: unknown }) {
  if (data && typeof data === 'object' && 'error' in (data as Record<string, unknown>)) {
    return <pre className="jx-ce-stderr">{String((data as Record<string, unknown>).error)}</pre>;
  }
  if (toolName === 'space_list_myspace_files')
    return <FilesBody data={data as Parameters<typeof FilesBody>[0]['data']} />;
  if (toolName === 'space_stage_myspace_file')
    return <StageFileBody data={data} />;
  return <pre className="jx-ce-stdout">{JSON.stringify(data, null, 2)}</pre>;
}

/**
 * Body-only content for MySpace tools — used inside ToolCallRow.
 * Renders the body without any outer card or header.
 */
export function MySpaceBodyContent({ tool }: { tool: ToolCall }) {
  const parsed = useMemo(() => parseOutput(tool.output), [tool.output]);
  if (parsed === null) return <div className="jx-ce-empty">{t('无结果')}</div>;
  return <MySpaceBody toolName={tool.name} data={parsed} />;
}
