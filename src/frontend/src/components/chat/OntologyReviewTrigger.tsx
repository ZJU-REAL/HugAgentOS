import {
  CheckCircleFilled,
  ExclamationCircleFilled,
  LoadingOutlined,
  RightOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';

import { t } from '../../i18n';
import { useCanvasStore } from '../../stores';
import type { OntologyGovernanceSummary } from '../../types';

interface OntologyReviewTriggerProps {
  governance: OntologyGovernanceSummary;
  chatId: string;
  messageTs: number;
}

export function OntologyReviewTrigger({
  governance,
  chatId,
  messageTs,
}: OntologyReviewTriggerProps) {
  const openOntology = useCanvasStore((state) => state.openOntology);
  const review = governance.review && typeof governance.review === 'object'
    ? governance.review
    : {};
  const isStreaming = review.status === 'running' || governance.revision?.status === 'streaming';
  const needsManualReview = review.status === 'failed'
    || review.verdict === 'escalate'
    || review.manual_review?.required === true;
  const hasRevision = review.revised === true
    || Boolean(
      (typeof governance.revision?.content === 'string' && governance.revision.content)
      || (typeof review.candidate_answer === 'string' && review.candidate_answer),
    );
  const passed = review.status === 'completed' && review.verdict === 'pass' && !hasRevision;
  const manualReviewItems = Array.isArray(review.manual_review?.items)
    ? review.manual_review.items
    : [];
  const hasManualReview = Boolean(review.manual_review)
    && (review.manual_review?.required === true || manualReviewItems.length > 0);

  if (!isStreaming && !hasRevision && !passed && !needsManualReview && !hasManualReview) {
    return null;
  }

  const title = isStreaming
    ? t('本体校验进行中')
    : needsManualReview
      ? t('本体校验需人工复核')
      : hasRevision
        ? t('本体校验已生成优化稿')
        : passed
          ? t('本体校验已通过')
          : t('查看本体校验');
  const description = isStreaming
    ? t('结果正在右侧实时生成')
    : t('在右侧面板查看完整结果');
  const icon = isStreaming
    ? <LoadingOutlined spin />
    : needsManualReview
      ? <ExclamationCircleFilled />
      : passed
        ? <CheckCircleFilled />
        : <SafetyCertificateOutlined />;

  return (
    <button
      type="button"
      className={`jx-ontologyReviewTrigger${isStreaming ? ' is-streaming' : ''}${needsManualReview ? ' is-warning' : ''}`}
      onClick={() => openOntology({ chatId, messageTs })}
      aria-label={title}
    >
      <span className="jx-ontologyReviewTrigger-icon">{icon}</span>
      <span className="jx-ontologyReviewTrigger-copy">
        <strong>{title}</strong>
        <small>{description}</small>
      </span>
      <RightOutlined className="jx-ontologyReviewTrigger-arrow" />
    </button>
  );
}
