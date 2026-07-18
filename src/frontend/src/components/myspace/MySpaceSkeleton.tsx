import type { MySpaceTab } from '../../types';
import type { AssetFilter } from '../../stores/mySpaceStore';

interface Props {
  tab: MySpaceTab;
  assetFilter: AssetFilter;
  rows?: number;
}

export function MySpaceSkeleton({ tab, assetFilter, rows }: Props) {
  if (tab === 'favorites') return <FavoriteSkeleton count={rows ?? 4} />;
  if (tab === 'assets' && assetFilter === 'image') return <ImageGridSkeleton count={rows ?? 8} />;
  return <DocumentTableSkeleton count={rows ?? 6} />;
}

function DocumentTableSkeleton({ count }: { count: number }) {
  return (
    <div className="jx-mySpace-docTable" aria-hidden="true">
      <div className="jx-mySpace-docTable-header">
        <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--check" />
        <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--name">
          <div className="jx-skeletonBlock jx-mySpace-skHeaderCell" />
        </div>
        <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--size">
          <div className="jx-skeletonBlock jx-mySpace-skHeaderCell jx-mySpace-skHeaderCell--narrow" />
        </div>
        <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--source">
          <div className="jx-skeletonBlock jx-mySpace-skHeaderCell jx-mySpace-skHeaderCell--narrow" />
        </div>
        <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--time">
          <div className="jx-skeletonBlock jx-mySpace-skHeaderCell" />
        </div>
        <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--actions" />
      </div>
      {Array.from({ length: count }).map((_, idx) => (
        <div key={idx} className="jx-mySpace-docRow jx-mySpace-docRow--skeleton">
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--check" />
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--name">
            <div className="jx-skeletonBlock jx-mySpace-skDocIcon" />
            <div className="jx-skeletonBlock jx-mySpace-skDocName" />
          </div>
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--size">
            <div className="jx-skeletonBlock jx-mySpace-skDocCell" />
          </div>
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--source">
            <div className="jx-skeletonBlock jx-mySpace-skDocCell" />
          </div>
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--time">
            <div className="jx-skeletonBlock jx-mySpace-skDocCell jx-mySpace-skDocCell--wide" />
          </div>
          <div className="jx-mySpace-docTable-col jx-mySpace-docTable-col--actions">
            <div className="jx-skeletonBlock jx-mySpace-skDocAction" />
          </div>
        </div>
      ))}
    </div>
  );
}

function ImageGridSkeleton({ count }: { count: number }) {
  return (
    <div className="jx-mySpace-imgGrid" aria-hidden="true">
      {Array.from({ length: count }).map((_, idx) => (
        <div key={idx} className="jx-mySpace-imgItem jx-mySpace-imgItem--skeleton">
          <div className="jx-skeletonBlock jx-mySpace-skImgCell" />
        </div>
      ))}
    </div>
  );
}

function FavoriteSkeleton({ count }: { count: number }) {
  return (
    <div className="jx-mySpace-favList" aria-hidden="true">
      {Array.from({ length: count }).map((_, idx) => (
        <div key={idx} className="jx-mySpace-favCard jx-mySpace-favCard--skeleton">
          <div className="jx-mySpace-favHeader">
            <div className="jx-skeletonBlock jx-mySpace-skFavSource" />
            <div className="jx-skeletonBlock jx-mySpace-skFavTime" />
          </div>
          <div className="jx-skeletonBlock jx-mySpace-skFavLine" />
          <div className="jx-skeletonBlock jx-mySpace-skFavLine jx-mySpace-skFavLine--short" />
          <div className="jx-mySpace-favActions">
            <div className="jx-skeletonBlock jx-mySpace-skFavAction" />
            <div className="jx-skeletonBlock jx-mySpace-skFavAction" />
          </div>
        </div>
      ))}
    </div>
  );
}
