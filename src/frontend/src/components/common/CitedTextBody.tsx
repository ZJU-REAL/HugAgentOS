import { useEffect, useMemo, useRef } from 'react';
import { findCitedRange } from '../../utils/highlight';
import { WindowedText } from './WindowedText';
import { t } from '../../i18n';

interface CitedTextBodyProps {
  /** 完整正文——不要在调用处先截断，定位靠的就是完整文本 */
  text: string;
  /** 被引用的片段（citation.snippet）；命中后只展示它附近的一段并高亮 */
  highlight?: string;
  className?: string;
}

/**
 * 长正文详情体：把被引用的那一段找出来、高亮、只展示它附近的内容。
 *
 * 起因是引用打开卡片后「永远从头显示」——正文两万字、引用的是最后一段时，
 * 用户看到的仍是开头，等于没打开。现在改为以命中处取景（上下文续展交给
 * WindowedText），并把滚动条送到跟前；设 scrollTop 而不是 scrollIntoView，
 * 免得连带把整个页面 / 弹窗一起滚走。
 */
export function CitedTextBody({ text, highlight, className }: CitedTextBodyProps) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const markRef = useRef<HTMLElement | null>(null);

  const hit = useMemo(
    () => (highlight ? !!findCitedRange(text, highlight) : false),
    [text, highlight],
  );

  useEffect(() => {
    const wrap = wrapRef.current;
    const mark = markRef.current;
    if (!wrap || !mark) return;
    const offset = mark.getBoundingClientRect().top - wrap.getBoundingClientRect().top;
    // 命中已在可视区内就别动滚动条——取景框本就把它放在开头附近
    if (offset >= 0 && offset < wrap.clientHeight - 40) return;
    wrap.scrollTop = Math.max(0, wrap.scrollTop + offset - wrap.clientHeight / 3);
  }, [hit, text]);

  return (
    <div className={className ? `${className} jx-citedBody` : 'jx-citedBody'} ref={wrapRef}>
      {hit && <div className="jx-citedBody-hint">{t('已定位到正文引用的位置')}</div>}
      <WindowedText
        text={text}
        fragment={highlight}
        onHitRef={(el) => { markRef.current = el; }}
      />
    </div>
  );
}

export default CitedTextBody;
