import { t } from '../../i18n';

export function ProjectListSkeleton() {
  return (
    <section className="jx-projects-section" role="status" aria-label={t('加载中…')} aria-busy="true">
      <div className="jx-projects-skeletonContent" aria-hidden="true">
        <div className="jx-skeletonBlock jx-projects-skSection" />
        <div className="jx-projects-grid">
          {Array.from({ length: 4 }, (_, index) => (
            <div className="jx-projectCard jx-projectCard--skeleton" key={index}>
              <div className="jx-projectCard-header">
                <div className="jx-skeletonBlock jx-projects-skCardTitle" />
                <div className="jx-skeletonBlock jx-projects-skStar" />
              </div>
              <div className="jx-skeletonBlock jx-projects-skDescription" />
              <div className="jx-skeletonBlock jx-projects-skDescriptionShort" />
              <div className="jx-projectCard-footer">
                <div className="jx-skeletonBlock jx-projects-skMeta" />
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

export function ProjectsSkeleton() {
  return (
    <div className="jx-projects jx-projects--skeleton">
      <div className="jx-projects-shell">
        <div className="jx-projects-header" aria-hidden="true">
          <div className="jx-projects-skHeading">
            <div className="jx-skeletonBlock jx-projects-skTitle" />
            <div className="jx-skeletonBlock jx-projects-skSubtitle" />
          </div>
          <div className="jx-projects-actions">
            <div className="jx-skeletonBlock jx-projects-skControl" />
            <div className="jx-skeletonBlock jx-projects-skControl" />
          </div>
        </div>
        <div className="jx-skeletonBlock jx-projects-searchInput jx-projects-skSearch" aria-hidden="true" />
        <ProjectListSkeleton />
      </div>
    </div>
  );
}

export function ProjectHeaderSkeleton() {
  return (
    <section className="jx-projectHeader jx-projectHeader--skeleton" role="status" aria-label={t('加载中…')} aria-busy="true">
      <div aria-hidden="true">
        <div className="jx-skeletonBlock jx-projectHeader-skBack" />
        <div className="jx-projectHeader-heading">
          <div className="jx-skeletonBlock jx-projectHeader-skTitle" />
          <div className="jx-projectHeader-actions">
            {Array.from({ length: 3 }, (_, index) => <div className="jx-skeletonBlock jx-projectHeader-skAction" key={index} />)}
          </div>
        </div>
      </div>
    </section>
  );
}

const RAIL_CARD_ROWS = [1, 1, 2, 4];

export function ProjectRailSkeleton() {
  return (
    <div className="jx-projectRail" role="status" aria-label={t('加载中…')} aria-busy="true">
      {RAIL_CARD_ROWS.map((rows, index) => (
        <div className="jx-projectRail-card" key={index} aria-hidden="true">
          <div className="jx-projectRail-cardHeader">
            <div className="jx-skeletonBlock jx-projectRail-skTitle" />
            <div className="jx-skeletonBlock jx-projectRail-skAction" />
          </div>
          {Array.from({ length: rows }, (_, row) => <div className="jx-skeletonBlock jx-skeletonLines-line" key={row} />)}
        </div>
      ))}
    </div>
  );
}

/** Suspense fallback for the lazily loaded rail: same shell, skeleton body. */
export function ProjectWorkspaceRailSkeleton() {
  return (
    <aside className="jx-projectWorkspaceRail" aria-label={t('项目资料')}>
      <div className="jx-projectWorkspaceRail-body"><ProjectRailSkeleton /></div>
    </aside>
  );
}

export function ProjectConversationHistorySkeleton() {
  return (
    <div className="jx-projectConversationHistory-list" role="status" aria-label={t('加载中…')} aria-busy="true">
      {Array.from({ length: 3 }, (_, index) => (
        <div className="jx-projectConversationHistory-item jx-projectConversationHistory-item--skeleton" key={index} aria-hidden="true">
          <div className="jx-skeletonBlock jx-projectConversationHistory-skIcon" />
          <span className="jx-projectConversationHistory-body">
            <div className="jx-skeletonBlock jx-projectConversationHistory-skTitle" />
            <div className="jx-skeletonBlock jx-projectConversationHistory-skMeta" />
          </span>
        </div>
      ))}
    </div>
  );
}
