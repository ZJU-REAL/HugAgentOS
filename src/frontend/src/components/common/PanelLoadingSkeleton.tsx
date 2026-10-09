import { t } from '../../i18n';

/** Code-loading placeholder; data-loading layouts belong to their feature. */
export default function PanelLoadingSkeleton() {
  return (
    <div className="jx-panelLoading" role="status" aria-label={t('加载中…')} aria-busy="true">
      <div className="jx-panelLoading-body" aria-hidden="true">
        <div className="jx-skeletonBlock jx-panelLoading-title" />
        <div className="jx-skeletonBlock jx-panelLoading-subtitle" />
        <div className="jx-skeletonBlock jx-panelLoading-toolbar" />
        <div className="jx-panelLoading-rows">
          {Array.from({ length: 4 }, (_, index) => (
            <div className="jx-panelLoading-row" key={index}>
              <div className="jx-skeletonBlock jx-panelLoading-icon" />
              <div className="jx-panelLoading-text">
                <div className="jx-skeletonBlock jx-panelLoading-name" />
                <div className="jx-skeletonBlock jx-panelLoading-description" />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
