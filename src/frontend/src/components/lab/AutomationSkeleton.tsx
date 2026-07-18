import { LeftOutlined } from '@ant-design/icons';
import { t } from '../../i18n';

export function AutomationListSkeleton({ count = 4 }: { count?: number }) {
  return (
    <div className="jx-automation-section" aria-hidden="true">
      <div className="jx-skeletonBlock jx-automation-skSectionTitle" />
      {Array.from({ length: count }).map((_, idx) => (
        <div key={idx} className="jx-automation-card jx-automation-card--skeleton">
          <div className="jx-automation-card-main">
            <div className="jx-automation-card-header">
              <div className="jx-skeletonBlock jx-automation-skName" />
              <div className="jx-skeletonBlock jx-automation-skBadge" />
            </div>
            <div className="jx-automation-card-meta">
              <div className="jx-skeletonBlock jx-automation-skMeta" />
              <div className="jx-skeletonBlock jx-automation-skMeta jx-automation-skMeta--wide" />
              <div className="jx-skeletonBlock jx-automation-skMeta" />
            </div>
          </div>
          <div className="jx-automation-card-actions">
            <div className="jx-skeletonBlock jx-automation-skAction" />
          </div>
        </div>
      ))}
    </div>
  );
}

export function AutomationDetailSkeleton({ onBack }: { onBack?: () => void }) {
  return (
    <div className="jx-automation-detail-top" aria-hidden="true">
      <button
        className="jx-automation-detail-backBtn"
        onClick={onBack}
        aria-label={t('返回')}
        type="button"
      >
        <LeftOutlined />
      </button>

      <div className="jx-automation-detail-content">
        <div className="jx-automation-detail-nameRow">
          <div className="jx-skeletonBlock jx-automation-skDetailIcon" />
          <div className="jx-skeletonBlock jx-automation-skDetailName" />
          <div className="jx-skeletonBlock jx-automation-skDetailBadge" />
        </div>

        <div className="jx-automation-detail-metaRow">
          <div className="jx-skeletonBlock jx-automation-skDetailMeta" />
          <div className="jx-skeletonBlock jx-automation-skDetailMeta jx-automation-skDetailMeta--wide" />
          <div className="jx-skeletonBlock jx-automation-skDetailMeta" />
          <div className="jx-skeletonBlock jx-automation-skDetailMeta jx-automation-skDetailMeta--wide" />
        </div>

        <hr className="jx-automation-detail-divider" />

        <div className="jx-automation-detail-sections">
          {Array.from({ length: 2 }).map((_, idx) => (
            <section key={idx} className="jx-automation-detail-section">
              <div className="jx-automation-detail-sectionHead">
                <div className="jx-skeletonBlock jx-automation-skSectionTitle" />
              </div>
              <div className="jx-automation-detail-grid">
                {Array.from({ length: 3 }).map((__, row) => (
                  <div key={row} className="jx-automation-detail-field">
                    <div className="jx-skeletonBlock jx-automation-skFieldLabel" />
                    <div className="jx-skeletonBlock jx-automation-skFieldValue" />
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
