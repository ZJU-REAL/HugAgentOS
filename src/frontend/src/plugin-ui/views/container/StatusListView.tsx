/** List whose rows carry a lifecycle state — scheduled tasks, runs, audit rows. */

import { t } from '../../../i18n';
import { readRecords, readText } from '../../pointer';
import { usableActions, type ViewProps } from '../../ViewProps';
import { InlineActions } from '../shared/InlineActions';
import { RawValue } from '../shared/RawValue';

/** Map an upstream status word onto one of four visual tones. */
function tone(status: string): string {
  if (/运行|active|running|已启用|成功|success/i.test(status)) return 'ok';
  if (/暂停|paused|停用|disabled/i.test(status)) return 'muted';
  if (/失败|error|异常|failed/i.test(status)) return 'error';
  if (/等待|pending|排队|queued/i.test(status)) return 'pending';
  return '';
}

export function StatusListView({ data, map, actions, ctx }: ViewProps) {
  const items = readRecords(data, map.items);
  if (items.length === 0) return <div className="jx-pv-empty">{t('暂无记录')}</div>;
  const itemActions = usableActions(actions, 'item', ctx.toolName);

  return (
    <ul className="jx-pv-statusList">
      {items.map((item, index) => {
        const title = readText(item, map.title) || t('第 {n} 条', { n: index + 1 });
        const status = readText(item, map.status);
        const description = readText(item, map.description);
        const time = readText(item, map.time);
        return (
          <li className="jx-pv-statusRow" key={index}>
            <span className={`jx-pv-statusDot ${tone(status)}`} aria-hidden="true" />
            <div className="jx-pv-statusMain">
              <button
                type="button"
                className="jx-pv-statusTitle"
                onClick={() => ctx.openDetail?.(title, <RawValue value={item} expanded />)}
              >
                {title}
              </button>
              {description && <span className="jx-pv-statusDesc">{description}</span>}
            </div>
            {status && <span className={`jx-pv-statusTag ${tone(status)}`}>{status}</span>}
            {time && <time className="jx-pv-statusTime">{time}</time>}
            <InlineActions actions={itemActions} item={item} ctx={ctx} />
          </li>
        );
      })}
    </ul>
  );
}
