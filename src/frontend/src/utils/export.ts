import type { ChatMessage } from '../types';
import html2pdf from 'html2pdf.js';
import { formatDateKey, formatTime } from './date';
import { mdToHtml } from './markdown';
import { t } from '../i18n';

function createPdfHeaderImage(text: string): string {
  const canvas = document.createElement('canvas');
  canvas.width = 1200;
  canvas.height = 84;
  const context = canvas.getContext('2d');
  if (!context) return '';

  context.clearRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = '#787878';
  context.font = "36px 'Microsoft YaHei', 'PingFang SC', sans-serif";
  context.textAlign = 'right';
  context.textBaseline = 'middle';
  context.fillText(text, canvas.width - 16, canvas.height / 2);
  return canvas.toDataURL('image/png');
}

function formatChatTimestamp(timestamp?: number): string {
  const date = timestamp ? new Date(timestamp) : new Date();
  return `${formatDateKey(date)} ${formatTime(date)}`;
}

export function getMessageExportText(msg: ChatMessage): string {
  if (msg.segments) {
    const text = msg.segments
      .filter((s) => s.type === 'text')
      .map((s) => s.content || '')
      .join('\n')
      .trim();
    if (text) return text;
  }
  return String(msg.content || '').trim();
}

function buildChatExportHtml(chatTitle: string, messages: ChatMessage[]): string {
  const title = chatTitle || t('对话记录');
  let html = `
    <div style="font-family: 'Microsoft YaHei', 'PingFang SC', 'Helvetica Neue', sans-serif; padding: 26px 20px 20px; color: #333; line-height: 1.8;">
      <style>
        .pdf-msg table { border-collapse: collapse; width: 100%; margin: 8px 0; }
        .pdf-msg th, .pdf-msg td { border: 1px solid #ddd; padding: 6px 10px; text-align: left; font-size: 12px; }
        .pdf-msg th { background: #f0f0f0; font-weight: 600; }
        .pdf-msg pre { background: #f5f5f5; padding: 10px; border-radius: 4px; overflow-x: auto; font-size: 12px; }
        .pdf-msg code { font-family: Consolas, 'Courier New', monospace; font-size: 12px; background: #f5f5f5; padding: 1px 4px; border-radius: 3px; }
        .pdf-msg pre code { background: none; padding: 0; }
        .pdf-msg ul, .pdf-msg ol { padding-left: 20px; margin: 6px 0; }
        .pdf-msg li { margin: 2px 0; }
        .pdf-msg p { margin: 4px 0; }
        .pdf-msg h1, .pdf-msg h2, .pdf-msg h3, .pdf-msg h4 { margin: 10px 0 6px; }
        .pdf-msg blockquote { border-left: 3px solid #ddd; padding-left: 12px; color: #666; margin: 8px 0; }
        .pdf-msg strong { font-weight: 700; }
      </style>
      <h2 style="text-align:center; color:#1a1a1a; margin-bottom:24px; font-size:18px;">${title}</h2>
  `;

  if (messages.length === 0) {
    html += `<p style="color:#999; text-align:center;">${t('（该对话暂无消息）')}</p>`;
  } else {
    messages.forEach((msg) => {
      const isUser = msg.role === 'user';
      const role = isUser ? t('用户') : t('助手');
      const roleColor = isUser ? '#126DFF' : '#333';
      const bgColor = isUser ? '#f0f7ff' : '#f9f9f9';
      const text = getMessageExportText(msg);
      const rendered = isUser ? text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br/>') : mdToHtml(text);
      html += `
        <div style="margin-bottom:16px; padding:12px 16px; border-radius:8px; background:${bgColor};">
          <div style="font-weight:600; color:${roleColor}; margin-bottom:6px; font-size:14px;">【${role}】</div>
          <div class="pdf-msg" style="font-size:13px; word-break:break-word;">${rendered}</div>
        </div>
      `;
    });
  }

  html += '</div>';
  return html;
}

export function triggerPdfDownload(filename: string, chatTitle: string, messages: ChatMessage[], chatTimestamp?: number): void {
  const htmlContent = buildChatExportHtml(chatTitle, messages);
  const container = document.createElement('div');
  container.innerHTML = htmlContent;
  document.body.appendChild(container);

  const worker = html2pdf()
    .set({
      margin: [18, 10, 10, 10],
      filename,
      image: { type: 'jpeg', quality: 0.98 },
      html2canvas: { scale: 2, useCORS: true },
      jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' },
    })
    .from(container);

  ((worker as any).toPdf?.() ?? worker)
    .get?.('pdf')
    .then((pdf: any) => {
      const pageCount = pdf.internal.getNumberOfPages();
      const pageWidth = pdf.internal.pageSize.getWidth();
      const headerText = `${t('HugAgentOS')}(${formatChatTimestamp(chatTimestamp)})`;
      const headerImage = createPdfHeaderImage(headerText);
      const headerWidth = 98;
      const headerHeight = 6.86;
      const rightPadding = 10;
      for (let pageIndex = 1; pageIndex <= pageCount; pageIndex += 1) {
        pdf.setPage(pageIndex);
        if (headerImage) {
          pdf.addImage(headerImage, 'PNG', pageWidth - rightPadding - headerWidth, 4, headerWidth, headerHeight);
        }
      }
    })
    .then(() => (worker as any).save?.())
    .finally(() => {
      document.body.removeChild(container);
    });
}

export function toSafeFileName(name: string): string {
  return name.replace(/[\\/:*?"<>|]/g, '-').replace(/\s+/g, ' ').trim();
}
