import { useRef, type ReactNode } from 'react';
import { message } from 'antd';
import { CopyOutlined, PaperClipOutlined } from '@ant-design/icons';
import { CitationMarkdownBlock } from '../../citation';
import { UserMarkdownBlock } from '../../chat/UserMarkdownBlock';
import { copyHtmlToClipboard } from '../../../utils/clipboard';
import { DeliveredFiles } from '../../chat/DeliveredFiles';
import { type PersonalMessage } from '../api';
import { t } from '../../../i18n';

const NO_CITATIONS: never[] = [];

interface Props {
  row: PersonalMessage;
  author: ReactNode;
}

/** One message, rendered and copied the same way as the HugAgentOS chat. */
export function PersonalMessageRow({ row, author }: Props) {
  const body = useRef<HTMLDivElement>(null);
  async function copy() {
    // The rendered HTML keeps tables and lists in Word; the source is the plain text.
    const html = body.current?.innerHTML || '';
    if (await copyHtmlToClipboard(html, row.content)) message.success(t('已复制'));
    else message.error(t('复制失败，请手动复制'));
  }
  const user = row.role === 'user';
  return <article className={'pa-message pa-message-' + row.role} data-delivery={row.artifacts?.length ? true : undefined} data-task-id={row.work_item_id} data-task-ids={row.work_item_ids?.join(',')}>
    {author}
    <div className="pa-message-bubble">
      <div ref={body} className="jx-md pa-message-text">
        {user ? <UserMarkdownBlock text={row.content} /> : <CitationMarkdownBlock text={row.content} isMarkdown citations={NO_CITATIONS} />}
      </div>
      {!!row.artifacts?.length && <div className="pa-delivery-artifacts"><DeliveredFiles artifacts={row.artifacts} /></div>}
      {row.attachments?.length && !row.artifacts?.length ? <div className="pa-message-files">{row.attachments.map(file => <a className="pa-attachment" key={file.id} href={file.url} target="_blank" rel="noopener noreferrer"><PaperClipOutlined /> {file.name}</a>)}</div> : null}
      <div className="jx-msgActions pa-message-actions">
        <button type="button" className="jx-msgActionBtn" title={t('复制')} aria-label={t('复制')} onClick={() => void copy()}><CopyOutlined /></button>
      </div>
    </div>
  </article>;
}
