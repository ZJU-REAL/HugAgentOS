import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeftOutlined,
  ArrowRightOutlined,
  CheckOutlined,
  FolderOpenOutlined,
  OrderedListOutlined,
  PartitionOutlined,
  RobotOutlined,
  SyncOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { AnimatePresence, motion } from 'motion/react';
import { scaleIn } from '../../utils/motionVariants';
import { usePopupFlip } from '../../hooks/usePopupFlip';
import { useAgentStore } from '../../stores/agentStore';
import type { UserAgentItem } from '../../stores/agentStore';
import { t } from '../../i18n';

export type MentionActionId = 'files' | 'agents' | 'plan' | 'batch' | 'workflow' | 'loop';

export interface MentionLauncherAction {
  id: MentionActionId;
  name: string;
  description: string;
  active?: boolean;
}

export type MentionCandidate =
  | { kind: 'action'; action: MentionLauncherAction }
  | { kind: 'agent'; agent: UserAgentItem; description: string };

type MentionScreen = 'root' | 'agents';

interface AgentMentionPopupProps {
  visible: boolean;
  screen: MentionScreen;
  candidates: MentionCandidate[];
  selectedIndex: number;
  onSelect: (candidate: MentionCandidate) => void;
  onBack: () => void;
  onHover: (index: number) => void;
}

const POPUP_MAX_HEIGHT = 320;

function agentDescription(agent: UserAgentItem): string {
  return agent.description.trim() || agent.welcome_message.trim();
}

function matchesAgent(agent: UserAgentItem, query: string): boolean {
  if (!query) return true;
  return agent.name.toLowerCase().includes(query)
    || agentDescription(agent).toLowerCase().includes(query);
}

function actionIcon(id: MentionActionId) {
  if (id === 'files') return <FolderOpenOutlined />;
  if (id === 'agents') return <RobotOutlined />;
  if (id === 'plan') return <OrderedListOutlined />;
  if (id === 'batch') return <ThunderboltOutlined />;
  if (id === 'workflow') return <PartitionOutlined />;
  return <SyncOutlined />;
}

function candidateKey(candidate: MentionCandidate): string {
  return candidate.kind === 'action'
    ? `action-${candidate.action.id}`
    : `agent-${candidate.agent.agent_id}`;
}

function candidateGroup(candidate: MentionCandidate): 'actions' | 'agents' {
  return candidate.kind === 'action' ? 'actions' : 'agents';
}

