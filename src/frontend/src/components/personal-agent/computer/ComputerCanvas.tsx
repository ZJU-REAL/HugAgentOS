import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Tooltip } from 'antd';
import { AimOutlined, DesktopOutlined, EyeOutlined } from '@ant-design/icons';
import { registerCanvasRenderer } from '../../canvas/customCanvasRegistry';
import { COMPUTER_CANVAS } from './canvasIds';
import { LiveScreen } from './LiveScreen';
import { personalApi, errorText } from '../api';
import { t } from '../../../i18n';

/** The cloud computer screen in the canvas. Watching never blocks the agent; control is explicit. */
export function ComputerCanvas() {
  const [controlToken, setControlToken] = useState('');
  const [notice, setNotice] = useState('');
  const token = useRef('');

  const release = useCallback(async () => {
    const value = token.current;
    token.current = '';
    if (value) await personalApi.releaseControl(value).catch(() => undefined);
  }, []);

  useEffect(() => {
    const leave = () => { void release(); };
    window.addEventListener('pagehide', leave);
    return () => { window.removeEventListener('pagehide', leave); leave(); };
  }, [release]);

  async function toggleControl() {
    if (controlToken) { setControlToken(''); await release(); return; }
    try {
      token.current = (await personalApi.control()).token;
      setControlToken(token.current);
      setNotice('');
    } catch (exc) { setNotice(errorText(exc)); }
  }
  const controlling = !!controlToken;

  return <div className="pa-computer-canvas">
    <div className="pa-computer-bar">
      <span className={'pa-computer-live' + (controlling ? ' pa-computer-live--control' : '')}><i />{controlling ? t('你正在操作，智能体暂停使用这台电脑') : t('实时画面')}</span>
      {notice && <span className="pa-computer-notice" role="status">{notice}</span>}
      <Tooltip title={controlling ? t('结束接管，把电脑交还给智能体') : t('接管后可以直接点击、输入和滚动')}>
        <Button size="small" type={controlling ? 'primary' : 'default'} icon={controlling ? <EyeOutlined /> : <AimOutlined />} onClick={() => void toggleControl()}>
          {controlling ? t('结束接管') : t('接管')}
        </Button>
      </Tooltip>
    </div>
    <LiveScreen controlToken={controlToken} />
  </div>;
}

registerCanvasRenderer(COMPUTER_CANVAS, { component: ComputerCanvas, icon: () => <DesktopOutlined /> });
