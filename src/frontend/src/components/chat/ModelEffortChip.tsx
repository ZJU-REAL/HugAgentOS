import { useEffect, useMemo, useRef, useState } from 'react';
import { RightOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { useChatStore, useChatModeStore, useModelCapabilitiesStore } from '../../stores';
import type { ThinkingEffort } from '../../stores/chatStore';
import { ChipChevron } from '../common/ChipChevron';
import { t } from '../../i18n';

interface EffortMeta {
  key: ThinkingEffort;
  title: string;
  desc: string;
  /** 收起态显示在模型名右侧的短标签 */
  short: string;
}

/**
 * 输入框右下角的「模型 + 思考强度」位，两级菜单。
 *
 * 一级是两行 cell（模型 / 思考强度，各自带当前值和右尖角），点进去才是具体列表——
 * 两件事各占一行，比原来"5 个模式挤一个下拉、模型另开一个下拉"清楚。收起态一个 chip
 * 同时报出两者：`模型名 · 思考·高`。
 *
 * 两个维度按各自的可用性独立出现：管理端没开用户模型切换就只剩思考强度，
 * 模型不支持多档 reasoning_effort 就只有「快速 / 思考」两级。极速模式下思考强度
 * 不适用（那条链路本就不思考），该行置灰并说明原因，而不是悄悄消失。
 */
export default function ModelEffortChip() {
  const chatMode = useChatStore((s) => s.chatMode);
  const setChatMode = useChatStore((s) => s.setChatMode);
  const modeSlug = useChatStore((s) => s.modeSlug);
  const modeOf = useChatModeStore((s) => s.modeOf);
  const supportsReasoningEffort = useModelCapabilitiesStore((s) => s.capabilities.supports_reasoning_effort);
  const userModelSwitchEnabled = useModelCapabilitiesStore((s) => s.capabilities.user_model_switch_enabled);
  const selectableModels = useModelCapabilitiesStore((s) => s.capabilities.user_selectable_models);
  const selectedModelProviderId = useModelCapabilitiesStore((s) => s.selectedModelProviderId);
  const setSelectedModelProviderId = useModelCapabilitiesStore((s) => s.setSelectedModelProviderId);

  const [open, setOpen] = useState(false);
  const [pane, setPane] = useState<'root' | 'model' | 'effort'>('root');
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  // 「思考强度不可调」现在由模式说了算（chat_modes.effort_locked），不再是
  // 硬编码的 chatMode==='turbo'——自定义模式也能锁死强度。
  const activeMode = modeOf(modeSlug);
  const turbo = activeMode.effort_locked || chatMode === 'turbo';
  const modelPickable = userModelSwitchEnabled && selectableModels.length > 0;

  const efforts = useMemo<EffortMeta[]>(() => {
    const fast: EffortMeta = {
      key: 'fast',
      title: t('快速'),
      desc: t('不额外思考，直接作答，适用于大部分情况'),
      short: t('快速'),
    };
    if (!supportsReasoningEffort) {
      return [fast, {
        key: 'medium',
        title: t('思考模式'),
        desc: t('作答前先推理，处理需要拆解的问题'),
        short: t('思考'),
      }];
    }
    return [
      fast,
      { key: 'medium', title: t('思考·中'),   desc: t('默认思考强度，兼顾速度与质量'), short: t('思考·中') },
      { key: 'high',   title: t('思考·高'),   desc: t('更深入推理，处理复杂分析'),     short: t('思考·高') },
      { key: 'max',    title: t('思考·超高'), desc: t('研究级别的专家智能体'),         short: t('思考·超高') },
    ];
  }, [supportsReasoningEffort]);

  // 模型不支持多档时，high/max 一律按「思考」显示，避免报出一个后端并不会照做的档位
  const effectiveEffort: ThinkingEffort = turbo
    ? 'fast'
    : supportsReasoningEffort
      ? (chatMode as ThinkingEffort)
      : (chatMode === 'fast' ? 'fast' : 'medium');
  const currentEffort = efforts.find((e) => e.key === effectiveEffort) ?? efforts[0];

  const currentModel = modelPickable
    ? (selectableModels.find((m) => m.provider_id === selectedModelProviderId)
      || selectableModels.find((m) => m.is_default)
      || selectableModels[0])
    : undefined;

  // 极速模式 + 没开模型切换 = 这个位没有可挑的东西了。但直接不渲染会让工具条右边
  // 塌一块、切回标准时又弹回来；留一枚只读标记占住这个位置，顺带说明"这里现在归极速管"。
  if (!modelPickable && turbo) {
    return (
      <div
        className="jx-composerChip jx-modelEffortStatic"
        role="status"
        aria-label={t('当前模式锁定了思考强度')}
        title={t('该模式锁定了思考强度')}
      >
        <ThunderboltOutlined className="jx-modelEffortStaticIcon" />
        {/* 报模式自己的名字，而不是写死「极速」——自定义模式也会走到这条分支 */}
        <span className="jx-composerChip-label">{activeMode.name}</span>
      </div>
    );
  }

  const triggerLabel = currentModel ? currentModel.display_name : currentEffort.short;
  const triggerAside = currentModel && !turbo ? currentEffort.short : undefined;

  // 只有一个维度可调时（管理端没开模型切换 = 只剩思考强度）直接落到那一层：
  // 让用户为了改一件事先点开一个只有一行的目录，是白赚的一次点击。
  const singlePane: 'root' | 'effort' = modelPickable ? 'root' : 'effort';

  const toggle = () => {
    setPane(singlePane);
    setOpen((v) => !v);
  };

  return (
    <div className="jx-modelEffort" ref={rootRef}>
      <button
        type="button"
        className={`jx-composerChip jx-modelEffortBtn${open ? ' open' : ''}`}
        onClick={toggle}
        aria-haspopup="menu"
        aria-expanded={open}
        title={t('切换模型与思考强度')}
      >
        <span className="jx-composerChip-label">{triggerLabel}</span>
        {triggerAside && <span className="jx-modelEffortAside">{triggerAside}</span>}
        <ChipChevron />
      </button>

      {open && (
        <div className="jx-modelEffortMenu" role="menu">
          {pane === 'root' && (
            <>
              {modelPickable && (
                <button type="button" role="menuitem" className="jx-menuCell" onClick={() => setPane('model')}>
                  <span className="jx-menuCell-label">{t('模型')}</span>
                  <span className="jx-menuCell-value">{currentModel?.display_name}</span>
                  <RightOutlined className="jx-menuCell-chevron" />
                </button>
              )}
              <button
                type="button"
                role="menuitem"
                className="jx-menuCell"
                disabled={turbo}
                onClick={() => { if (!turbo) setPane('effort'); }}
                title={turbo ? t('该模式锁定了思考强度') : undefined}
              >
                <span className="jx-menuCell-label">{t('思考强度')}</span>
                <span className="jx-menuCell-value">
                  {turbo ? t('当前模式下不可调') : currentEffort.short}
                </span>
                {!turbo && <RightOutlined className="jx-menuCell-chevron" />}
              </button>
            </>
          )}

          {pane === 'model' && (
            <>
              <button type="button" className="jx-menuBack" onClick={() => setPane('root')}>
                <RightOutlined className="jx-menuBack-chevron" />
                <span>{t('模型')}</span>
              </button>
              <div className="jx-menuPaneList">
                {selectableModels.map((model) => {
                  const selected = model.provider_id === currentModel?.provider_id;
                  return (
                    <button
                      type="button"
                      role="menuitemradio"
                      aria-checked={selected}
                      key={model.provider_id}
                      className="jx-menuOption jx-menuOption--oneLine"
                      onClick={() => { setSelectedModelProviderId(model.provider_id); setOpen(false); }}
                      title={model.model_name || model.provider}
                    >
                      {/* 模型只报名字一行——底下那行 model_name 对选择没有帮助
                          （多数就是 display_name 的小写连字符版），挪进 title 提示 */}
                      <span className="jx-menuOption-title">{model.display_name}</span>
                      {selected && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon" />}
                    </button>
                  );
                })}
              </div>
            </>
          )}

          {pane === 'effort' && (
            <>
              {modelPickable ? (
                <button type="button" className="jx-menuBack" onClick={() => setPane('root')}>
                  <RightOutlined className="jx-menuBack-chevron" />
                  <span>{t('思考强度')}</span>
                </button>
              ) : (
                <div className="jx-menuPaneTitle">{t('思考强度')}</div>
              )}
              <div className="jx-menuPaneList">
                {efforts.map((level) => {
                  const selected = level.key === effectiveEffort;
                  return (
                    <button
                      type="button"
                      role="menuitemradio"
                      aria-checked={selected}
                      key={level.key}
                      className="jx-menuOption"
                      onClick={() => { setChatMode(level.key); setOpen(false); }}
                    >
                      <span className="jx-menuOption-copy">
                        <span className="jx-menuOption-title">{level.title}</span>
                        <span className="jx-menuOption-desc">{level.desc}</span>
                      </span>
                      {selected && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon" />}
                    </button>
                  );
                })}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
