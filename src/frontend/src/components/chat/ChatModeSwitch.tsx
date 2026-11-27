import { Fragment, useEffect, useRef, useState } from 'react';
import { LockOutlined } from '@ant-design/icons';
import { useChatStore, useChatModeStore } from '../../stores';
import { defaultChatMode, isRouterMode } from '../../stores/chatModeStore';
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
 *
 * 「智能路由」（is_router，slug=auto）由服务端按权限放在名单最前面：它不是一套装配，
 * 而是每轮先识别意图、再进入其余某个模式。所以选中它不动思考强度（用户自己选的档位
 * 原样带上去，路由进锁定强度的模式时由后端处理），菜单里它和具体模式之间隔一道细线。
 *
 * 新对话进入本人的默认模式（管理员在权限配置里按角色 / 团队 / 个人设）。没有「模式切换」
 * 权限时这一位锁定：只显示默认模式、点不开菜单；服务端每轮也会把模式换成默认模式。
 */
export default function ChatModeSwitch() {
  const modeSlug = useChatStore((s) => s.modeSlug);
  const setModeSlug = useChatStore((s) => s.setModeSlug);
  const setChatMode = useChatStore((s) => s.setChatMode);
  const modes = useChatModeStore((s) => s.modes);
  const fetchModes = useChatModeStore((s) => s.fetchModes);
  const defaultSlug = useChatModeStore((s) => s.defaultSlug);
  const canSwitch = useChatModeStore((s) => s.canSwitch);
  const policyLoaded = useChatModeStore((s) => s.loaded);
  // 还没落记录的草稿（新对话、首条消息前）：策略一到就换成本人的默认模式。
  const isDraft = useChatStore((s) => !s.store.chats[s.currentChatId]);

  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => { void fetchModes(); }, [fetchModes]);

  useEffect(() => {
    if (policyLoaded && isDraft) setModeSlug(defaultSlug);
  }, [policyLoaded, isDraft, defaultSlug, setModeSlug]);

  // 锁定时菜单不该还开着（权限在使用中被收回）。
  useEffect(() => { if (!canSwitch) setOpen(false); }, [canSwitch]);

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

  const shownSlug = canSwitch ? modeSlug : defaultSlug;
  const current = modes.find((m) => m.slug === shownSlug) || defaultChatMode(modes);
  // 只有一条可执行的模式时这个位没有决策价值，直接不渲染（部署方把官方模式全停用的
  // 情况）——这时智能路由也无从可选，一并不出现。
  const executable = modes.filter((m) => !isRouterMode(m)).length;
  if (!current || executable <= 1) return null;
  const currentIsRouter = isRouterMode(current);

  if (!canSwitch) {
    return (
      <div className="jx-modeSeat">
        <span
          className={`jx-modeSeatBtn locked${currentIsRouter ? ' router' : current.slug !== 'standard' ? ' turbo' : ''}`}
          title={t('模式由管理员设置为「{name}」，暂不能切换', { name: current.name })}
          aria-label={t('当前模式：{name}（由管理员设置）', { name: current.name })}
        >
          <IconAgentPreset size={15} className="jx-modeSeatIcon" />
          <span className="jx-modeSeatLabel">{current.name}</span>
          <LockOutlined className="jx-modeSeatLock" aria-hidden />
        </span>
      </div>
    );
  }

  const pick = (slug: string) => {
    setOpen(false);
    if (slug === modeSlug) return;
    const next = modes.find((m) => m.slug === slug);
    setModeSlug(slug);
    // 智能路由每轮替用户挑模式，思考强度仍听用户的：不跟着改档。
    if (!next || isRouterMode(next)) return;
    // 模式带默认思考强度：换模式就跟着切到它的默认档（这是"模式"的意义之一——
    // 一次选择把工具面和想多深一起定了）。锁死的模式用户之后改不了，没锁的可以
    // 在右侧模型位自己再调。
    setChatMode(next.default_effort);
  };

  return (
    <div className="jx-modeSeat" ref={rootRef}>
      <button
        type="button"
        className={`jx-modeSeatBtn${open ? ' open' : ''}${currentIsRouter ? ' router' : current.slug !== 'standard' ? ' turbo' : ''}`}
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
          {modes.map((mode, index) => {
            const selected = mode.slug === current.slug;
            const router = isRouterMode(mode);
            const divided = router && index < modes.length - 1 && !isRouterMode(modes[index + 1]);
            return (
              <Fragment key={mode.slug}>
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={selected}
                  className={`jx-menuOption${router ? ' jx-modeSeatOption--router' : ''}`}
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
                {divided && <div className="jx-modeSeatDivider" role="separator" />}
              </Fragment>
            );
          })}
        </div>
      )}
    </div>
  );
}
