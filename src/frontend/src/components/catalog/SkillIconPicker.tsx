import { useState } from 'react';
import { Popover, Button, Upload, message } from 'antd';
import { t } from '../../i18n';
import { UploadOutlined, UndoOutlined } from '@ant-design/icons';
import type { UploadProps } from 'antd';
import { SkillAvatar, PRESET_LIST } from './skillIcons';

const MAX_UPLOAD_BYTES = 80 * 1024; // raw image cap; ~107KB after base64, < the backend's 200KB cap

// Skill icon picker: current icon + a "choose icon" popover (built-in icon grid + upload custom + restore default).
// value: 'preset:<key>' / URL / data-URI / '' (default).
export function SkillIconPicker({
  value, name, seed, onChange,
}: { value?: string; name?: string; seed?: string; onChange: (icon: string) => void }) {
  const [open, setOpen] = useState(false);

  const beforeUpload: UploadProps['beforeUpload'] = (file) => {
    const okType = /^image\/(svg\+xml|png|jpeg|webp|gif)$/.test(file.type);
    if (!okType) {
      message.error(t('仅支持 SVG / PNG / JPG / WebP 图标'));
      return Upload.LIST_IGNORE;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      message.error(`图标过大，请控制在 ${MAX_UPLOAD_BYTES / 1024}KB 以内`);
      return Upload.LIST_IGNORE;
    }
    const reader = new FileReader();
    reader.onload = () => {
      onChange(String(reader.result || ''));
      setOpen(false);
      message.success(t('图标已选用'));
    };
    reader.onerror = () => message.error(t('读取图标失败'));
    reader.readAsDataURL(file);
    return Upload.LIST_IGNORE; // skip the default upload, save inline as a data-URI instead
  };

  const content = (
    <div className="jx-iconPicker">
      <div className="jx-iconPicker-grid">
        {PRESET_LIST.map((p) => {
          const v = `preset:${p.key}`;
          const active = value === v;
          return (
            <button
              key={p.key}
              type="button"
              title={p.label}
              className={`jx-iconPicker-cell${active ? ' active' : ''}`}
              onClick={() => { onChange(v); setOpen(false); }}
            >
              <SkillAvatar icon={v} size={30} />
            </button>
          );
        })}
      </div>
      <div className="jx-iconPicker-actions">
        <Upload accept="image/*" showUploadList={false} beforeUpload={beforeUpload}>
          <Button size="small" icon={<UploadOutlined />}>{t('上传自定义')}</Button>
        </Upload>
        <Button size="small" type="text" icon={<UndoOutlined />} onClick={() => { onChange(''); setOpen(false); }}>
          {t('恢复默认')}
        </Button>
      </div>
    </div>
  );

  return (
    <Popover content={content} title={t('选择技能图标')} trigger="click" open={open} onOpenChange={setOpen} placement="bottomLeft">
      <button type="button" className="jx-iconPicker-trigger">
        <SkillAvatar icon={value} name={name} seed={seed} size={48} />
        <span className="jx-iconPicker-triggerHint">{t('选择图标')}</span>
      </button>
    </Popover>
  );
}
