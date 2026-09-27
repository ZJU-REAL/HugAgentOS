import { useState } from 'react';
import { LoadingOutlined } from '@ant-design/icons';
import { getToolCallResult } from '../../api';
import { useAuthStore, useChatStore } from '../../stores';
import type { ToolCall } from '../../types';
import { resolveToolDisplayName } from '../../utils/toolMeta';
import { t } from '../../i18n';
import { DataView } from './renderers/DataView';
import type { ToolMessageIdentity } from './ToolMessageContext';

interface ForkedToolCallRowProps {
  tool: ToolCall;
  identity: ToolMessageIdentity;
}

/** Never mount live plugin/subagent controls for a copied result, even if its payload contains IDs. */
export function ForkedToolCallRow({ tool, identity }: ForkedToolCallRowProps) {
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const toolDisplayNames = useChatStore((s) => s.toolDisplayNames);
  const displayName = resolveToolDisplayName(tool, toolDisplayNames);
  const needsFullOutput = tool.outputTruncated && !tool.outputLoaded && tool.id && identity.messageId;

  const loadOutput = async () => {
    if (!needsFullOutput || loading || !identity.messageId || !tool.id) return;
    const userId = useAuthStore.getState().authUser?.user_id;
    setLoading(true);
    setError(false);
    try {
      const result = await getToolCallResult(identity.chatId, identity.messageId, tool.id);
      if (useAuthStore.getState().authUser?.user_id !== userId) return;
      useChatStore.getState().applyToolCallOutput(identity.chatId, identity.messageId, tool.id, result.result);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  };

  return (
    <details className="jx-tcr" onToggle={(event) => {
      const open = event.currentTarget.open;
      setExpanded(open);
      if (open) void loadOutput();
    }}>
      <summary className="jx-tcr-header" title={t('历史工具结果（只读）')}>
        <span className="jx-tcr-label">{displayName}</span>
        {loading && <LoadingOutlined spin />}
      </summary>
      {expanded && <div className="jx-tcr-body">
        {error && <button type="button" onClick={() => void loadOutput()}>
          {t('加载完整结果失败，请重试')}
        </button>}
        {tool.input !== undefined && <DataView value={tool.input} maxHeight={260} />}
        {tool.output !== undefined && <DataView value={tool.output} maxHeight={520} />}
        {tool.subSteps?.length ? <DataView value={tool.subSteps} maxHeight={520} /> : null}
      </div>}
    </details>
  );
}
