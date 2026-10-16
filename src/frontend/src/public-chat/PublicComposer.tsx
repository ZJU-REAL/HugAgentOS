import { useVoiceInput } from '../components/voice/useVoiceInput';
import { VoiceInput } from '../components/voice/VoiceInput';
import { useComposerStore } from '../stores/composerStore';
import { useRef } from 'react';
import type React from 'react';
import { Dropdown } from 'antd';
import { FileImageOutlined, FileTextOutlined } from '@ant-design/icons';
import { t } from '../i18n';
import { useChatStore } from '../stores/chatStore';
import { chatDraftKey, useComposerDraft, readComposer } from '../stores/composerStore';
import { useModelCapabilitiesStore } from '../stores/modelCapabilitiesStore';
import { RichTextField } from '../components/chat/RichTextField';
import { ComposerSendButton } from '../components/chat/ComposerSendButton';
import { ComposerAttachments } from '../components/chat/ComposerAttachments';
import ApprovalPill from '../components/chat/ApprovalPill';
import ModelEffortChip from '../components/chat/ModelEffortChip';
import { IconPlus } from '../components/common/DshIcons';
import { DropOverlay } from '../components/common/DropOverlay';
import { useFileDropZone } from '../hooks/useFileDropZone';
import { extractClipboardImageFiles } from '../utils/clipboardFiles';
import type { InputAreaProps } from '../components/chat/composerTypes';

export function PublicComposer({ inputRef, fileInputRef, handleFileSelect, removeFile, send, abort, placeholder, mobilePlaceholder }: InputAreaProps) {
  const id = useChatStore(s => s.currentChatId);
  const sending = useChatStore(s => s.sendingChatIds.has(id));
  const draftKey = chatDraftKey(id);
  const { input, uploadingFiles, uploadErrors } = useComposerDraft(draftKey);
  const canReadImage = useModelCapabilitiesStore(s => s.capabilities.can_read_image);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const setInput = (value: string) => readComposer(draftKey).setInput(value);
  const epoch = useComposerStore(s => s.epoch);
  const voice = useVoiceInput(`${draftKey}:${epoch}`, inputRef, false);
  const disabled = voice.busy || !input.trim() || uploadingFiles.size > 0 || uploadErrors.size > 0;
  const submit = () => { if (!sending && !disabled) send(); };
  const selectFiles = (files: File[] | FileList) => handleFileSelect(
    { target: { files } } as unknown as React.ChangeEvent<HTMLInputElement>, fileInputRef, draftKey,
  );
  const { dragActive, dropZoneProps } = useFileDropZone(true, selectFiles);
  return <div className="jx-inputArea" {...dropZoneProps}>
    <DropOverlay active={dragActive} hint={t('松开即可添加为附件')} className="jx-inputArea-dropOverlay" iconSize={20} />
    <ComposerAttachments draftKey={draftKey} currentChatId={id} removeFile={index => removeFile(index, draftKey)} />
    <div className="jx-composerWrap">
      <input ref={fileInputRef} type="file" multiple hidden onChange={event => handleFileSelect(event, fileInputRef, draftKey)} />
      <input ref={imageInputRef} type="file" multiple hidden accept="image/png,image/jpeg,image/gif,image/webp,image/bmp,image/svg+xml"
        onChange={event => handleFileSelect(event, imageInputRef, draftKey)} />
      <div className="jx-composerBody" onPasteCapture={event => {
        const images = extractClipboardImageFiles(event.clipboardData);
        if (images.length) { event.preventDefault(); selectFiles(images); }
      }}>
        <RichTextField key={id} value={input} onChange={setInput} onSubmit={submit}
          inputRef={inputRef} active={false} label={t('输入问题或需求')} />
        {!input && <div className="jx-composerPlaceholder" data-placeholder={placeholder}
          data-mobile-placeholder={mobilePlaceholder || placeholder} aria-hidden="true" />}
      </div>
      <div className="jx-composerBar">
        <Dropdown trigger={['click']} placement="topLeft" overlayClassName="jx-attachMenu"
          menu={{ items: [
            { key: 'image', icon: <FileImageOutlined />, label: canReadImage ? t('上传图片') : t('上传图片（当前模型不识图）'),
              onClick: () => imageInputRef.current?.click() },
            { key: 'file', icon: <FileTextOutlined />, label: t('上传文件'), onClick: () => fileInputRef.current?.click() },
          ] }}>
          <button type="button" className="jx-attachBtn" aria-label={t('添加文件')} title={t('添加文件')}>
            <IconPlus size={16} className="jx-attachIcon" />
          </button>
        </Dropdown>
        <ApprovalPill />
        <div className="jx-composerSpacer" style={{ flex: 1 }} />
        <ModelEffortChip />
        <VoiceInput voice={voice} />
        <ComposerSendButton stopping={sending} disabled={disabled} onSend={submit} onStop={abort} />
      </div>
    </div>
  </div>;
}
