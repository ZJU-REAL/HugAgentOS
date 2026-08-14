import { useEffect, useRef, useState } from 'react';
import { useChatStore, useChatModeStore } from '../../stores';
import { ChipChevron } from '../common/ChipChevron';
import { IconAgentPreset } from '../common/DshIcons';
import { t } from '../../i18n';

/**
 * 对话框上方的模式选择位。
 *
 * 形态照搬参考稿的 AgentPresetSeat：平时是一枚透明 chip（标识 + 当前模式名 +
 * 箭头），hover / 展开才浮出底色；菜单每行「名称 + 一句描述」——光看模式名说不清
 * 它到底改了什么，描述才是选择依据。
 *
 * 名单来自服务端（内置的标准/极速 + 管理员建的官方模式 + 本人建的私有模式），
 * 见 stores/chatModeStore.ts。选中一个模式会顺带把思考强度切到它的默认档；模式
 * 若锁死强度（极速模式），右侧模型位随之置灰（ModelEffortChip 自己读这个策略）。
 *
 * 上行是两个正交字段：`mode_slug` 决定工具面与提示词，`chat_mode` 只管思考强度。
 */
export default function ChatModeSwitch() {
  const modeSlug = useChatStore((s) => s.modeSlug);
  const setModeSlug = useChatStore((s) => s.setModeSlug);
  const setChatMode = useChatStore((s) => s.setChatMode);
  const modes = useChatModeStore((s) => s.modes);
  const fetchModes = useChatModeStore((s) => s.fetchModes);

  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => { void fetchModes(); }, [fetchModes]);

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

  const current = modes.find((m) => m.slug === modeSlug) || modes[0];
  // 只有一条可选时这个位没有决策价值，直接不渲染（部署方把官方模式全停用的情况）
  if (!current || modes.length <= 1) return null;

  const pick = (slug: string) => {
    setOpen(false);
    if (slug === modeSlug) return;
    const next = modes.find((m) => m.slug === slug);
    setModeSlug(slug);
    if (!next) return;
    // 模式带默认思考强度：换模式就跟着切到它的默认档（这是"模式"的意义之一——
    // 一次选择把工具面和想多深一起定了）。锁死的模式用户之后改不了，没锁的可以
    // 在右侧模型位自己再调。
    setChatMode(next.default_effort);
  };

  return (
    <div className="jx-modeSeat" ref={rootRef}>
      <button
        type="button"
        className={`jx-modeSeatBtn${open ? ' open' : ''}${current.slug !== 'standard' ? ' turbo' : ''}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        title={t('选择这段对话的模式')}
      >
        <IconAgentPreset size={15} className="jx-modeSeatIcon" />
        <span className="jx-modeSeatLabel">{current.name}</span>
        <ChipChevron />
      </button>

      {open && (
        <div className="jx-modeSeatMenu" role="menu">
          {modes.map((mode) => {
            const selected = mode.slug === current.slug;
            return (
              <button
                type="button"
                role="menuitemradio"
                aria-checked={selected}
                key={mode.slug}
                className="jx-menuOption"
                onClick={() => pick(mode.slug)}
              >
                <span className="jx-menuOption-copy">
                  <span className="jx-menuOption-title">
                    {mode.name}
                    {mode.is_private && <span className="jx-modeSeatTag">{t('我的')}</span>}
                  </span>
                  <span className="jx-menuOption-desc">{mode.description}</span>
                </span>
                {selected && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
