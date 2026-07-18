export function AppLoadingSkeleton() {
  return (
    <div className="jx-appLoading" aria-hidden="true">
      <aside className="jx-appLoading-sidebar">
        <div className="jx-appLoading-brand">
          <div className="jx-skeletonBlock jx-appLoading-skLogo" />
          <div className="jx-skeletonBlock jx-appLoading-skBrandText" />
        </div>
        <div className="jx-skeletonBlock jx-appLoading-skNewChat" />
        <div className="jx-appLoading-navList">
          {Array.from({ length: 5 }).map((_, idx) => (
            <div key={idx} className="jx-skeletonBlock jx-appLoading-skNavItem" />
          ))}
        </div>
        <div className="jx-appLoading-historyList">
          <div className="jx-skeletonBlock jx-appLoading-skHistoryGroupTitle" />
          {Array.from({ length: 4 }).map((_, idx) => (
            <div key={idx} className="jx-skeletonBlock jx-appLoading-skHistoryItem" />
          ))}
          <div className="jx-skeletonBlock jx-appLoading-skHistoryGroupTitle" />
          {Array.from({ length: 3 }).map((_, idx) => (
            <div key={`b-${idx}`} className="jx-skeletonBlock jx-appLoading-skHistoryItem" />
          ))}
        </div>
      </aside>
      <main className="jx-appLoading-main">
        <div className="jx-appLoading-center">
          <div className="jx-skeletonBlock jx-appLoading-skTitle" />
          <div className="jx-skeletonBlock jx-appLoading-skSubtitle" />
          <div className="jx-skeletonBlock jx-appLoading-skInput" />
          <div className="jx-appLoading-cards">
            {Array.from({ length: 4 }).map((_, idx) => (
              <div key={idx} className="jx-skeletonBlock jx-appLoading-skCard" />
            ))}
          </div>
        </div>
      </main>
    </div>
  );
}

export default AppLoadingSkeleton;
