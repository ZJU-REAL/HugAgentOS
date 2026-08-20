import { useMemo, useState } from 'react';
import { findCitedHighlight } from '../../utils/highlight';
import { t } from '../../i18n';

interface WindowedTextProps {
  /** 完整正文——不要在调用处先截断，定位与"还有多少"都要靠完整文本算 */
  text: string;
  /** 被引用的片段（citation.snippet）；命中后只展示它附近的一段，两头可续展 */
  fragment?: string;
  className?: string;
  /** 没有命中时的默认展示上限（字） */
  clamp?: number;
  /** 命中时窗口向前 / 向后各留多少字 */
  before?: number;
  after?: number;
  /** 命中处的 DOM，交给外层做滚动定位 */
  onHitRef?: (el: HTMLElement | null) => void;
}

/** 每次「展开上文 / 下文」续多少字 */
const STEP = 2000;

/**
 * 长文本的取景框：命中引用片段时只渲染它**附近**的一段，两头留「展开上文 / 下文 /
 * 显示全文」。
 *
 * 之所以不直接全展开：证据动辄几万字，一次性铺开既慢又淹没重点——用户要看的是
 * 「这句话是从哪儿来的」，不是把整份结果读一遍。窗口给足上下文（默认前 400 / 后 900 字），
 * 想看更多再一步步续。
 *
 * 命中区间走 `findCitedHighlight`（不是 findCitedRange）：整体型锚点的 snippet 本来就是
 * 正文开头的截断、列表型条目的 snippet 约等于整条记录，这两种"命中"涂出来毫无信息量，
 * 一律按未命中处理——正常从头显示，不画高亮框。
 */
export function WindowedText({
  text,
  fragment,
  className,
  clamp = 500,
  before = 400,
  after = 900,
  onHitRef,
}: WindowedTextProps) {
  const range = useMemo(
    () => findCitedHighlight(text, fragment),
    [text, fragment],
  );
  const [full, setFull] = useState(false);
  const [extraBefore, setExtraBefore] = useState(0);
  const [extraAfter, setExtraAfter] = useState(0);

  const cls = className ? `jx-wt ${className}` : 'jx-wt';
  const chars = (n: number) => n.toLocaleString('zh-CN');

  // ── 无命中：普通长文本，夹到 clamp 再给「展开全部」 ──
  if (!range) {
    if (full || text.length <= clamp) {
      return (
        <span className={cls}>
          {text}
          {text.length > clamp && (
            <button type="button" className="jx-wt-btn" onClick={() => setFull(false)}>
              {t('收起')}
            </button>
          )}
        </span>
      );
    }
    return (
      <span className={cls}>
        {text.slice(0, clamp)}
        <span className="jx-wt-ellipsis">…</span>
        <button type="button" className="jx-wt-btn" onClick={() => setFull(true)}>
          {t('展开全部 {n} 字', { n: chars(text.length) })}
        </button>
      </span>
    );
  }

  // ── 有命中：只渲染命中附近的取景框 ──
  const start = full ? 0 : Math.max(0, range[0] - before - extraBefore);
  const end = full ? text.length : Math.min(text.length, range[1] + after + extraAfter);
  const restBefore = start;
  const restAfter = text.length - end;

  return (
    <span className={cls}>
      {restBefore > 0 && (
        <button
          type="button"
          className="jx-wt-btn jx-wt-btn--block"
          onClick={() => setExtraBefore(extraBefore + STEP)}
        >
          {t('展开上文（还有 {n} 字）', { n: chars(restBefore) })}
        </button>
      )}
      {text.slice(start, range[0])}
      <mark className="jx-citedHit" ref={onHitRef as never} title={t('正文引用的就是这一段')}>
        {text.slice(range[0], range[1])}
      </mark>
      {text.slice(range[1], end)}
      {restAfter > 0 && (
        <button
          type="button"
          className="jx-wt-btn jx-wt-btn--block"
          onClick={() => setExtraAfter(extraAfter + STEP)}
        >
          {t('展开下文（还有 {n} 字）', { n: chars(restAfter) })}
        </button>
      )}
      {(restBefore > 0 || restAfter > 0) && (
        <button type="button" className="jx-wt-btn jx-wt-btn--block" onClick={() => setFull(true)}>
          {t('显示全文')}
        </button>
      )}
    </span>
  );
}

export default WindowedText;
