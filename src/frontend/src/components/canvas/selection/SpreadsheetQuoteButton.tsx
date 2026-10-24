import { CommentOutlined } from '@ant-design/icons';
import { t } from '../../../i18n';
import './SpreadsheetQuoteButton.css';

interface SpreadsheetQuoteButtonProps {
  /** The selected range as `Sheet1!A1:C5`. */
  range: string;
  onQuote: () => void;
}

/**
 * Univer draws the grid on a <canvas>, so there is no DOM selection for the generic
 * toolbar to pick up; the spreadsheet reports its selected range and this pill quotes it.
 */
export function SpreadsheetQuoteButton({ range, onQuote }: SpreadsheetQuoteButtonProps) {
  return (
    <button type="button" className="jx-spreadsheetQuote" onClick={onQuote} title={t('把选中的单元格引用到对话')}>
      <CommentOutlined className="jx-spreadsheetQuote-icon" />
      <span className="jx-spreadsheetQuote-label">{t('引用选区')}</span>
      <span className="jx-spreadsheetQuote-range">{range}</span>
    </button>
  );
}
