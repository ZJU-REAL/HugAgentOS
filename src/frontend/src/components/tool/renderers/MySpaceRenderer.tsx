import { useMemo } from 'react';
import {
  FileTextOutlined, PictureOutlined, FileOutlined, StarFilled,
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
    return <FileTextOutlined style={{ color: '#4472c4' }} />;
  if (mimeType.includes('sheet') || mimeType.includes('excel') || mimeType.includes('csv'))
    return <FileTextOutlined style={{ color: '#217346' }} />;
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
        <code style={{ fontSize: 11, color: '#6b7280', wordBreak: 'break-all' }}>{info.path ?? ''}</code>
        {info.size_bytes != null && (
          <span className="jx-ms-fileMeta">{formatBytes(info.size_bytes)}{info.mime_type ? ` · ${info.mime_type}` : ''}</span>
        )}
      </div>
    </div>
  );
}

function FavoritesBody({ data }: { data: { total: number; items: Array<{
  chat_id: string; title: string; last_message_preview?: string; updated_at?: string;
}> } }) {
  if (!data.items?.length) {
    return <div className="jx-ce-empty">{t('暂无收藏会话')}</div>;
  }
  return (
    <div className="jx-ms-list">
      {data.total > data.items.length && (
        <div className="jx-ms-listMeta">{t('共 {total} 个收藏，显示前 {n} 项', { total: data.total, n: data.items.length })}</div>
      )}
      {data.items.map((item) => (
        <div key={item.chat_id} className="jx-ms-listItem">
          <span className="jx-ms-fileIcon"><StarFilled style={{ color: '#f59e0b', fontSize: 13 }} /></span>
          <span className="jx-ms-fileName">{item.title}</span>
          {item.last_message_preview && (
            <span className="jx-ms-fileMeta jx-ms-preview">{item.last_message_preview}</span>
          )}
        </div>
      ))}
    </div>
  );
}

function MessagesBody({ data }: { data: { chat_id: string; messages: Array<{
  role: string; content: string; created_at?: string;
}> } }) {
  if (!data.messages?.length) {
    return <div className="jx-ce-empty">{t('会话无消息记录')}</div>;
  }
  return (
    <div className="jx-ms-list">
      <div className="jx-ms-listMeta">{t('共 {n} 条消息', { n: data.messages.length })}</div>
      {data.messages.slice(0, 6).map((msg, idx) => (
        <div key={idx} className={`jx-ms-msgItem jx-ms-msgItem--${msg.role}`}>
          <span className="jx-ms-msgRole">{msg.role === 'user' ? t('用户') : 'AI'}</span>
          <span className="jx-ms-msgContent">{(msg.content || '').slice(0, 120)}{msg.content?.length > 120 ? '…' : ''}</span>
        </div>
      ))}
      {data.messages.length > 6 && (
        <div className="jx-ms-listMeta">{t('…还有 {n} 条消息', { n: data.messages.length - 6 })}</div>
      )}
    </div>
  );
}

function MySpaceBody({ toolName, data }: { toolName: string; data: unknown }) {
  if (data && typeof data === 'object' && 'error' in (data as Record<string, unknown>)) {
    return <pre className="jx-ce-stderr">{String((data as Record<string, unknown>).error)}</pre>;
  }
  if (toolName === 'list_myspace_files')
    return <FilesBody data={data as Parameters<typeof FilesBody>[0]['data']} />;
  if (toolName === 'stage_myspace_file')
    return <StageFileBody data={data} />;
  if (toolName === 'list_favorite_chats')
    return <FavoritesBody data={data as Parameters<typeof FavoritesBody>[0]['data']} />;
  if (toolName === 'get_chat_messages')
    return <MessagesBody data={data as Parameters<typeof MessagesBody>[0]['data']} />;
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
