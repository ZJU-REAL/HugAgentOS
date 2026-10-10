import { useMemo, useState } from 'react';
import { useEditionStore, usePluginUiStore } from '../../stores';
import { resolveText } from '../../plugin-ui';
import { useAgentStore } from '../../stores/agentStore';
import { usePageConfig } from '../../hooks/usePageConfig';
import { projectOverviewId } from '../../stores/chatStore';
import { resolveBatchModeActive } from '../../utils/chatMode';
import { t } from '../../i18n';
import type { ChatItem } from '../../types';

const HOME_SUGGESTIONS_PER_PAGE = 3;

export function useChatWelcome(chat: ChatItem | undefined) {
  const [page, setPage] = useState<{ questions?: string[]; show?: boolean; index: number }>({ index: 0 });
  const isCE = useEditionStore((s) => s.edition === 'ce');
  const pluginContributions = usePluginUiStore((s) => s.items);
  // 首页快捷入口只保留插件贡献的入口（管理端配置项已下线，场景引导统一走首页建议问题）。
  const pluginShortcuts = useMemo(() => {
    if (isCE) return [];
    return pluginContributions
      .flatMap((item) => item.contributes.shortcuts || [])
      .filter((shortcut) => !!shortcut.prompt)
      .map((shortcut) => ({
        id: shortcut.id,
        label: resolveText(shortcut.label, shortcut.id),
        icon: shortcut.icon || '',
        prompt: resolveText(shortcut.prompt!),
      }));
  }, [isCE, pluginContributions]);

  // ── Resolve sub-agent details for welcome page ──
  const { agents } = useAgentStore();
  const agentDetail = useMemo(() => {
    const aid = chat?.agentId;
    if (!aid) return null;
    return agents.find((a) => a.agent_id === aid) || null;
  }, [chat?.agentId, agents]);

  // ── Page config default values ──
  const cfgHeroTitle = usePageConfig('branding.hero_title', '你好，我是 HugAgentOS');
  const cfgHeroSubtitle = usePageConfig('branding.hero_subtitle', '今天想从哪里开始？');
  const cfgProductName = usePageConfig('branding.product_name', 'HugAgentOS');
  const cfgDisclaimer = usePageConfig('branding.disclaimer', '');
  const cfgInputPlaceholder = usePageConfig('texts.input_placeholder', '请输入你的问题，按Enter发送，Shift+Enter换行');
  const showHomepageLogo = usePageConfig('homepage.show_logo', true);
  const homepageLogoUrl = usePageConfig('homepage.logo_url', '/icon.png');
  const showHomepageSuggestions = usePageConfig('homepage.show_suggestions', true);
  const configuredHomepageSuggestions = usePageConfig<string[]>('homepage.suggested_questions', []);
  const suggestionPage = page.questions === configuredHomepageSuggestions && page.show === showHomepageSuggestions ? page.index : 0;
  const setSuggestionPage = (update: (value: number) => number) => setPage({
    questions: configuredHomepageSuggestions, show: showHomepageSuggestions, index: update(suggestionPage),
  });
  const homepageSuggestions = useMemo(
    () => showHomepageSuggestions
      ? configuredHomepageSuggestions.filter((prompt) => typeof prompt === 'string' && prompt.trim())
      : [],
    [configuredHomepageSuggestions, showHomepageSuggestions],
  );
  const suggestionPageCount = Math.max(
    1,
    Math.ceil(homepageSuggestions.length / HOME_SUGGESTIONS_PER_PAGE),
  );
  const visibleHomepageSuggestions = useMemo(() => {
    const start = (suggestionPage % suggestionPageCount) * HOME_SUGGESTIONS_PER_PAGE;
    return homepageSuggestions.slice(start, start + HOME_SUGGESTIONS_PER_PAGE);
  }, [homepageSuggestions, suggestionPage, suggestionPageCount]);

  // ── Resolve hero text: sub-agent uses its own name/description ──
  const isAgentChat = !!(chat?.agentId);
  const isSiteChat = !!chat?.siteChat;
  const isProjectChat = !!chat?.projectId;
  const heroTitle = isSiteChat
    ? t('我们该构建什么？')
    : isAgentChat ? (chat.agentName || t('智能体')) : cfgHeroTitle;
  const heroSubtitle = isSiteChat
    ? t('描述你想要的网站，AI 将为你生成并一键发布上线')
    : isAgentChat
      ? (agentDetail?.description || agentDetail?.welcome_message || t('专业智能体'))
      : cfgHeroSubtitle;
  const suggestedQuestions = isAgentChat ? (agentDetail?.suggested_questions || []) : [];
  const isBatchChat = resolveBatchModeActive(chat);
  const inputPlaceholder = isSiteChat
    ? t('描述你想要的网站，例如：一个展示咖啡馆菜单与营业时间的单页网站')
    : isAgentChat
      ? t('向{name}提问...', { name: chat.agentName || t('智能体') })
      : isBatchChat
        ? t('描述要批量处理的对象与任务，例如："分别用一句话评价阿里、腾讯、字节"')
        : isProjectChat ? t('在「{name}」内开始新对话，Enter 发送，Shift+Enter 换行', { name: chat.projectName || t('项目') }) : cfgInputPlaceholder;

  return { overviewProjectId: projectOverviewId(chat), isAgentChat, isSiteChat, isProjectChat, showHomepageLogo, homepageLogoUrl, cfgProductName, heroTitle, heroSubtitle, inputPlaceholder, suggestedQuestions, visibleHomepageSuggestions, suggestionPageCount, setSuggestionPage, pluginShortcuts, cfgDisclaimer };
}
