import { useRef, useState } from 'react';
import { Button, Input, Modal, message } from 'antd';
import { UploadOutlined } from '@ant-design/icons';
import { AVATAR_PRESETS, DEFAULT_AGENT_NAME, resizeAvatar } from '../shared/avatarPresets';
import { PersonalAvatar } from '../shared/PersonalAvatar';
import { personalApi, errorText, type PersonalAgent } from '../api';
import { MAX_NAME_LENGTH } from '../shared/limits';
import { t } from '../../../i18n';

interface ProfileModalProps {
  agent: PersonalAgent;
  open: boolean;
  onClose: () => void;
  onSaved: () => Promise<void>;
}

/** Name and avatar of the agent. Uploads are cropped and shrunk in the browser. */
export function ProfileModal({ agent, open, onClose, onSaved }: ProfileModalProps) {
  const [name, setName] = useState(agent.config.name || DEFAULT_AGENT_NAME);
  const [avatar, setAvatar] = useState(agent.config.avatar || 'preset:robot');
  const [saving, setSaving] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  async function upload(file?: File) {
    if (!file) return;
    if (!file.type.startsWith('image/')) { message.error(t('请选择图片文件')); return; }
    try { setAvatar(await resizeAvatar(file)); } catch { message.error(t('无法读取这张图片')); }
    finally { if (input.current) input.current.value = ''; }
  }
  async function save() {
    setSaving(true);
    try { await personalApi.settings({ name: name.trim() || DEFAULT_AGENT_NAME, avatar }); await onSaved(); onClose(); }
    catch (exc) { message.error(errorText(exc)); }
    finally { setSaving(false); }
  }

  return <Modal open={open} title={t('头像与名称')} onCancel={onClose} onOk={() => void save()} okText={t('保存')} cancelText={t('取消')} confirmLoading={saving} width={440} destroyOnHidden>
    <div className="pa-profile">
      <PersonalAvatar avatar={avatar} name={name} size={88} />
      <Input aria-label={t('智能体名称')} value={name} maxLength={MAX_NAME_LENGTH} showCount onChange={event => setName(event.target.value)} placeholder={DEFAULT_AGENT_NAME} />
      <div className="pa-profile-grid" role="radiogroup" aria-label={t('选择头像')}>
        {AVATAR_PRESETS.map(item => <button key={item.id} type="button" role="radio" aria-checked={avatar === 'preset:' + item.id}
          className={'pa-profile-option' + (avatar === 'preset:' + item.id ? ' pa-profile-option--active' : '')} onClick={() => setAvatar('preset:' + item.id)}>
          <PersonalAvatar avatar={'preset:' + item.id} name={item.id} size={44} />
        </button>)}
      </div>
      <input ref={input} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={event => void upload(event.target.files?.[0])} />
      <Button icon={<UploadOutlined />} onClick={() => input.current?.click()}>{t('上传图片')}</Button>
      <p className="pa-hint">{t('图片会裁成正方形并压缩，建议使用清晰的头像。')}</p>
    </div>
  </Modal>;
}
