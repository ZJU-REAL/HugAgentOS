import { MessageOutlined } from '@ant-design/icons';

interface InvocationBadgesProps {
  mentionName?: string;
  skillName?: string;
  pluginName?: string;
  connectorName?: string;
  /** 这条消息引用的历史会话，与上面四类共用同一套徽章呈现。 */
  chatRefs?: { chat_id: string; title: string }[];
  className?: string;
}

/** Shared display contract for explicit agent/skill/plugin/connector invocation. */
export function InvocationBadges({
  mentionName,
  skillName,
  pluginName,
  connectorName,
  chatRefs,
  className,
}: InvocationBadgesProps) {
  const badges = [
    mentionName ? { key: 'mention', prefix: '@', name: mentionName, kind: 'mention' } : null,
    skillName ? { key: 'skill', prefix: '/', name: skillName, kind: 'skill' } : null,
    pluginName ? { key: 'plugin', prefix: '/', name: pluginName, kind: 'plugin' } : null,
    connectorName ? { key: 'connector', prefix: 'MCP', name: connectorName, kind: 'connector' } : null,
    ...(chatRefs || []).map((chat) => ({
      key: `chat:${chat.chat_id}`, prefix: '', name: chat.title, kind: 'chat',
    })),
  ].filter((item): item is { key: string; prefix: string; name: string; kind: string } => item !== null);

  if (badges.length === 0) return null;
  return (
    <div className={['jx-msgChipBadges', className].filter(Boolean).join(' ')}>
      {badges.map((item) => (
        <span key={item.key} className={`jx-msgChip jx-msgChip--${item.kind}`}>
          <span className="jx-msgChip-prefix">
            {item.kind === 'chat' ? <MessageOutlined /> : item.prefix}
          </span>{item.name}
        </span>
      ))}
    </div>
  );
}
