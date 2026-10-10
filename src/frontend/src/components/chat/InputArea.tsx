import React, { useEffect, useRef, useState } from 'react';
import { message } from 'antd';
import { AnimatePresence, motion } from 'motion/react';
import { DUR, EASE } from '../../utils/motionTokens';
import type { ReferencableChat } from '../../types';
import { AgentMentionPopup } from '../agent';
import { SkillSlashPopup } from './SkillSlashPopup';
import LoopPlanBar from '../loop/LoopPlanBar';
import { useFileDropZone } from '../../hooks/useFileDropZone';
import { CHAT_REFERENCE_MIME } from '../../utils/constants';
import { DropOverlay } from '../common/DropOverlay';
import DeploymentSwitcher from './DeploymentSwitcher';
import ChatModeSwitch from './ChatModeSwitch';
import { QueuedMessageCard } from './QueuedMessageCard';
import { extractClipboardImageFiles } from '../../utils/clipboardFiles';
import { hasChatInvocation } from '../../utils/chatInvocation';
import { t } from '../../i18n';
import { richEditor, pastePlainText, insertClipboardContent } from './composerRichText';
import { moveCaretToEnd } from './composerEditorDom';
import { createComposerKeyHandler } from './composerKeyboard';
import { ComposerAttachments } from './ComposerAttachments';
import { ComposerToolbar } from './ComposerToolbar';
import { useComposerState } from './useComposerState';
import { useComposerSuggestions } from './useComposerSuggestions';
import { useComposerEditor } from './useComposerEditor';
import type { ComposerOptions, InputAreaProps } from './composerTypes';

