import { useEffect, useRef, useState } from 'react';
import { Button, Input, message } from 'antd';
import { CheckCircleOutlined, EditOutlined, CloseCircleOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import {
  EVALUATION_REASONS, EVALUATION_REASON_LABEL as REASON_LABELS, EVALUATION_VERDICT_LABEL as VERDICT_LABELS,
  isCurrentReason, type EvaluationReason, type EvaluationRecord, type EvaluationVerdict,
} from '../../api/evaluation';
import { useEvaluationStore } from '../../stores/evaluationStore';

const VERDICT_ICONS: Record<EvaluationVerdict, React.ReactNode> = {
  adopt: <CheckCircleOutlined />,
  revise: <EditOutlined />,
  reject: <CloseCircleOutlined />,
};

const VERDICT_ORDER: readonly EvaluationVerdict[] = ['adopt', 'revise', 'reject'];

interface EvaluationCardProps {
  chatId: string;
  messageId: string;
}

/**
 * Evaluation card under an assistant answer: one click on 满意 / 需修改 / 不满意 records the
 * verdict; for 需修改 / 不满意 the user may then pick reason dimensions and add a note.
 */
export function EvaluationCard({ chatId, messageId }: EvaluationCardProps) {
  const record = useEvaluationStore((s) => s.records[messageId]);
  const submit = useEvaluationStore((s) => s.submit);
  const [detailOpen, setDetailOpen] = useState(false);
  const [reasons, setReasons] = useState<EvaluationReason[]>([]);
  const [comment, setComment] = useState('');
  const [saving, setSaving] = useState(false);
  const detailRef = useRef<HTMLDivElement>(null);

  // Editing the details starts from what was saved.
  useEffect(() => {
    if (!detailOpen) return;
    // Retired reasons are not offered any more, so editing starts from the current ones only.
    setReasons((record?.reasons ?? []).filter(isCurrentReason));
    setComment(record?.comment ?? '');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detailOpen]);

  // The details open below the answer, often past the fold: bring the submit row into view.
  useEffect(() => {
    if (!detailOpen) return;
    const frame = requestAnimationFrame(() => {
      detailRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
    return () => cancelAnimationFrame(frame);
  }, [detailOpen]);

  const save = async (next: EvaluationRecord) => {
    setSaving(true);
    try {
      await submit(chatId, messageId, next);
      return true;
    } catch (error) {
      const reason = error instanceof Error ? error.message : t('网络异常');
      void message.error(t('评价没有保存：{reason}，请稍后再试', { reason }));
      return false;
    } finally {
      setSaving(false);
    }
  };

  const pick = async (verdict: EvaluationVerdict) => {
    if (saving) return;
    const keepDetails = record && record.verdict !== 'adopt' && verdict !== 'adopt';
    const ok = await save({
      verdict,
      reasons: keepDetails ? record.reasons : [],
      comment: keepDetails ? record.comment : '',
    });
    if (ok) setDetailOpen(verdict !== 'adopt');
  };

  const toggleReason = (reason: EvaluationReason) => {
    setReasons((current) => (current.includes(reason)
      ? current.filter((r) => r !== reason)
      : EVALUATION_REASONS.filter((r) => r === reason || current.includes(r))));
  };

  const saveDetails = async () => {
    if (!record) return;
    if (await save({ verdict: record.verdict, reasons, comment })) {
      setDetailOpen(false);
      void message.success(t('已记录，我们会据此改进回答'));
    }
  };

  const verdict = record?.verdict;
  const summary = record && record.verdict !== 'adopt'
    ? [record.reasons.map((r) => REASON_LABELS[r]).join('、'), record.comment].filter(Boolean).join(' · ')
    : '';

  return (
    <section
      className="jx-evalCard"
      aria-label={t('评价这个回答')}
      data-evaluated={verdict ? 'true' : 'false'}
      data-eval-message={messageId}
    >
      <div className="jx-evalCard-row">
        <span className="jx-evalCard-prompt">
          {verdict ? t('已评价：{verdict}', { verdict: VERDICT_LABELS[verdict] }) : t('你是否对智能体回答结果感到满意？')}
          {summary && <span className="jx-evalCard-summary">{summary}</span>}
        </span>
        <div className="jx-evalCard-actions" role="group" aria-label={t('评价')}>
          {VERDICT_ORDER.map((v) => (
            <button
              key={v}
              type="button"
              className={`jx-evalCard-btn jx-evalCard-btn--${v}${verdict === v ? ' is-active' : ''}`}
              aria-pressed={verdict === v}
              disabled={saving}
              onClick={() => { void pick(v); }}
            >
              {VERDICT_ICONS[v]}
              <span>{VERDICT_LABELS[v]}</span>
            </button>
          ))}
          {verdict && verdict !== 'adopt' && !detailOpen && (
            <button type="button" className="jx-evalCard-link" onClick={() => setDetailOpen(true)}>
              {summary ? t('修改说明') : t('补充原因')}
            </button>
          )}
        </div>
      </div>
      {detailOpen && verdict && verdict !== 'adopt' && (
        <div ref={detailRef} className="jx-evalCard-detail jx-anim-fadeIn">
          <div className="jx-evalCard-reasons" role="group" aria-label={t('主要问题（可多选）')}>
            <span className="jx-evalCard-label">{t('主要问题（可多选）')}</span>
            {EVALUATION_REASONS.map((r) => (
              <button
                key={r}
                type="button"
                className={`jx-evalCard-reason${reasons.includes(r) ? ' is-active' : ''}`}
                aria-pressed={reasons.includes(r)}
                onClick={() => toggleReason(r)}
              >
                {REASON_LABELS[r]}
              </button>
            ))}
          </div>
          <label className="jx-evalCard-label" htmlFor={`eval-comment-${messageId}`}>{t('具体说明（选填）')}</label>
          <Input.TextArea
            id={`eval-comment-${messageId}`}
            rows={2}
            maxLength={2000}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder={t('例如：编制数口径应以 2026 年批复为准')}
          />
          <div className="jx-evalCard-detailActions">
            <Button size="small" type="text" onClick={() => setDetailOpen(false)}>{t('跳过')}</Button>
            <Button size="small" type="primary" loading={saving} onClick={() => { void saveDetails(); }}>{t('提交说明')}</Button>
          </div>
        </div>
      )}
    </section>
  );
}
