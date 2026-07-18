import { useEffect, useRef, useState } from 'react';
import { Modal, Input, Tag, Table, Alert, Space, message } from 'antd';
import type { TextAreaRef } from 'antd/es/input/TextArea';
import { useBatchStore } from '../../stores';
import { confirmBatchPlan, cancelBatchPlan } from '../../api';
import { t } from '../../i18n';

const { TextArea } = Input;

const SOURCE_TYPE_LABEL: Record<string, string> = {
  text_list: t('文本枚举'),
  xlsx: t('Excel 行迭代'),
  word_files: t('多份文档'),
};

interface Props {
  /** Hook into the streaming pipeline so cancelling the plan can re-stream
   *  the user's question via ordinary tools (no batch_plan). */
  onCancelResume?: (planId: string, chatId: string) => Promise<void>;
}

/**
 * Modal that opens whenever the SSE stream emits a `batch_confirm` event.
 * The user can review the auto-generated plan items, edit the prompt
 * template, and either confirm execution or cancel.
 */
export function BatchConfirmModal({ onCancelResume }: Props = {}) {
  const pendingId = useBatchStore((s) => s.pendingConfirmPlanId);
  const plans = useBatchStore((s) => s.plans);
  const clearPendingConfirm = useBatchStore((s) => s.clearPendingConfirm);
  const startRun = useBatchStore((s) => s.startRun);
  const connectStream = useBatchStore((s) => s.connectStream);
  const cancel = useBatchStore((s) => s.cancel);

  const meta = pendingId ? plans[pendingId]?.meta : null;
  const [template, setTemplate] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const textAreaRef = useRef<TextAreaRef>(null);

  useEffect(() => {
    if (meta) setTemplate(meta.default_template || '');
  }, [meta?.plan_id]);  // eslint-disable-line react-hooks/exhaustive-deps

  if (!pendingId || !meta) return null;

  const open = !!pendingId;

  const insertPlaceholder = (key: string) => {
    setTemplate((t) => `${t}{${key}}`);
    // Return focus to the template editor so the user can keep typing.
    textAreaRef.current?.focus();
  };

  const handleConfirm = async () => {
    if (!template.trim()) {
      message.warning(t('请填写 prompt 模板'));
      return;
    }
    setSubmitting(true);
    try {
      await confirmBatchPlan(pendingId, { prompt_template: template.trim() });
      startRun(pendingId, template.trim());
      // Pass `meta` explicitly so the store has chat_id even on slow
      // store updates / React-render races. `connectStream` uses it to
      // light up the sidebar pulse for the host chat.
      connectStream(pendingId, meta);
      clearPendingConfirm();
    } catch (e) {
      message.error(t('确认失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancel = async () => {
    // Snapshot what we need BEFORE mutating any store state — closing the
    // modal triggers a re-render that will null out `meta` and `pendingId`.
    const planIdToCancel = pendingId;
    const chatId = meta?.chat_id;

    // Close the modal optimistically.
    cancel(planIdToCancel);

    if (onCancelResume && chatId) {
      message.info(t('已取消批量，正在用普通方式回答…'));
      try {
        await onCancelResume(planIdToCancel, chatId);
      } catch (e) {
        // The hook already surfaces user-facing errors via antd.message.
        // Fall back to plain cancel so the plan is at least marked done.
        try { await cancelBatchPlan(planIdToCancel); } catch { /* ignore */ }
      }
    } else {
      // No streaming hook plumbed through, or no chat context (rare —
      // happens if the plan was created outside of a chat). Just mark
      // the plan cancelled and stop.
      try { await cancelBatchPlan(planIdToCancel); } catch { /* ignore */ }
      message.info(t('已取消批量执行'));
    }
  };

  // ── Build preview table ─────────────────────────────────────────────
  const previewRows = (meta.preview || []).slice(0, 5).map((r, i) => ({
    __key: i,
    ...r,
  }));
  const previewKeys = Array.from(
    previewRows.reduce<Set<string>>((acc, row) => {
      Object.keys(row).forEach((k) => {
        if (k !== '__key') acc.add(k);
      });
      return acc;
    }, new Set()),
  ).slice(0, 5);
  const columns = previewKeys.map((k) => ({
    title: k,
    dataIndex: k,
    key: k,
    ellipsis: true,
    width: 160,
    render: (v: unknown) => {
      const s = typeof v === 'string' ? v : JSON.stringify(v);
      return s.length > 80 ? s.slice(0, 80) + '…' : s;
    },
  }));

  return (
    <Modal
      title={t('批量执行 — 请确认计划')}
      open={open}
      onOk={handleConfirm}
      onCancel={handleCancel}
      okText={t('确认执行（{n} 项）', { n: meta.total })}
      cancelText={t('取消批量，普通问答')}
      confirmLoading={submitting}
      width={760}
      destroyOnHidden
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Alert
          type="info"
          showIcon
          message={t('即将对 {n} 个对象批量执行任务', { n: meta.total })}
          description={
            <span>
              {t('来源：')}<Tag color="blue">{SOURCE_TYPE_LABEL[meta.source_type] || meta.source_type}</Tag>
              {t('执行时将逐条处理，每条失败重试 2 次后跳过。')}
            </span>
          }
        />

        {meta.warnings && meta.warnings.length > 0 && (
          <Alert
            type="warning"
            showIcon
            message={t('数据规模提示')}
            description={
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {meta.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            }
          />
        )}

        <div>
          <div style={{ marginBottom: 6, fontWeight: 500 }}>{t('数据预览（前 {n} 条）', { n: previewRows.length })}</div>
          <Table
            size="small"
            columns={columns}
            dataSource={previewRows}
            rowKey="__key"
            pagination={false}
            scroll={{ x: 'max-content', y: 180 }}
          />
        </div>

        <div>
          <div style={{ marginBottom: 6, fontWeight: 500 }}>
            {t('可用占位符（点击插入到模板）')}
          </div>
          <Space wrap>
            {(meta.placeholder_keys || []).map((k) => (
              <Tag
                key={k}
                color="geekblue"
                className="jx-batch-phTag"
                style={{ cursor: 'pointer', userSelect: 'none' }}
                onClick={() => insertPlaceholder(k)}
              >
                {`{${k}}`}
              </Tag>
            ))}
          </Space>
        </div>

        <div>
          <div style={{ marginBottom: 6, fontWeight: 500 }}>{t('Prompt 模板（每条任务都会按此模板渲染）')}</div>
          <TextArea
            ref={textAreaRef}
            value={template}
            onChange={(e) => setTemplate(e.target.value)}
            rows={6}
            placeholder="例如：请分析公司 {公司名称}，营收 {营收}，给出一句话经营评价。"
          />
        </div>
      </Space>
    </Modal>
  );
}
