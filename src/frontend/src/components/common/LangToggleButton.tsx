import { Button } from 'antd';
import { GlobalOutlined } from '@ant-design/icons';
import { getLang, setLang } from '../../i18n';

/** Chinese/English toggle button (shared by the admin / config console headers). By convention the label shows the native name of the target language. */
export function LangToggleButton() {
  const isEn = getLang() === 'en';
  return (
    <Button
      icon={<GlobalOutlined />}
      onClick={() => setLang(isEn ? 'zh-CN' : 'en')}
      size="small"
    >
      {isEn ? '简体中文' : 'English'}
    </Button>
  );
}