export function InputArea({
  inputRef, fileInputRef, send, abort, activateQueuedMessage, discardQueuedMessage, continueLoop, handleFileSelect, removeFile,
  placeholder = t('请输入你的问题，按Enter发送，Shift+Enter换行'),
  mobilePlaceholder,
  disableMention = false,
}: InputAreaProps) {

  const options: ComposerOptions = {
    disableMention, abort,
  };
  const state = useComposerState(options);
  const selectFiles: typeof handleFileSelect = (event, ref) => handleFileSelect(event, ref, state.draftKey);
  const removeDraftFile = (index: number) => removeFile(index, state.draftKey);
  const suggestions = useComposerSuggestions(state, options);
  const editor = useComposerEditor(state, suggestions, { inputRef, fileInputRef, send });
  const {
    input, activeMention, activeSkill, activePlugin, activeConnector, activeCommand,
    referencedChats, uploadedFiles, importedSpaceFiles, queuedMessages,
    currentChatId, activeRuns, sending, updateQueuedMessage, quotedFollowUp, setQuotedFollowUp,
    planMode, imageInputRef,
  } = state;
  const {
    slashVisible, slashEntries, sIdx, mentionVisible, mentionCandidates,
    mIdx, mentionScreen, backToMentionRoot, setMentionVisible, setMIdx,
    setSlashVisible, setSIdx,
  } = suggestions;
  const {
    composingRef, onSlashEntrySelect, onMentionCandidateSelect, editorHostRef, editorRef, syncText,
    isComposing, applyChatReference, setIsComposing,
  } = editor;
  const onKeyDown = createComposerKeyHandler(state, suggestions, editor);

  // 引用会话同样是"框里已经有内容"的一种：chip 不产生文本，光看 input 会误判成空，
  // 占位文字就会压在 chip 和后面输入的字上（其余四类早就在这条判断里了）。
  const showPlaceholder = !input.trim() && !activeMention && !activeSkill && !activePlugin
    && !activeConnector && !activeCommand && referencedChats.length === 0 && !isComposing;

  // 命令 chip 自己就是一条完整消息（技能 / 插件 chip 只是修饰，还得再打字），所以框里只有
  // 一个命令 chip 时发送按钮必须是亮的，否则用户只能靠回车才发得出去。
  const composerHasContent = !!input.trim() || !!activeCommand;

  const hasAttachments = uploadedFiles.length > 0 || importedSpaceFiles.length > 0;
  const queuedMessage = queuedMessages[currentChatId];
  const canSteerQueued = !!activeRuns[currentChatId]?.runId
    && !hasAttachments
    && !hasChatInvocation(queuedMessage?.invocation);

  // A terminal run can race with the steer response. Never leave the card in
  // an impossible "waiting for a tool boundary" state once this chat is idle.
  useEffect(() => {
    if (!sending && queuedMessage?.status === 'steering' && !queuedMessage.targetRunId) {
      updateQueuedMessage(currentChatId, (current) => ({
        ...current,
        status: 'queued',
      }));
    }
  }, [currentChatId, queuedMessage?.status, queuedMessage?.targetRunId, sending, updateQueuedMessage]);

  const showStopButton = sending && !composerHasContent;

  // 拖文件到输入区直接作为附件上传，复用点击"浏览"的同一条 handleFileSelect 管线
  // （它只读 e.target.files，合成一个最小 change 事件即可）。
  const { dragActive, dropZoneProps } = useFileDropZone(true, (files) => {
    selectFiles(
      { target: { files } } as unknown as React.ChangeEvent<HTMLInputElement>,
      fileInputRef,
    );
  });

  // 从侧边栏把会话拖进输入框 = 引用它。与文件拖放共用同一个落区：按拖拽携带的类型
  // 分流，会话走引用、文件走附件，侧边栏内部的排序拖拽两者都不认。
  const [chatDragActive, setChatDragActive] = useState(false);
  const chatDragDepth = useRef(0);
  const hasChatPayload = (e: React.DragEvent) =>
    Array.from(e.dataTransfer?.types ?? []).includes(CHAT_REFERENCE_MIME);

  const dropProps = {
    onDragEnter: (e: React.DragEvent) => {
      if (!hasChatPayload(e)) { dropZoneProps.onDragEnter(e); return; }
      e.preventDefault();
      chatDragDepth.current += 1;
      setChatDragActive(true);
    },
    onDragOver: (e: React.DragEvent) => {
      if (!hasChatPayload(e)) { dropZoneProps.onDragOver(e); return; }
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    },
    onDragLeave: (e: React.DragEvent) => {
      if (!hasChatPayload(e)) { dropZoneProps.onDragLeave(e); return; }
      chatDragDepth.current = Math.max(0, chatDragDepth.current - 1);
      if (chatDragDepth.current === 0) setChatDragActive(false);
    },
    onDrop: (e: React.DragEvent) => {
      if (!hasChatPayload(e)) { dropZoneProps.onDrop(e); return; }
      e.preventDefault();
      chatDragDepth.current = 0;
      setChatDragActive(false);
      let dropped: ReferencableChat | null = null;
      try {
        dropped = JSON.parse(e.dataTransfer.getData(CHAT_REFERENCE_MIME)) as ReferencableChat;
      } catch { dropped = null; }
      if (!dropped?.chat_id) return;
      if (dropped.chat_id === currentChatId) {
        void message.info(t('当前会话的内容已经在上下文里，无需引用'));
        return;
      }
      if (referencedChats.some((c) => c.chat_id === dropped.chat_id)) return;
      const ed = editorRef.current;
      if (!ed) return;
      ed.focus();
      moveCaretToEnd(ed);
      applyChatReference(dropped);
    },
  };

  return (
    <div className="jx-inputArea" {...dropProps}>
      <DropOverlay active={dragActive} hint={t('松开即可添加为附件')} className="jx-inputArea-dropOverlay" iconSize={20} />
      <DropOverlay active={chatDragActive} hint={t('松开即可引用这段会话')} className="jx-inputArea-dropOverlay" iconSize={20} />
      <LoopPlanBar onContinue={continueLoop} />
      <AnimatePresence initial={false}>
        {queuedMessage && (
          <motion.div
            key={queuedMessage.id}
            initial={{ opacity: 0, y: 8, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 4, scale: 0.98 }}
            transition={{ duration: DUR.fast, ease: EASE.brandOut }}
          >
            <QueuedMessageCard
              queued={queuedMessage}
              running={sending}
              canSteer={canSteerQueued}
              onSteer={() => { void activateQueuedMessage?.(currentChatId); }}
              onDelete={() => { void discardQueuedMessage?.(currentChatId); }}
              onEdit={(content) => updateQueuedMessage(currentChatId, (current) => ({
                ...current,
                content,
              }))}
            />
          </motion.div>
        )}
      </AnimatePresence>
      <ComposerAttachments draftKey={state.draftKey} currentChatId={currentChatId} removeFile={removeDraftFile} />
      {quotedFollowUp && (
        <div className="jx-inputQuote">
          <div className="jx-inputQuoteBadge">{t('追问引用')}</div>
          <div className="jx-inputQuoteText" title={quotedFollowUp.text}>{quotedFollowUp.text}</div>
          <button type="button" className="jx-inputQuoteRemove" onClick={() => setQuotedFollowUp(null)} aria-label={t('移除引用')}>×</button>
        </div>
      )}
      {/* 对话框上方独立一行：标准/极速二选一（常驻）+ 运行位置胶囊（仅桌面双模式）。
          极速与否决定整段对话的工具面和提示词，值得摆在框外常驻可见，而不是藏进下拉。 */}
      <div className="jx-runTargetRow">
        <ChatModeSwitch />
        <DeploymentSwitcher />
      </div>
      <div className={`jx-composerWrap${planMode ? ' jx-composerWrap--plan' : ''}`}>
        <AgentMentionPopup
          visible={mentionVisible}
          screen={mentionScreen}
          candidates={mentionCandidates}
          selectedIndex={mIdx}
          onSelect={onMentionCandidateSelect}
          onBack={backToMentionRoot}
          onHover={setMIdx}
        />
        <SkillSlashPopup
          entries={slashEntries}
          visible={slashVisible}
          selectedIndex={sIdx}
          onSelect={onSlashEntrySelect}
          onHover={setSIdx}
        />

        <input ref={fileInputRef} type="file" multiple style={{ display: 'none' }}
          onChange={(e) => selectFiles(e, fileInputRef)} />
        <input ref={imageInputRef} type="file" multiple style={{ display: 'none' }}
          accept="image/png,image/jpeg,image/gif,image/webp,image/bmp,image/svg+xml"
          onChange={(e) => selectFiles(e, imageInputRef)} />

        {/* ContentEditable editor — chips and text live on the same layer */}
        <div
          ref={editorHostRef}
          onCompositionStart={() => { composingRef.current = true; setIsComposing(true); }}
          onCompositionEnd={() => { composingRef.current = false; setIsComposing(false); queueMicrotask(syncText); }}
          onKeyDownCapture={(e) => {
            onKeyDown(e);
            if (e.key.toLowerCase() === 'v' && (e.ctrlKey || e.metaKey) && e.shiftKey) {
              editorHostRef.current?.setAttribute('data-plain-paste', 'true');
              window.setTimeout(() => editorHostRef.current?.removeAttribute('data-plain-paste'), 1000);
            }
          }}
          onPasteCapture={(e) => {
            const host = editorHostRef.current;
            const plain = host?.getAttribute('data-plain-paste') === 'true';
            host?.removeAttribute('data-plain-paste');
            const rich = editorRef.current && richEditor(editorRef.current);
            if (!rich) return;
            if (plain) {
              e.preventDefault();
              e.stopPropagation();
              pastePlainText(rich, e.clipboardData.getData('text/plain'));
              return;
            }
            const images = extractClipboardImageFiles(e.clipboardData);
            if (images.length) {
              e.preventDefault();
              e.stopPropagation();
              selectFiles(
                { target: { files: images } } as unknown as React.ChangeEvent<HTMLInputElement>,
                imageInputRef,
              );
              insertClipboardContent(rich, e.clipboardData);
            }
          }}
          onBlur={() => { setTimeout(() => { setMentionVisible(false); setSlashVisible(false); }, 200); }}
        />
        {showPlaceholder && (
          <div
            className="jx-composerPlaceholder"
            data-placeholder={placeholder}
            data-mobile-placeholder={mobilePlaceholder || placeholder}
            aria-hidden="true"
          />
        )}


        <ComposerToolbar state={state} editor={editor} options={options} fileInputRef={fileInputRef}
          showStopButton={showStopButton} composerHasContent={composerHasContent} />
      </div>
    </div>
  );
}
