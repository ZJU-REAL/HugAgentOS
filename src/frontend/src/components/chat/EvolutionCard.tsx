import {
  CheckCircleFilled,
  DeleteOutlined,
  EditOutlined,
  ExclamationCircleFilled,
} from '@ant-design/icons';
import { motion } from 'motion/react';
import { useState } from 'react';

import {
  deleteGraphRelation,
  deleteMemory,
  deleteProfileField,
  updateMemory,
  updateProfileField,
} from '../../api';
import { useEvolutionSettlement, applySettlement } from '../../hooks/useEvolutionSettlement';
import { t } from '../../i18n';
import type { EvolutionMemoryEntry, EvolutionSummary } from '../../types';

interface EvolutionCardProps {
  summary: EvolutionSummary;
  chatId: string;
  messageId?: string;
}

/**
 * What this turn remembered — the only thing a turn can honestly claim to have
 * changed.
 *
 * Skills and orchestration policies are distilled in batch and reviewed in the
 * console; a conversation neither creates nor promotes one, and the skills it
 * used were already installed. Reporting those here announced an evolution on
 * every turn that opened a skill, which is how the card taught users to ignore
 * it.
 *
 * Every row is editable and deletable in place. A memory the user cannot fix at
 * the moment they see it wrong is one they stop trusting, and their only other
 * option — wiping the layer — is not a correction.
 */
const LAYER_LABEL: Record<string, string> = {
  L1: '用户画像',
  L2: '做法沉淀',
  L3: '图谱关系',
};

function MemoryRow({
  entry,
  messageId,
  onChanged,
  onRemoved,
}: {
  entry: EvolutionMemoryEntry;
  messageId?: string;
  onChanged: (text: string) => void;
  onRemoved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(entry.text);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const isProfile = entry.layer === 'L1';
  const isGraph = entry.layer === 'L3';

  const save = async () => {
    const text = draft.trim();
    if (!text || text === entry.text) {
      setEditing(false);
      setDraft(entry.text);
      return;
    }
    setBusy(true);
    setError('');
    try {
      if (isProfile) {
        await updateProfileField(entry.handle, text, messageId);
      } else if (isGraph) {
        // Graph relations are triples. They can be removed and re-learned, but
        // free-text editing would make the relation shape ambiguous.
        return;
      } else {
        await updateMemory(entry.handle, text, messageId);
      }
      onChanged(text);
      setEditing(false);
    } catch {
      setError(t('修改失败，请重试'));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    setError('');
    try {
      if (isProfile) {
        await deleteProfileField(entry.handle, messageId);
      } else if (isGraph) {
        await deleteGraphRelation(entry.handle, messageId);
      } else {
        await deleteMemory(entry.handle, messageId);
      }
      onRemoved();
    } catch {
      setError(t('删除失败，请重试'));
      setBusy(false);
    }
  };

  return (
    <li className={`jx-evolutionCard-entry${busy ? ' is-busy' : ''}`}>
      <span className={`jx-evolutionCard-layer is-${entry.layer.toLowerCase()}`}>
        {t(LAYER_LABEL[entry.layer] ?? entry.layer)}
      </span>

      <div className="jx-evolutionCard-entryBody">
        {editing ? (
          <textarea
            className="jx-evolutionCard-editor"
            value={draft}
            rows={2}
            autoFocus
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void save();
              }
              if (e.key === 'Escape') {
                setEditing(false);
                setDraft(entry.text);
              }
            }}
          />
        ) : (
          <p className="jx-evolutionCard-entryText">{entry.text}</p>
        )}

        {/* The reason is what tells a reader where the rule stops applying, so
            it is shown next to the rule rather than hidden in a detail panel. */}
        {!editing && (entry.why || entry.applies_to) && (
          <p className="jx-evolutionCard-entryMeta">
            {entry.applies_to && <span>{t('适用于')}：{entry.applies_to}</span>}
            {entry.why && <span>{t('原因')}：{entry.why}</span>}
          </p>
        )}

        {error && <p className="jx-evolutionCard-entryError">{error}</p>}
      </div>

      <div className="jx-evolutionCard-entryActions">
        {editing ? (
          <>
            <button type="button" onClick={() => void save()} disabled={busy}>
              {t('保存')}
            </button>
            <button
              type="button"
              onClick={() => {
                setEditing(false);
                setDraft(entry.text);
              }}
              disabled={busy}
            >
              {t('取消')}
            </button>
          </>
        ) : (
          <>
            {!isGraph && (
              <button
                type="button"
                aria-label={t('编辑这条记忆')}
                onClick={() => setEditing(true)}
                disabled={busy}
              >
                <EditOutlined />
              </button>
            )}
            <button
              type="button"
              aria-label={t('删除这条记忆')}
              onClick={() => void remove()}
              disabled={busy}
            >
              <DeleteOutlined />
            </button>
          </>
        )}
      </div>
    </li>
  );
}

export function EvolutionCard({ summary, chatId, messageId }: EvolutionCardProps) {
  // Local overlay on top of the persisted summary, so an edit or delete shows
  // immediately instead of waiting for a refetch.
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [removed, setRemoved] = useState<string[]>([]);

  // Settlement finishes after the stream closed; poll until it lands, then patch
  // the transcript. Nothing renders while it is pending.
  useEvolutionSettlement(messageId ?? summary?.message_id, summary?.state, (settled) => {
    const id = messageId ?? summary?.message_id ?? '';
    if (id) applySettlement(chatId, id, settled);
  });

  if (!summary || summary.state === 'empty' || summary.state === 'pending') return null;

  const isFailed = summary.state === 'failed' || summary.status === 'failed';
  const entries = (summary.entries ?? []).filter((e) => !removed.includes(e.handle));

  if (!isFailed && entries.length === 0) return null;

  return (
    // Appears the way the follow-up questions do: absent until the background
    // settlement lands, then it fades in. No placeholder, no spinner.
    <motion.section
      className={`jx-evolutionCard${isFailed ? ' is-failed' : ''}`}
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.24, ease: 'easeOut' }}
    >
      <header className="jx-evolutionCard-head">
        <span className="jx-evolutionCard-title">
          {isFailed ? <ExclamationCircleFilled /> : <CheckCircleFilled />}
          <span>{isFailed ? t('本轮记忆写入失败') : t('本轮新增记忆')}</span>
        </span>
        {!isFailed && <span className="jx-evolutionCard-gain">+{entries.length}</span>}
      </header>

      {isFailed ? (
        <div className="jx-evolutionCard-error">
          <span>{t('这轮对话的记忆没有写入成功，本轮内容不会被记住。')}</span>
          {summary.error && <small>{summary.error}</small>}
        </div>
      ) : (
        <ul className="jx-evolutionCard-entries">
          {entries.map((entry) => (
            <MemoryRow
              key={entry.handle}
              entry={{ ...entry, text: edits[entry.handle] ?? entry.text }}
              messageId={messageId ?? summary.message_id}
              onChanged={(text) => setEdits((prev) => ({ ...prev, [entry.handle]: text }))}
              onRemoved={() => setRemoved((prev) => [...prev, entry.handle])}
            />
          ))}
        </ul>
      )}
    </motion.section>
  );
}
