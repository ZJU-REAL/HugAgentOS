/** Chronological event stream with an explicit time axis and sentiment tint. */

import { t } from '../../../i18n';
import { readRecords, readText } from '../../pointer';
import type { ViewProps } from '../../ViewProps';
import { CiteTag } from '../shared/CiteTag';
import { RawValue } from '../shared/RawValue';

function sentimentClass(value: string): string {
  if (/正|利好|positive/i.test(value)) return ' positive';
  if (/负|利空|风险|negative/i.test(value)) return ' negative';
  return '';
}

export function TimelineView({ data, map, ctx }: ViewProps) {
  const items = readRecords(data, map.items);
  if (items.length === 0) return <div className="jx-pv-empty">{t('暂无事件')}</div>;

  return (
    <ul className="jx-pv-timeline">
      {items.map((item, index) => {
        const time = readText(item, map.time);
        const title = readText(item, map.title) || t('事件 {n}', { n: index + 1 });
        const snippet = readText(item, map.snippet);
        const source = readText(item, map.source);
        const sentiment = readText(item, map.sentiment);
        return (
          <li className={`jx-pv-timelineItem${sentimentClass(sentiment)}`} key={index}>
            <span className="jx-pv-timelineDot" aria-hidden="true" />
            <div className="jx-pv-timelineBody">
              <div className="jx-pv-timelineHead">
                {time && <time className="jx-pv-timelineTime">{time}</time>}
                <button
                  type="button"
                  className="jx-pv-timelineTitle"
                  onClick={() => ctx.openDetail?.(title, <RawValue value={item} expanded />)}
                >
                  {title}
                </button>
                <CiteTag item={item} />
              </div>
              {snippet && <p className="jx-pv-timelineSnippet">{snippet}</p>}
              {(source || sentiment) && (
                <div className="jx-pv-timelineMeta">
                  {source && <span>{source}</span>}
                  {sentiment && <span className="jx-pv-timelineSentiment">{sentiment}</span>}
                </div>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
