import { Button } from 'antd';
import { GlobalOutlined } from '@ant-design/icons';
import { getLang, setLang } from '../../i18n';

interface LangToggleButtonProps {
  /** Class for the text label, so a narrow header can hide it and keep the icon. */
  labelClassName?: string;
}

/** Chinese/English toggle button (shared by the admin / config console headers). By convention the label shows the native name of the target language. */
export function LangToggleButton({ labelClassName }: LangToggleButtonProps = {}) {
  const isEn = getLang() === 'en';
  const label = isEn ? '简体中文' : 'English';
  return (
    <Button
      icon={<GlobalOutlined />}
      onClick={() => setLang(isEn ? 'zh-CN' : 'en')}
      size="small"
      aria-label={label}
    >
      <span className={labelClassName}>{label}</span>
    </Button>
  );
}
