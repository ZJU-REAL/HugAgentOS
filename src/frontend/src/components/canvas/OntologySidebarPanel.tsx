import { SafetyCertificateOutlined } from '@ant-design/icons';
import { useCallback, useEffect, useRef } from 'react';

import { t } from '../../i18n';
import { useCanvasStore, useChatStore, useUIStore } from '../../stores';
import { OntologyRevisionPanel } from '../chat/OntologyRevisionPanel';

const AUTO_FOLLOW_THRESHOLD = 72;

export function OntologySidebarPanel() {
  const target = useCanvasStore((state) => state.ontologyTarget);
  const dispatchProcessVisible = useUIStore((state) => state.dispatchProcessVisible);
  const message = useChatStore((state) => {
    if (!target) return undefined;
    return state.store.chats[target.chatId]?.messages.find((item) => item.ts === target.messageTs);
  });
  const bodyRef = useRef<HTMLDivElement>(null);
  const userScrolledUpRef = useRef(false);

  const handleScroll = useCallback(() => {
    const body = bodyRef.current;
    if (!body) return;
    const distanceFromBottom = body.scrollHeight - body.scrollTop - body.clientHeight;
    userScrolledUpRef.current = distanceFromBottom > AUTO_FOLLOW_THRESHOLD;
  }, []);

  const revisionContent = message?.ontologyGovernance?.revision?.content;
  const streamFingerprint = [
    message?.lastActivityTs || 0,
    typeof revisionContent === 'string' ? revisionContent.length : 0,
    message?.ontologyGovernance?.review?.status || '',
  ].join(':');

  useEffect(() => {
    userScrolledUpRef.current = false;
  }, [target?.chatId, target?.messageTs]);

  useEffect(() => {
    const body = bodyRef.current;
    if (!body || userScrolledUpRef.current) return;
    const frame = requestAnimationFrame(() => {
      body.scrollTop = body.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [streamFingerprint]);

  return (
    <aside className="jx-rightSidebar jx-rightSidebar--ontology" aria-label={t('本体校验侧边栏')}>
      <div ref={bodyRef} className="jx-rightSidebar-body" onScroll={handleScroll} aria-live="polite">
        {message?.ontologyGovernance && target ? (
          <OntologyRevisionPanel
            governance={message.ontologyGovernance}
            message={message}
            chatId={target.chatId}
            dispatchProcessVisible={dispatchProcessVisible}
          />
        ) : (
          <div className="jx-rightSidebar-empty">
            <SafetyCertificateOutlined />
            <strong>{t('暂无本体校验结果')}</strong>
            <span>{t('本体校验开始后，结果会在这里实时显示。')}</span>
          </div>
        )}
      </div>
    </aside>
  );
}
