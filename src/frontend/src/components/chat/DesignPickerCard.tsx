import { useEffect, useState } from 'react';
import { Image, message } from 'antd';
import { AnimatePresence, motion } from 'motion/react';
import { BgColorsOutlined, CheckCircleFilled } from '@ant-design/icons';
import { EASE } from '../../utils/motionTokens';
import { useChatStore, useUIStore } from '../../stores';
import { submitDesignPick } from '../../api';
import { buildFileUrl } from '../../utils/constants';
import { t } from '../../i18n';

/**
 * Site-build design pick-one-of-three selection card — floats above the input box (same position as FileConfirmBar).
 *
 * Same mechanism as §13 write confirmation: the backend choose_design tool coroutine is right now **suspended** at
 * `await event.wait()` in `_myspace_confirm.pick`, and the original SSE stream has not ended.
 * When the user picks an option (or "let the assistant decide") → an out-of-band POST /file-confirm
 * (decision: choice/skip) wakes the coroutine, and the agent resumes in place with the chosen option.
 */
export function DesignPickerCard() {
  const currentChatId = useChatStore((s) => s.currentChatId);
  const info = useUIStore((s) => s.pendingDesignPick[currentChatId]);
  const setPendingDesignPick = useUIStore((s) => s.setPendingDesignPick);
  const [busy, setBusy] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Reset local state when the selector changes (confirmId changes) or when switching sessions — the component stays permanently mounted,
  // only the inner part renders conditionally on info; without resetting, the previous selection's highlight/disabled state would carry into the new card.
  useEffect(() => {
    setBusy(false);
    setSelectedId(null);
  }, [info?.confirmId]);

  const submit = async (optionId: string | null) => {
    if (busy || !info) return;
    setBusy(true);
    setSelectedId(optionId);
    try {
      const res = await submitDesignPick(currentChatId, info.confirmId, optionId);
      if (res.stale) {
        setPendingDesignPick(currentChatId, null);
        if (res.chat_interrupted) {
          message.warning({
            content: res.message || t('上次会话因服务端重启未完成，请重新发送您的消息'),
            duration: 6,
          });
        } else {
          message.info(t('该选择已过期（可能超时），助手将自行选择方案继续。'));
        }
        return;
      }
      // Selection succeeded: collapse the card, the agent continues from the original SSE stream (subsequent tool_result carries the chosen option)
      setPendingDesignPick(currentChatId, null);
      if (optionId) {
        const picked = info.options.find((o) => o.id === optionId);
        message.success(t('已选择「{name}」方案，助手继续搭建中…', { name: picked?.title || optionId }));
      } else {
        message.info(t('已交由助手决定设计方案。'));
      }
    } catch (e: unknown) {
      // Real failure (network, etc.): keep the selection card so the user can retry
      setSelectedId(null);
      message.error(t('操作失败：{msg}', { msg: e instanceof Error ? e.message : String(e) }));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AnimatePresence initial={false}>
      {info && (
        <motion.div
          key="designPickerCard"
          exit={{ opacity: 0, y: 8, transition: { duration: 0.15, ease: EASE.exit } }}
        >
          <div className="jx-designPicker" role="dialog" aria-label={t('设计方案选择')}>
            <div className="jx-designPicker-header">
              <span className="jx-designPicker-icon" aria-hidden="true">
                <BgColorsOutlined />
              </span>
              <div className="jx-designPicker-question">
                {info.question || t('请选择一个设计方案')}
              </div>
            </div>
            <div className="jx-designPicker-grid" data-count={info.options.length}>
              {info.options.map((o) => {
                const isSelected = selectedId === o.id;
                return (
                  <div
                    key={o.id}
                    className={
                      'jx-designPicker-option' +
                      (isSelected ? ' jx-designPicker-option--selected' : '') +
                      (busy && !isSelected ? ' jx-designPicker-option--muted' : '')
                    }
                  >
                    <div className="jx-designPicker-thumbWrap">
                      <Image
                        src={`${buildFileUrl(o.imageFileId)}?inline=true`}
                        alt={o.title}
                        className="jx-designPicker-thumb"
                        preview={{ mask: t('放大预览') }}
                      />
                      {isSelected && (
                        <span className="jx-designPicker-check">
                          <CheckCircleFilled />
                        </span>
                      )}
                    </div>
                    <button
                      type="button"
                      className="jx-designPicker-pickBtn"
                      disabled={busy}
                      onClick={() => submit(o.id)}
                    >
                      {o.title}
                    </button>
                    {o.brief && <div className="jx-designPicker-brief">{o.brief}</div>}
                  </div>
                );
              })}
            </div>
            <div className="jx-designPicker-footer">
              <span className="jx-designPicker-hint">
                {t('点击图片可放大对比；选定后助手将按该方案继续搭建。')}
              </span>
              <button
                type="button"
                className="jx-designPicker-skipBtn"
                disabled={busy}
                onClick={() => submit(null)}
              >
                {t('让助手决定')}
              </button>
            </div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
