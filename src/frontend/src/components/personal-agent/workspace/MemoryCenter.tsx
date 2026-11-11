import { useCallback, useEffect, useState } from 'react';
import { Modal, Tabs } from 'antd';
import { personalApi } from '../api';
import { t } from '../../../i18n';
import { MemoryWiki } from './MemoryWiki';
import { MemoryChanges, type ChangeFilter } from './MemoryChanges';
import { PersonalDreaming } from './PersonalDreaming';
import { fileLabel } from './memoryLabels';
import './memory-panel.css';

export type MemoryTab = 'wiki' | 'changes' | 'dreaming';

interface MemoryCenterProps {
  open: boolean;
  tab: MemoryTab;
  onTab: (tab: MemoryTab) => void;
  onClose: () => void;
}

/** The owner's view of the agent's memory: the wiki, every change to it, and Dreaming. */
export function MemoryCenter({ open, tab, onTab, onClose }: MemoryCenterProps) {
  const [selected, setSelected] = useState<string | null>(null);
  const [filter, setFilter] = useState<ChangeFilter>({});
  const [pageOptions, setPageOptions] = useState<{ value: string; label: string }[]>([]);
  // Remount the wiki after a restore or a Dreaming run so it reads the new pages.
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    if (!open || tab !== 'changes') return;
    const controller = new AbortController();
    personalApi.memoryPages(controller.signal).then((data) => {
      const pages = (data.pages ?? []).map((p) => ({ value: p.path, label: p.name }));
      const files = [data.index_path, '/workspace/USER.md', '/workspace/MEMORY.md'].filter((p): p is string => !!p)
        .map((p) => ({ value: p, label: fileLabel(p) }));
      setPageOptions([...pages, ...files]);
    }).catch(() => { /* the filter still works by typing nothing; the list itself shows load errors */ });
    return () => controller.abort();
  }, [open, tab, generation]);

  const showHistory = useCallback((path: string) => { setFilter({ path }); onTab('changes'); }, [onTab]);
  const showSource = useCallback((id: string) => { setFilter({ source_id: id }); onTab('changes'); }, [onTab]);
  const changed = useCallback(() => setGeneration((n) => n + 1), []);

  return <Modal open={open} onCancel={onClose} footer={null} title={t('记忆')} width="min(1120px, 96vw)"
    className="pa-mem-modal" destroyOnHidden>
    <Tabs activeKey={tab} onChange={(key) => onTab(key as MemoryTab)} items={[
      { key: 'wiki', label: t('记忆页面'), children: <MemoryWiki key={generation} selected={selected} onSelect={setSelected}
        onHistory={showHistory} onSource={showSource} /> },
      { key: 'changes', label: t('修改记录'), children: <MemoryChanges key={generation} filter={filter} onFilter={setFilter} pageOptions={pageOptions} /> },
      { key: 'dreaming', label: 'Dreaming', children: <PersonalDreaming onChanged={changed} /> },
    ]} />
  </Modal>;
}