export function AgentMentionPopup({
  visible,
  screen,
  candidates,
  selectedIndex,
  onSelect,
  onBack,
  onHover,
}: AgentMentionPopupProps) {
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    if (visible && itemRefs.current[selectedIndex]) {
      itemRefs.current[selectedIndex]!.scrollIntoView({ block: 'nearest' });
    }
  }, [selectedIndex, visible]);

  const showPopup = visible && (screen === 'agents' || candidates.length > 0);
  // Insufficient space above (e.g. on the project detail page the input box sits close to the top) → flip to below the cursor's line
  const popupRef = useRef<HTMLDivElement | null>(null);
  const { below: flipBelow, belowTop } = usePopupFlip(popupRef, showPopup, POPUP_MAX_HEIGHT);

  return (
    <AnimatePresence>
      {showPopup && (
        <motion.div
          ref={popupRef}
          className={`jx-mentionPopup${flipBelow ? ' jx-mentionPopup--below' : ''}`}
          style={flipBelow && belowTop != null ? { top: belowTop } : undefined}
          onMouseDown={(e) => e.preventDefault()}
          role="listbox"
          aria-label={t('快捷入口与智能体建议')}
          variants={scaleIn}
          initial="hidden"
          animate="visible"
          exit="exit"
        >
          {screen === 'agents' && (
            <button type="button" className="jx-mentionPopup-back" onClick={onBack}>
              <ArrowLeftOutlined />
              <span>{t('返回快捷入口')}</span>
            </button>
          )}

          {screen === 'agents' && candidates.length === 0 ? (
            <>
              <div className="jx-commandPopup-groupTitle" role="presentation">{t('智能体')}</div>
              <div className="jx-mentionPopup-empty">{t('暂无可用智能体')}</div>
            </>
          ) : candidates.map((candidate, idx) => {
            const group = candidateGroup(candidate);
            const previousGroup = idx > 0 ? candidateGroup(candidates[idx - 1]) : null;
            const showGroup = idx === 0 || group !== previousGroup;
            const isAction = candidate.kind === 'action';
            const name = isAction ? candidate.action.name : candidate.agent.name;
            const description = isAction ? candidate.action.description : candidate.description;
            const actionId = isAction ? candidate.action.id : null;

            return (
              <Fragment key={candidateKey(candidate)}>
                {showGroup && (
                  <div className="jx-commandPopup-groupTitle" role="presentation">
                    {group === 'actions' ? t('快捷入口') : t('智能体')}
                  </div>
                )}
                <button
                  ref={(el) => { itemRefs.current[idx] = el; }}
                  type="button"
                  role="option"
                  aria-selected={idx === selectedIndex}
                  className={`jx-mentionPopup-item${idx === selectedIndex ? ' active' : ''}`}
                  onMouseEnter={() => onHover(idx)}
                  onClick={() => onSelect(candidate)}
                >
                  {isAction ? (
                    <span className={`jx-mentionPopup-icon jx-mentionPopup-icon--${actionId}`}>
                      {actionIcon(candidate.action.id)}
                    </span>
                  ) : (
                    <span className="jx-mentionPopup-at">@</span>
                  )}
                  <span className="jx-mentionPopup-name" title={name}>{name}</span>
                  {description && (
                    <span className="jx-commandPopup-description" title={description}>{description}</span>
                  )}
                  {isAction && candidate.action.active && (
                    <span className="jx-mentionPopup-state" title={t('已开启')}>
                      <CheckOutlined />
                    </span>
                  )}
                  {isAction && actionId === 'agents' && !candidate.action.active && (
                    <ArrowRightOutlined className="jx-mentionPopup-state" aria-hidden="true" />
                  )}
                </button>
              </Fragment>
            );
          })}
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/**
 * Hook: @ launcher visibility, first/second-level candidates, and keyboard navigation.
 */
export function useAgentMention(input: string, actions: MentionLauncherAction[]) {
  const { agents, fetchAgents } = useAgentStore();
  const [mentionVisible, setMentionVisible] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [screen, setScreen] = useState<MentionScreen>('root');

  const query = useMemo(() => {
    if (!mentionVisible) return '';
    const lastAt = input.lastIndexOf('@');
    return lastAt === -1 ? '' : input.slice(lastAt + 1).toLowerCase();
  }, [input, mentionVisible]);

  const canMentionAgents = actions.some((action) => action.id === 'agents');

  useEffect(() => {
    if (mentionVisible && canMentionAgents && agents.length === 0) void fetchAgents();
  }, [agents.length, canMentionAgents, fetchAgents, mentionVisible]);

  const candidates = useMemo<MentionCandidate[]>(() => {
    const enabledAgents = canMentionAgents
      ? agents.filter((agent) => agent.is_enabled && matchesAgent(agent, query))
      : [];
    const agentCandidates = enabledAgents.map((agent) => ({
      kind: 'agent' as const,
      agent,
      description: agentDescription(agent),
    }));

    if (screen === 'agents') return agentCandidates;

    const actionCandidates = actions
      .filter((action) => !query
        || action.id.includes(query)
        || action.name.toLowerCase().includes(query)
        || action.description.toLowerCase().includes(query))
      .map((action) => ({ kind: 'action' as const, action }));

    // An empty query is the ChatGPT-like first level. Once the user keeps typing,
    // include matching agents directly so the old fast path `@agent-name` still works.
    return query ? [...actionCandidates, ...agentCandidates] : actionCandidates;
  }, [actions, agents, canMentionAgents, query, screen]);

  const safeSelectedIndex = Math.min(selectedIndex, Math.max(candidates.length - 1, 0));

  function handleInputChange(value: string, prevValue: string) {
    // Count @ signs — works regardless of where the @ was inserted
    const newAtCount = (value.match(/@/g) || []).length;
    const oldAtCount = (prevValue.match(/@/g) || []).length;
    if (newAtCount > oldAtCount) {
      setScreen('root');
      setMentionVisible(true);
      setSelectedIndex(0);
      return;
    }
    if (mentionVisible) {
      const lastAt = value.lastIndexOf('@');
      if (lastAt === -1) {
        setMentionVisible(false);
      } else {
        const afterAt = value.slice(lastAt + 1);
        if (afterAt.includes(' ')) setMentionVisible(false);
        else setSelectedIndex(0);
      }
    }
  }

  /** Only handles ArrowUp/Down. Enter/Tab/Escape are handled by InputArea. */
  function handleKeyDown(e: React.KeyboardEvent) {
    if (!mentionVisible || candidates.length === 0) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelectedIndex((index) => Math.min(index + 1, candidates.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelectedIndex((index) => Math.max(index - 1, 0));
    }
  }

  function showAgentPicker() {
    setScreen('agents');
    setSelectedIndex(0);
  }

  function backToRoot() {
    setScreen('root');
    setSelectedIndex(0);
  }

  return {
    mentionVisible,
    setMentionVisible,
    selectedIndex: safeSelectedIndex,
    setSelectedIndex,
    screen,
    candidates,
    handleInputChange,
    handleKeyDown,
    showAgentPicker,
    backToRoot,
  };
}
