import { useCallback, useState } from 'react';
import { message } from 'antd';
import { t } from '../i18n';

interface ChildLike {
  chunk_id: string;
  chunk_index: number;
  content: string;
}

/**
 * "View child chunks" expander: lazily loads and caches the child chunks of a parent chunk on demand, tracking expanded/loading state.
 * Shared by the public library (adminFetch) and the private library (catalog API); the only difference is the `fetchChildren` passed in.
 */
export function useChunkChildrenExpander<T extends ChildLike>(
  fetchChildren: (parentId: string) => Promise<T[]>,
) {
  const [childrenMap, setChildrenMap] = useState<Record<string, T[]>>({});
  const [expandedParents, setExpandedParents] = useState<Set<string>>(new Set());
  const [loadingParents, setLoadingParents] = useState<Set<string>>(new Set());

  // Clear expanded state after list refresh/pagination to avoid mismatches with the new data
  const reset = useCallback(() => {
    setChildrenMap({});
    setExpandedParents(new Set());
  }, []);

  const toggle = useCallback(async (parentId: string) => {
    if (expandedParents.has(parentId)) {
      setExpandedParents((s) => {
        const n = new Set(s);
        n.delete(parentId);
        return n;
      });
      return;
    }
    if (!childrenMap[parentId]) {
      setLoadingParents((s) => new Set(s).add(parentId));
      try {
        const list = await fetchChildren(parentId);
        setChildrenMap((m) => ({ ...m, [parentId]: list }));
      } catch (e) {
        message.error(t('加载子块失败：{msg}', { msg: (e as Error).message }));
        return;
      } finally {
        setLoadingParents((s) => {
          const n = new Set(s);
          n.delete(parentId);
          return n;
        });
      }
    }
    setExpandedParents((s) => new Set(s).add(parentId));
  }, [expandedParents, childrenMap, fetchChildren]);

  return { childrenMap, expandedParents, loadingParents, toggle, reset };
}
