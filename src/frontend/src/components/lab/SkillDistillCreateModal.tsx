import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Checkbox, Input, Modal, Radio, Spin, message } from 'antd';
import { listSessions } from '../../api';
import type { ChatItem } from '../../types';
import { useSkillDistillStore } from '../../stores';
import { t } from '../../i18n';

interface SkillDistillCreateModalProps {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}

type ScopeMode = 'all' | 'selected';

const PAGE_SIZE = 50;

export function SkillDistillCreateModal({ open, onClose, onCreated }: SkillDistillCreateModalProps) {
  const { createJob, creating } = useSkillDistillStore();
  const [mode, setMode] = useState<ScopeMode>('selected');
  const [hint, setHint] = useState('');
  const [sessions, setSessions] = useState<ChatItem[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const loadSessions = useCallback(async (p: number, append: boolean) => {
    setSessionsLoading(true);
    try {
      const res = await listSessions(p, PAGE_SIZE);
      // Exclude non-standard sessions such as batch execution
      const items = res.items.filter((c) => !c.batchChat);
      setSessions((prev) => (append ? [...prev, ...items] : items));
      setHasMore(res.has_more);
      setPage(p);
    } catch (e) {
      message.error(t('会话列表加载失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      setMode('selected');
      setHint('');
      setSelected(new Set());
      loadSessions(1, false);
    }
  }, [open, loadSessions]);

  const allVisibleChecked = useMemo(
    () => sessions.length > 0 && sessions.every((s) => selected.has(s.id)),
    [sessions, selected],
  );

  const toggleOne = (id: string, checked: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const toggleAllVisible = (checked: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      sessions.forEach((s) => (checked ? next.add(s.id) : next.delete(s.id)));
      return next;
    });
  };

  const handleSubmit = async () => {
    if (mode === 'selected' && selected.size === 0) {
      message.warning(t('请至少选择一个会话'));
      return;
    }
    try {
      await createJob({
        chat_ids: mode === 'all' ? 'all' : Array.from(selected),
        hint: hint.trim() || undefined,
      });
      message.success(t('蒸馏作业已创建，正在后台执行'));
      onCreated();
      onClose();
    } catch (e) {
      message.error(t('创建失败：{msg}', { msg: (e as Error).message }));
    }
  };

  return (
    <Modal
      title={t('新建个人技能蒸馏')}
      open={open}
      onCancel={onClose}
      width={620}
      footer={[
        <Button key="cancel" onClick={onClose}>{t('取消')}</Button>,
        <Button key="ok" type="primary" loading={creating} onClick={handleSubmit}>
          {t('开始蒸馏')}
        </Button>,
      ]}
    >
      <div className="jx-skillDistill-create">
        <div className="jx-skillDistill-create-section">
          <div className="jx-skillDistill-create-label">{t('会话范围')}</div>
          <Radio.Group value={mode} onChange={(e) => setMode(e.target.value)}>
            <Radio value="selected">{t('选择会话')}</Radio>
            <Radio value="all">{t('全部会话（近期优先，最多 100 个）')}</Radio>
          </Radio.Group>
        </div>

        {mode === 'selected' && (
          <div className="jx-skillDistill-create-section">
            <div className="jx-skillDistill-create-label">
              <Checkbox
                checked={allVisibleChecked}
                indeterminate={!allVisibleChecked && selected.size > 0}
                onChange={(e) => toggleAllVisible(e.target.checked)}
              >
                {t('全选当前列表')}
              </Checkbox>
              <span className="jx-skillDistill-create-count">{t('已选 {n} 个', { n: selected.size })}</span>
            </div>
            <div className="jx-skillDistill-sessionList">
              {sessionsLoading && sessions.length === 0 ? (
                <div className="jx-skillDistill-sessionList-loading"><Spin size="small" /></div>
              ) : sessions.length === 0 ? (
                <div className="jx-skillDistill-sessionList-empty">{t('暂无历史会话')}</div>
              ) : (
                sessions.map((s) => (
                  <label key={s.id} className="jx-skillDistill-sessionItem">
                    <Checkbox
                      checked={selected.has(s.id)}
                      onChange={(e) => toggleOne(s.id, e.target.checked)}
                    />
                    <span className="jx-skillDistill-sessionItem-title">{s.title || t('新对话')}</span>
                    <span className="jx-skillDistill-sessionItem-time">
                      {new Date(s.updatedAt).toLocaleDateString('zh-CN')}
                    </span>
                  </label>
                ))
              )}
              {hasMore && (
                <Button
                  size="small"
                  type="link"
                  loading={sessionsLoading}
                  onClick={() => loadSessions(page + 1, true)}
                >
                  {t('加载更多')}
                </Button>
              )}
            </div>
          </div>
        )}

        <div className="jx-skillDistill-create-section">
          <div className="jx-skillDistill-create-label">{t('蒸馏侧重（可选）')}</div>
          <Input.TextArea
            value={hint}
            onChange={(e) => setHint(e.target.value)}
            placeholder={t('例如：侧重提炼我做产业链分析报告的套路')}
            maxLength={500}
            autoSize={{ minRows: 2, maxRows: 4 }}
          />
        </div>
      </div>
    </Modal>
  );
}
