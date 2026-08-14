import React, { useCallback, useEffect, useState } from 'react';
import { DownOutlined, FileTextOutlined, FolderOutlined, RightOutlined } from '@ant-design/icons';
import { Spin } from 'antd';
import { getWikiFolders, getWikiPages } from '../../api';
import type { WikiFolder, WikiPageBrief } from '../../types';

/**
 * Wiki 目录树：逐层懒加载。
 *
 * 大库有几千页，一次性拉平整棵树必然卡；folders 接口本来就按 parent_id 分层返回，
 * 并给了**递归**的 page_count 与 has_children，正好支持「展开才加载」。
 * 每个目录展开后先列子目录、再列该目录直属的页面。
 */

const TYPE_ICON: Record<string, string> = {
  entity: '◆',
  concept: '●',
  synthesis: '▲',
  comparison: '◇',
  summary: '▤',
};

interface TreeNodeState {
  loading: boolean;
  folders: WikiFolder[];
  pages: WikiPageBrief[];
  pageTotal: number;
  loaded: boolean;
}

interface WikiTreeProps {
  kbId: string;
  /** 逗号分隔的页面类型，决定这棵树统计与展示哪些页 */
  pageTypes: string;
  activeSlug: string | null;
  onSelectPage: (slug: string) => void;
}

export function WikiTree({ kbId, pageTypes, activeSlug, onSelectPage }: WikiTreeProps) {
  const [roots, setRoots] = useState<WikiFolder[] | null>(null);
  const [rootLoading, setRootLoading] = useState(true);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [nodes, setNodes] = useState<Record<string, TreeNodeState>>({});
  // 目录 id → 从根到它的名称路径，用于按 category_path 取该层的页面
  const [paths, setPaths] = useState<Record<string, string[]>>({});

  // 换库/换分类由调用方通过 key 重挂载本组件（见 WikiPanel），所以这里只管加载
  // 根层，不需要在 effect 里把一堆 state 重置回去。
  useEffect(() => {
    let alive = true;
    void getWikiFolders(kbId, '', pageTypes)
      .then((list) => {
        if (!alive) return;
        setRoots(list);
        setPaths(Object.fromEntries(list.map((f) => [f.id, [f.name]])));
      })
      .catch(() => alive && setRoots([]))
      .finally(() => alive && setRootLoading(false));
    return () => {
      alive = false;
    };
  }, [kbId, pageTypes]);

  const loadNode = useCallback(
    async (folder: WikiFolder, path: string[]) => {
      // 保留上一次的数据、只把 loading 打开，避免重新展开时先闪一下空态
      setNodes((prev) => {
        const before = prev[folder.id];
        return {
          ...prev,
          [folder.id]: {
            folders: before?.folders || [],
            pages: before?.pages || [],
            pageTotal: before?.pageTotal || 0,
            loaded: before?.loaded || false,
            loading: true,
          },
        };
      });
      try {
        // 子目录与本层页面互不依赖，并行取
        const [folders, pageResult] = await Promise.all([
          folder.has_children ? getWikiFolders(kbId, folder.id, pageTypes) : Promise.resolve([]),
          getWikiPages(kbId, {
            pageType: pageTypes,
            categoryPath: path.join('/'),
            categoryDepth: path.length,
            pageSize: 100,
          }),
        ]);
        setPaths((prev) => ({
          ...prev,
          ...Object.fromEntries(folders.map((f) => [f.id, [...path, f.name]])),
        }));
        setNodes((prev) => ({
          ...prev,
          [folder.id]: {
            loading: false,
            loaded: true,
            folders,
            pages: pageResult.pages,
            pageTotal: pageResult.total,
          },
        }));
      } catch {
        setNodes((prev) => ({
          ...prev,
          [folder.id]: { loading: false, loaded: true, folders: [], pages: [], pageTotal: 0 },
        }));
      }
    },
    [kbId, pageTypes],
  );

  const toggle = useCallback(
    (folder: WikiFolder, path: string[]) => {
      // 取数放在 updater 外面：state updater 必须是纯函数，React 在严格模式下会
      // 调用两次，把副作用写进去会漏跑或重复跑（表现就是点了没反应）。
      const willOpen = !expanded.has(folder.id);
      setExpanded((prev) => {
        const next = new Set(prev);
        if (willOpen) next.add(folder.id);
        else next.delete(folder.id);
        return next;
      });
      if (willOpen && !nodes[folder.id]?.loaded) void loadNode(folder, path);
    },
    [expanded, nodes, loadNode],
  );

  const renderFolder = (folder: WikiFolder, depth: number): React.ReactNode => {
    const path = paths[folder.id] || [folder.name];
    const isOpen = expanded.has(folder.id);
    const node = nodes[folder.id];
    return (
      <div key={folder.id} className="jx-wikiTreeBranch">
        <button
          type="button"
          className="jx-wikiTreeFolder"
          style={{ paddingLeft: 8 + depth * 14 }}
          onClick={() => toggle(folder, path)}
          aria-expanded={isOpen}
        >
          <span className="jx-wikiTreeCaret">
            {isOpen ? <DownOutlined /> : <RightOutlined />}
          </span>
          <FolderOutlined className="jx-wikiTreeFolderIcon" />
          <span className="jx-wikiTreeFolderName" title={folder.name}>
            {folder.name}
          </span>
          <span className="jx-wikiTreeCount">{folder.page_count}</span>
        </button>

        {isOpen && (
          <div className="jx-wikiTreeChildren">
            {node?.loading ? (
              <div className="jx-wikiTreeLoading" style={{ paddingLeft: 22 + depth * 14 }}>
                <Spin size="small" />
              </div>
            ) : (
              <>
                {(node?.folders || []).map((child) => renderFolder(child, depth + 1))}
                {(node?.pages || []).map((page) => (
                  <button
                    key={page.slug}
                    type="button"
                    className={`jx-wikiTreePage${activeSlug === page.slug ? ' is-active' : ''}`}
                    style={{ paddingLeft: 22 + depth * 14 }}
                    onClick={() => onSelectPage(page.slug)}
                    title={page.title}
                  >
                    <span className={`jx-wikiTreePageIcon jx-wikiTypeTag--${page.page_type}`}>
                      {TYPE_ICON[page.page_type] || '·'}
                    </span>
                    <span className="jx-wikiTreePageName">{page.title}</span>
                  </button>
                ))}
                {node?.loaded && !node.folders.length && !node.pages.length && (
                  <div className="jx-wikiTreeEmpty" style={{ paddingLeft: 22 + depth * 14 }}>
                    该目录下暂无内容
                  </div>
                )}
                {node?.loaded && node.pageTotal > node.pages.length && (
                  <div className="jx-wikiTreeEmpty" style={{ paddingLeft: 22 + depth * 14 }}>
                    仅显示前 {node.pages.length} / {node.pageTotal} 页，用搜索精确定位
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>
    );
  };

  if (rootLoading) {
    return (
      <div className="jx-wikiTreeLoading">
        <Spin size="small" />
      </div>
    );
  }

  if (!roots?.length) {
    return (
      <div className="jx-wikiTreeEmpty">
        <FileTextOutlined /> 该分类下没有目录
      </div>
    );
  }

  return <div className="jx-wikiTree">{roots.map((folder) => renderFolder(folder, 0))}</div>;
}

export default WikiTree;
