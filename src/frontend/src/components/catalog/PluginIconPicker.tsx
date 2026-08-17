import { useState } from 'react';
import { Popover, Button, Upload, message } from 'antd';
import type { UploadProps } from 'antd';
import { AppstoreOutlined, UploadOutlined, UndoOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { APP_ICON_LIBRARY, PLUGIN_ICON_LIBRARY } from '../../utils/iconLibrary';

const MAX_UPLOAD_BYTES = 80 * 1024; // raw image cap; ~107KB after base64, < the backend's 200KB cap

/** Plugin avatar: built-in library path / data-URI / URL; falls back to the generic plugin glyph. */
export function PluginAvatar({ icon, size = 36 }: { icon?: string | null; size?: number }) {
  const value = String(icon || '').trim();
  if (value) {
    return (
      <img src={value} alt="" width={size} height={size}
        style={{ borderRadius: 8, objectFit: 'contain', display: 'block', background: 'var(--color-bg-container)' }} />
    );
  }
  return (
    <div className="jx-mcp-iconWrap jx-mcp-iconFallback" style={{ width: size, height: size }}>
      <AppstoreOutlined style={{ color: 'var(--color-tint-purple)' }} />
    </div>
  );
}

// Plugin icon picker: pick from the built-in SVG library or upload a custom image
// (stored inline as a data-URI). No URL typing — icons are chosen, not pasted.
// Implements the antd Form.Item value/onChange contract.
export function PluginIconPicker({
  value, onChange,
}: { value?: string; onChange?: (icon: string) => void }) {
  const [open, setOpen] = useState(false);

  const pick = (icon: string) => {
    onChange?.(icon);
    setOpen(false);
  };

  const beforeUpload: UploadProps['beforeUpload'] = (file) => {
    const okType = /^image\/(svg\+xml|png|jpeg|webp)$/.test(file.type);
    if (!okType) {
      message.error(t('仅支持 SVG / PNG / JPG / WebP 图标'));
      return Upload.LIST_IGNORE;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      message.error(t('图标过大，请控制在 {n}KB 以内', { n: MAX_UPLOAD_BYTES / 1024 }));
      return Upload.LIST_IGNORE;
    }
    const reader = new FileReader();
    reader.onload = () => {
      pick(String(reader.result || ''));
      message.success(t('图标已选用'));
    };
    reader.onerror = () => message.error(t('读取图标失败'));
    reader.readAsDataURL(file);
    return Upload.LIST_IGNORE; // save inline as a data-URI, no server upload round-trip
  };

  const section = (label: string, icons: string[]) => (
    <>
      <div style={{ fontSize: 12, color: '#8C8C8C', margin: '6px 2px 4px' }}>{label}</div>
      <div className="jx-iconPicker-grid">
        {icons.map((url) => (
          <button key={url} type="button" title={url.split('/').pop()}
            className={`jx-iconPicker-cell${value === url ? ' active' : ''}`}
            onClick={() => pick(url)}>
            <img src={url} alt="" width={30} height={30} style={{ objectFit: 'contain' }} />
          </button>
        ))}
      </div>
    </>
  );

  const content = (
    <div className="jx-iconPicker" style={{ width: 320 }}>
      <div style={{ maxHeight: 300, overflowY: 'auto' }}>
        {section(t('插件图标'), PLUGIN_ICON_LIBRARY)}
        {section(t('应用图标'), APP_ICON_LIBRARY)}
      </div>
      <div className="jx-iconPicker-actions">
        <Upload accept="image/svg+xml,image/png,image/jpeg,image/webp" showUploadList={false} beforeUpload={beforeUpload}>
          <Button size="small" icon={<UploadOutlined />}>{t('上传自定义')}</Button>
        </Upload>
        <Button size="small" type="text" icon={<UndoOutlined />} onClick={() => pick('')}>
          {t('恢复默认')}
        </Button>
      </div>
    </div>
  );

  return (
    <Popover content={content} title={t('选择插件图标')} trigger="click" open={open}
      onOpenChange={setOpen} placement="bottomLeft">
      <button type="button" className="jx-iconPicker-trigger">
        <PluginAvatar icon={value} size={48} />
        <span className="jx-iconPicker-triggerHint">{t('选择图标')}</span>
      </button>
    </Popover>
  );
}
