import { useEffect, useRef, useState } from 'react';
import { CloudOutlined, LaptopOutlined } from '@ant-design/icons';

import { isLocalChat, isLocalProject, isRegisteredLocalChat } from '../../api';
import { t } from '../../i18n';
import { useChatStore } from '../../stores';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';

/**
 * 混合架构（桌面双模式）：当前对话的「运行位置」选择器——云端 / 本机。
 *
 * 按对话生效、不重启不登出。反代按 x-hugagent-target 头把该对话的请求路由到本机执行面（useStreaming
 * 读 chat.runTarget / 项目归属打头）。规则：
 *   - 仅双模式（provision_mode==='dual'）显示；纯本机 / 纯云端无需选择。
 *   - 绑定了项目的对话跟随项目归属（本地项目→本机，云端项目→云端），锁定。
 *   - 已开聊的对话锁定（会话已落在某一侧，中途搬家会割裂历史）。
 */
export default function DeploymentSwitcher() {
  const provisionMode = useDeploymentModeStore((s) => s.provisionMode);
  const currentChatId = useChatStore((s) => s.currentChatId);
  const chat = useChatStore((s) => s.store.chats[s.currentChatId]);
  const setChatRunTarget = useChatStore((s) => s.setChatRunTarget);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  if (provisionMode !== 'dual') return null;

  const projectId = chat?.projectId;
  const projectBound = !!projectId;
  const started = !!chat && (chat.messages.length > 0 || isRegisteredLocalChat(chat.id));
  const local = projectBound ? isLocalProject(projectId) : isLocalChat(currentChatId);
  const locked = projectBound || started;
  const lockReason = projectBound ? t('跟随项目归属') : started ? t('对话已开始，运行位置不可更换') : '';

  const pick = (wantLocal: boolean) => {
    setOpen(false);
    if (locked || wantLocal === local) return;
    setChatRunTarget(currentChatId, wantLocal ? 'local' : undefined);
  };

  return (
    <div ref={rootRef} className="jx-deploySwitcher">
      <button
        type="button"
        className="jx-deploySwitcherPill"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={locked ? lockReason : t('选择这段对话在哪里运行')}
      >
        <span className="jx-deploySwitcherIcon">{local ? <LaptopOutlined /> : <CloudOutlined />}</span>
        <span className="jx-deploySwitcherLabel">{local ? t('本机') : t('云端')}</span>
        <img src="/home/arrow-down.svg" alt="" className="jx-modeArrow" />
      </button>

      {open && (
        <div className="jx-deploySwitcherMenu" role="menu">
          <div className="jx-deploySwitcherGroup">{t('这段对话在哪里运行')}</div>
          <button
            type="button"
            className="jx-deploySwitcherItem"
            role="menuitem"
            disabled={locked}
            onClick={() => pick(false)}
          >
            <span className="jx-deploySwitcherItemIcon"><CloudOutlined /></span>
            <span className="jx-deploySwitcherItemBody">
              <span className="jx-deploySwitcherItemTitle">{t('云端')}</span>
              <span className="jx-deploySwitcherItemDesc">{t('会话保存在云端，可用「我的空间」')}</span>
            </span>
            {!local && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon jx-deploySwitcherCheck" />}
          </button>
          <button
            type="button"
            className="jx-deploySwitcherItem"
            role="menuitem"
            disabled={locked}
            onClick={() => pick(true)}
          >
            <span className="jx-deploySwitcherItemIcon"><LaptopOutlined /></span>
            <span className="jx-deploySwitcherItemBody">
              <span className="jx-deploySwitcherItemTitle">{t('本机')}</span>
              <span className="jx-deploySwitcherItemDesc">
                {t('在这台电脑上运行并保存，可操作本机文件')}
              </span>
            </span>
            {local && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon jx-deploySwitcherCheck" />}
          </button>
          {locked && <div className="jx-deploySwitcherGroup">{lockReason}</div>}
        </div>
      )}
    </div>
  );
}
