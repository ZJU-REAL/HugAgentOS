import { ChatSlot } from '../../customization/ChatCustomization';
import { usePublicChat } from '../../public-chat/PublicChatContext';
import { useChatStore } from '../../stores/chatStore';
import { chatDraftKey, readComposer } from '../../stores/composerStore';
import { FileTextOutlined, PieChartOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { staggerStyle } from '../../utils/motionTokens';
import { useCatalogStore } from '../../stores/catalogStore';
import { useProjectStore } from '../../stores/projectStore';
import { t } from '../../i18n';
import type { ChatAreaProps } from './ChatArea';
import type { useChatWelcome } from './useChatWelcome';
import { InputArea } from './InputArea';
import ProjectConversationHistory from '../projects/ProjectConversationHistory';

const HOME_SUGGESTION_ICONS = [SearchOutlined, FileTextOutlined, PieChartOutlined] as const;

type ComposerProps = Pick<ChatAreaProps, 'send' | 'abort' | 'activateQueuedMessage' | 'discardQueuedMessage' | 'continueLoop' | 'handleFileSelect' | 'removeFile' | 'inputRef' | 'fileInputRef'>;

export function ChatWelcome({ view, shareAccessLevel, ...composer }: ComposerProps & {
  view: ReturnType<typeof useChatWelcome>;
  shareAccessLevel: 'admin' | 'edit' | 'read' | null;
}) {
  const publicChat = usePublicChat();
  const project = useProjectStore(s => s.currentProject);
  const { overviewProjectId, isAgentChat, isSiteChat, isProjectChat, showHomepageLogo, homepageLogoUrl, cfgProductName, heroTitle, heroSubtitle, inputPlaceholder, suggestedQuestions, visibleHomepageSuggestions, suggestionPageCount, setSuggestionPage, pluginShortcuts, cfgDisclaimer } = view;
  const isProjectOverview = !!overviewProjectId;
  const { send, abort, activateQueuedMessage, discardQueuedMessage, continueLoop, handleFileSelect, removeFile, inputRef, fileInputRef } = composer;
  const applyQuickScenario = (prompt: string) => {
    readComposer(chatDraftKey(useChatStore.getState().currentChatId)).setInput(prompt);
    inputRef.current?.focus();
  };
  return (
    <div className={`jx-emptyPage${(!isAgentChat && !isSiteChat) || publicChat ? ' jx-emptyPage--main' : ''}`}>
      {isSiteChat && (
        <div className="jx-siteHeroTop">
          <button
            type="button"
            className="jx-siteHeroTopBtn"
            onClick={() => {
              useCatalogStore.getState().setPanel('sites');
            }}
          >
            {t('我的站点')}
          </button>
        </div>
      )}
      <div className="jx-emptyCenter jx-anim-stagger">
        {!isProjectOverview && <div className="jx-heroBg" style={staggerStyle(0)}>
          {!isAgentChat && !isSiteChat && showHomepageLogo && homepageLogoUrl && (
            <img
              src={homepageLogoUrl}
              alt={`${cfgProductName} Logo`}
              className="jx-homeBrandMark"
            />
          )}
          <h1 className="jx-heroTitle">{heroTitle}</h1>
          <p className="jx-heroSubtitle">{heroSubtitle}</p>
          {((!isAgentChat && !isSiteChat) || publicChat) && (
            <div className="jx-mobileHeroText">
              <h1>{publicChat ? heroTitle : 'HugAgentOS'}</h1>
              <p>{publicChat ? heroSubtitle : t('你的智能任务助手')}</p>
            </div>
          )}
        </div>}

        <div className="jx-homeInput" style={staggerStyle(1)}>
          {shareAccessLevel === 'read' ? (
            <div className="jx-chatShareReadonly">
              {t('该会话由创建者设为只读共享，无法在此发送消息')}
            </div>
          ) : (
            <ChatSlot name="composer"><InputArea
              inputRef={inputRef}
              fileInputRef={fileInputRef}
              send={() => send()}
              abort={abort}
              activateQueuedMessage={activateQueuedMessage}
              discardQueuedMessage={discardQueuedMessage}
              continueLoop={continueLoop}
              handleFileSelect={handleFileSelect}
              removeFile={removeFile}
              placeholder={inputPlaceholder}
              mobilePlaceholder={!isAgentChat && !isSiteChat && !isProjectChat ? t('输入问题或需求') : inputPlaceholder}
              disableMention={isAgentChat}
            /></ChatSlot>
          )}
        </div>

        {overviewProjectId && project?.project_id === overviewProjectId && <ProjectConversationHistory key={overviewProjectId} project={project} />}

        {/* Quick pills: only sub-agents show suggested questions */}
        {isAgentChat && suggestedQuestions.length > 0 && (
          <div className="jx-quickPills" style={staggerStyle(2)}>
            {suggestedQuestions.map((prompt: string) => (
              <button key={prompt} className="jx-quickPill" onClick={() => applyQuickScenario(prompt)}>
                {prompt}
              </button>
            ))}
          </div>
        )}

        {!isProjectOverview && !isAgentChat && !isSiteChat && visibleHomepageSuggestions.length > 0 && (
          <div className="jx-homeSuggestions" style={staggerStyle(2)}>
            <div className="jx-homeSuggestionList">
              {visibleHomepageSuggestions.map((prompt, idx) => {
                const SuggestionIcon = HOME_SUGGESTION_ICONS[idx % HOME_SUGGESTION_ICONS.length];
                return (
                  <button
                    key={prompt}
                    type="button"
                    className="jx-homeSuggestion"
                    onClick={() => applyQuickScenario(prompt)}
                  >
                    <SuggestionIcon className="jx-homeSuggestionIcon" aria-hidden="true" />
                    <span>{t(prompt)}</span>
                  </button>
                );
              })}
            </div>
            {suggestionPageCount > 1 && (
              <button
                type="button"
                className="jx-homeSuggestionRefresh"
                onClick={() => setSuggestionPage((page) => (page + 1) % suggestionPageCount)}
                aria-label={t('换一批')}
              >
                <ReloadOutlined aria-hidden="true" />
                <span>{t('换一批')}</span>
              </button>
            )}
          </div>
        )}

        {/* Capability cards: plugin-contributed homepage entries (main agent page only) */}
        {!isProjectOverview && !isAgentChat && !isSiteChat && pluginShortcuts.length > 0 && (
          <div className="jx-capCards" style={staggerStyle(2)}>
            {pluginShortcuts.map((card) => (
              <button
                key={card.id}
                type="button"
                className="jx-capCard"
                onClick={() => applyQuickScenario(card.prompt)}
              >
                {card.icon ? <img src={card.icon} alt="" className="jx-capCardIcon" /> : null}
                <span className="jx-capCardLabel">{t(card.label)}</span>
              </button>
            ))}
          </div>
        )}

      </div>
      {!isAgentChat && cfgDisclaimer && cfgDisclaimer.trim() && (
        <div className="jx-aiDisclaimer">
          {cfgDisclaimer.split('\n').map((line, i, arr) => (
            <span key={i}>{line}{i < arr.length - 1 ? <br /> : null}</span>
          ))}
        </div>
      )}
    </div>
  );
}
