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
