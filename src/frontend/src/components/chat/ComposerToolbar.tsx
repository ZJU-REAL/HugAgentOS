import { useCompactComposerToolbar } from './useCompactComposerToolbar';
import { OrderedListOutlined, SyncOutlined, PartitionOutlined } from '@ant-design/icons';
import { ContentErrorBoundary } from '../common';
import ApprovalPill from './ApprovalPill';
import ModelEffortChip from './ModelEffortChip';
import { ContextGauge } from './ContextGauge';
import { ComposerModeChip } from './ComposerModeChip';
import { ComposerSendButton } from './ComposerSendButton';
import { ComposerAttachmentMenu } from './ComposerAttachmentMenu';
import { ComposerProjectMenu } from './ComposerProjectMenu';
import { VoiceInput } from '../voice/VoiceInput';
import type { VoiceInputState } from '../voice/useVoiceInput';
import { t } from '../../i18n';
import type { InputAreaProps, ComposerOptions } from './composerTypes';
import type { ComposerState } from './useComposerState';
import type { ComposerEditor } from './useComposerEditor';

export function ComposerToolbar({ state, editor, options, fileInputRef, showStopButton, composerHasContent, voice }: {
  state: ComposerState; editor: ComposerEditor; options: ComposerOptions;
  fileInputRef: InputAreaProps['fileInputRef']; showStopButton: boolean; composerHasContent: boolean; voice?: VoiceInputState;
}) {
  const { isCE, isAppAllowed, promptHubOpen, setPromptHubOpen, planMode, workflowModeOn,
    onCloseMode, showLoopEntry, loopMode, setLoopMode, uploadingFiles } = state;
  const { forkPending, sendFromComposer } = editor;
  const { abort } = options;
  const toolbarRef = useCompactComposerToolbar();
  return (
    <div className="jx-composerToolbar">
      <div className="jx-composerBar" ref={toolbarRef}>
        <div className="jx-composerTools">
          <ComposerAttachmentMenu state={state} editor={editor} options={options} fileInputRef={fileInputRef} />
          {/* 提示词中心：社区版没有这个能力；商业版按 Config「权限配置 → 应用可见范围」
              的 prompt_hub 位放行（allowed_apps 为空 = 不限制，等同全员可见）。 */}
          {!isCE && isAppAllowed('prompt_hub') && (
            <button
              type="button"
              className={`jx-composerChip jx-promptHubBtn${promptHubOpen ? ' active' : ''}`}
              onClick={() => setPromptHubOpen(!promptHubOpen)}
              title={t('提示词中心')}
              aria-label={t('提示词中心')}
              aria-pressed={promptHubOpen}
            >
              <img src="/home/prompt.svg" alt="" className="jx-promptHubIcon" />
              <span className="jx-composerChip-label">{t('提示词中心')}</span>
            </button>
          )}

          <ComposerProjectMenu state={state} />
          <ApprovalPill />
          {/* Active modes always retain a keyboard-accessible exit. */}
          {planMode && (
            <ComposerModeChip
              icon={<OrderedListOutlined className="jx-planModeIcon" />}
              label={t('计划模式')}
              title={t('计划模式：AI 将自动分解任务为多步骤并逐步执行')}
              closeLabel={t('关闭计划模式：切换为普通对话')}
              onClose={() => onCloseMode('plan')}
            />
          )}

          {workflowModeOn && (
            <ComposerModeChip
              icon={<PartitionOutlined className="jx-planModeIcon" />}
              label={t('工作流模式')}
              title={t('工作流模式：面对成百上千个同类工作项时，AI 会写一段作业脚本交给后台并发处理，进度记在台账上，中断可续跑')}
              closeLabel={t('关闭工作流模式：切换为普通对话')}
              onClose={() => onCloseMode('workflow')}
            />
          )}

          {showLoopEntry && loopMode && (
            <ComposerModeChip
              icon={<SyncOutlined className="jx-planModeIcon" />}
              label={t('自主循环')}
              title={t('自主循环：描述一个可验证目标，AI 会反复迭代、自我修正，达标或触预算即停')}
              closeLabel={t('关闭自主循环：切换为普通对话')}
              onClose={() => setLoopMode(false)}
            />
          )}

        </div>
        <div className="jx-composerPrimary">
          {/* 模型 + 思考强度：两级菜单，收起态一个 chip 同时报出两者 */}
          <ModelEffortChip />

          {/* Context-usage ring: estimated context-window occupancy for the current conversation */}
          {/* 上下文用量条是装饰性的：它要遍历整轮工具结果算 token，成本随内容涨。
              万一在这里抛错，它长在消息列表那圈 ContentErrorBoundary **之外**，
              会一路捅到顶层错误边界、整页变成"页面显示遇到异常"——为一条进度条
              赔上整个页面不值当。单独兜一层，出事就让它自己消失。 */}
          <ContentErrorBoundary fallback={null}>
            <ContextGauge draftKey={state.draftKey} />
          </ContentErrorBoundary>

          {/* While a run is active, an empty composer keeps the stop button; typing switches
              back to send so Enter/click can queue a follow-up without cancelling the run. */}
          {voice && <VoiceInput voice={voice} />}
          <ComposerSendButton stopping={showStopButton}
            disabled={voice?.busy || forkPending || uploadingFiles.size > 0 || !composerHasContent}
            onSend={sendFromComposer} onStop={abort} />
        </div>
      </div>
    </div>
  );
}
