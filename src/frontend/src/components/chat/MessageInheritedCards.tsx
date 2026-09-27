import { CheckCircleFilled, SafetyCertificateOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import type { ChatMessage } from '../../types';
import { DataView } from '../tool/renderers/DataView';

interface MessageInheritedCardsProps { m: ChatMessage }

/** Forked cards describe past decisions; they must not poll or mutate source jobs. */
export function MessageInheritedCards({ m }: MessageInheritedCardsProps) {
  const entries = m.evolution?.state === 'settled' ? m.evolution.entries ?? [] : [];
  const governance = m.ontologyGovernance;
  return <>
    {entries.length > 0 && (
      <section className="jx-evolutionCard">
        <header className="jx-evolutionCard-head">
          <span className="jx-evolutionCard-title"><CheckCircleFilled /> {t('历史记忆（只读）')}</span>
        </header>
        <ul className="jx-evolutionCard-entries">
          {entries.map((entry, index) => <li className="jx-evolutionCard-entry" key={index}>
            <span className="jx-evolutionCard-layer">{entry.layer}</span>
            <div className="jx-evolutionCard-entryBody">
              <p className="jx-evolutionCard-entryText">{entry.text}</p>
              {(entry.why || entry.applies_to) && <p className="jx-evolutionCard-entryMeta">
                {entry.applies_to && <span>{t('适用于')}：{entry.applies_to}</span>}
                {entry.why && <span>{t('原因')}：{entry.why}</span>}
              </p>}
            </div>
          </li>)}
        </ul>
      </section>
    )}
    {governance && (
      <details className="jx-ontologyReviewInline">
        <summary><SafetyCertificateOutlined /> {t('历史本体校验（只读）')}</summary>
        <DataView value={governance} maxHeight={520} />
      </details>
    )}
  </>;
}
