import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
} from 'react';
import { Button, message } from 'antd';
import {
  CheckOutlined,
  CloseOutlined,
  DownOutlined,
  EditOutlined,
  LeftOutlined,
  RightOutlined,
  UpOutlined,
} from '@ant-design/icons';

import { answerUserQuestion, cancelUserQuestion } from '../../api';
import { useChatStore, useUIStore } from '../../stores';
import type {
  UserQuestionAnswer,
  UserQuestionItem,
  UserQuestionRequest,
} from '../../types';
import { t } from '../../i18n';


interface AnswerDraft {
  selected: string[];
  custom: string;
  skipped: boolean;
}

interface RequestComposerProps {
  chatId: string;
  request: UserQuestionRequest;
  resolveRequest: (chatId: string, requestId: string) => void;
}

/** Inline validation feedback; network failures still surface as toasts. */
type Feedback = '' | 'unanswered' | 'incomplete';

const EMPTY_DRAFT: AnswerDraft = { selected: [], custom: '', skipped: false };

function isAnswered(draft: AnswerDraft | undefined): boolean {
  return !!(draft && (draft.selected.length > 0 || draft.custom.trim().length > 0));
}

function isComplete(draft: AnswerDraft | undefined): boolean {
  return !!draft && (draft.skipped || isAnswered(draft));
}

interface AnswerFieldProps {
  /** `inline` borrows the custom row's chrome; `block` carries its own frame. */
  variant: 'inline' | 'block';
  value: string;
  placeholder: string;
  disabled: boolean;
  autoFocus?: boolean;
  onChange: (event: ChangeEvent<HTMLTextAreaElement>) => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
}

/**
 * Auto-growing free-text answer: a textarea stacked over a hidden mirror that
 * owns the height, so a long answer soft-wraps instead of scrolling a fixed
 * box. Mirror and textarea must keep identical type, padding and wrapping —
 * a mismatch makes the box the wrong height for the text being typed.
 */
function AnswerField({ variant, ...props }: AnswerFieldProps) {
  return (
    <div
      className={
        'jx-askQ-field'
        + (variant === 'inline' ? ' jx-askQ-field--inline' : ' jx-askQ-field--block')
      }
    >
      <div aria-hidden className="jx-askQ-fieldMirror">{`${props.value}\n`}</div>
      <textarea
        autoFocus={props.autoFocus}
        className="jx-askQ-fieldInput"
        rows={1}
        maxLength={2000}
        value={props.value}
        disabled={props.disabled}
        placeholder={props.placeholder}
        onChange={props.onChange}
        onKeyDown={props.onKeyDown}
      />
    </div>
  );
}

/**
 * Resident composer for model-initiated questions.
 *
 * The keyed inner component isolates async state between chats and FIFO
 * requests. Server-owned POST/SSE/pending responses all resolve the exact
 * request id; no synthetic continuation message is sent.
 */
export function AskUserQuestionComposer() {
  const currentChatId = useChatStore((state) => state.currentChatId);
  const request = useUIStore(
    (state) => state.pendingUserQuestions[currentChatId]?.[0],
  );
  const resolveRequest = useUIStore((state) => state.resolvePendingUserQuestion);

  if (!request) return null;
  return (
    <RequestComposer
      key={`${currentChatId}:${request.requestId}`}
      chatId={currentChatId}
      request={request}
      resolveRequest={resolveRequest}
    />
  );
}

function RequestComposer({ chatId, request, resolveRequest }: RequestComposerProps) {
  const [questionIndex, setQuestionIndex] = useState(0);
  const [drafts, setDrafts] = useState<Record<string, AnswerDraft>>({});
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<Feedback>('');
  // Collapsed to the header strip so the conversation above stays readable
  // while the user decides; drafts survive because the state lives here.
  const [minimized, setMinimized] = useState(false);
  const panelRef = useRef<HTMLElement>(null);
  const accessibleId = useId();
  const titleId = `${accessibleId}-title`;

  const questions = request.questions;
  const question = questions[questionIndex] as UserQuestionItem | undefined;
  const draft = question ? drafts[question.id] ?? EMPTY_DRAFT : EMPTY_DRAFT;
  const hasOptions = !!question && question.options.length > 0;
  const multiSelect = !!question?.multiSelect;

  useEffect(() => {
    if (minimized) return undefined;
    const frame = window.requestAnimationFrame(() => {
      panelRef.current
        ?.querySelector<HTMLElement>('[data-question-focus="true"]')
        ?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [questionIndex, minimized]);

  const buildAnswers = useMemo(() => (
    (answerDrafts: Record<string, AnswerDraft>): UserQuestionAnswer[] => questions.map((item) => {
      const itemDraft = answerDrafts[item.id] ?? EMPTY_DRAFT;
      const custom = itemDraft.custom.trim();
      return {
        id: item.id,
        // A custom answer supplements checked labels on a multi-select
        // question, but replaces the choice on a single-select one.
        selected: custom && !item.multiSelect ? [] : itemDraft.selected,
        ...(custom ? { custom } : {}),
        ...(itemDraft.skipped ? { skipped: true } : {}),
      };
    })
  ), [questions]);

  if (!question) return null;

  const updateDraft = (patch: Partial<AnswerDraft>) => {
    setFeedback('');
    setDrafts((current) => ({
      ...current,
      [question.id]: { ...(current[question.id] ?? EMPTY_DRAFT), ...patch },
    }));
  };

  const submit = async (answerDrafts: Record<string, AnswerDraft>) => {
    const missing = questions.findIndex((item) => !isComplete(answerDrafts[item.id]));
    if (missing >= 0) {
      setQuestionIndex(missing);
      setFeedback('incomplete');
      return;
    }
    if (busy) return;
    setBusy(true);
    try {
      const result = await answerUserQuestion(
        chatId,
        request.requestId,
        buildAnswers(answerDrafts),
      );
      // The POST response is itself server-authoritative: a successful claim
      // atomically removed this request. SSE and periodic pending snapshots
      // provide the equivalent terminal signal to other tabs.
      resolveRequest(chatId, request.requestId);
      if (result.stale) {
        message.warning(
          result.message
          || (result.chat_interrupted
            ? t('上次会话因服务端重启未完成，请重新发送您的消息')
            : t('该问题已过期，无需再回答。')),
        );
        return;
      }
      message.success(t('答案已提交，助手正在继续…'));
    } catch (error: unknown) {
      message.error(
        t('提交失败：{msg}', {
          msg: error instanceof Error ? error.message : String(error),
        }),
      );
    } finally {
      setBusy(false);
    }
  };

  const goTo = (index: number) => {
    setQuestionIndex(index);
    setFeedback('');
  };

  const advance = (nextDrafts: Record<string, AnswerDraft> = drafts) => {
    if (questionIndex < questions.length - 1) {
      goTo(questionIndex + 1);
      return;
    }
    void submit(nextDrafts);
  };

  const toggleOption = (optionId: string) => {
    if (busy) return;
    if (multiSelect) {
      updateDraft({
        selected: draft.selected.includes(optionId)
          ? draft.selected.filter((id) => id !== optionId)
          : [...draft.selected, optionId],
        skipped: false,
      });
      return;
    }
    const nextDrafts = {
      ...drafts,
      [question.id]: { selected: [optionId], custom: '', skipped: false },
    };
    setDrafts(nextDrafts);
    setFeedback('');
    // A single choice is the whole answer: move on rather than making the
    // user confirm what they just clicked.
    if (questionIndex < questions.length - 1) goTo(questionIndex + 1);
  };

  const continueFlow = () => {
    if (busy) return;
    if (!isAnswered(drafts[question.id])) {
      setFeedback('unanswered');
      return;
    }
    advance();
  };

  const skip = () => {
    if (busy) return;
    const nextDrafts = {
      ...drafts,
      [question.id]: { selected: [], custom: '', skipped: true },
    };
    setDrafts(nextDrafts);
    setFeedback('');
    advance(nextDrafts);
  };

  const cancel = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const result = await cancelUserQuestion(chatId, request.requestId);
      resolveRequest(chatId, request.requestId);
      message.info(result.message || t('已取消本次提问，助手会根据已有信息继续。'));
    } catch (error: unknown) {
      message.error(
        t('取消失败：{msg}', {
          msg: error instanceof Error ? error.message : String(error),
        }),
      );
    } finally {
      setBusy(false);
    }
  };

  const draftCustom = (event: ChangeEvent<HTMLTextAreaElement>) => {
    updateDraft({
      selected: multiSelect ? draft.selected : [],
      custom: event.target.value,
      skipped: false,
    });
  };

  const onFieldKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key !== 'Enter'
      || event.shiftKey
      || event.nativeEvent.isComposing
      || event.keyCode === 229
    ) return;
    event.preventDefault();
    continueFlow();
  };

  const isLast = questionIndex === questions.length - 1;

  return (
    <section
      ref={panelRef}
      className={'jx-askQ' + (minimized ? ' jx-askQ--minimized' : '')}
      role="dialog"
      aria-labelledby={titleId}
      aria-busy={busy}
    >
      <header className="jx-askQ-header">
        <div className="jx-askQ-heading">
          {question.header && (
            <div className="jx-askQ-eyebrow">{question.header}</div>
          )}
          <h2 id={titleId} className="jx-askQ-title">{question.question}</h2>
        </div>
        <div className="jx-askQ-headerActions">
          <button
            type="button"
            className="jx-askQ-iconButton"
            aria-label={minimized ? t('展开提问') : t('收起提问')}
            title={minimized ? t('展开提问') : t('收起提问')}
            aria-expanded={!minimized}
            disabled={busy}
            onClick={() => setMinimized((current) => !current)}
          >
            {minimized ? <UpOutlined /> : <DownOutlined />}
          </button>
          <button
            type="button"
            className="jx-askQ-iconButton"
            aria-label={t('取消本次提问')}
            title={t('取消本次提问')}
            disabled={busy}
            onClick={() => void cancel()}
          >
            <CloseOutlined />
          </button>
        </div>
      </header>

      {!minimized && (
        <>
          <div className="jx-askQ-body">
            {question.description && (
              <div className="jx-askQ-detail">{question.description}</div>
            )}
            <div
              className="jx-askQ-options"
              role={multiSelect ? 'group' : 'radiogroup'}
              aria-label={question.question}
            >
              {question.options.map((option, optionIndex) => {
                const selected = draft.selected.includes(option.id);
                return (
                  <button
                    key={option.id}
                    type="button"
                    role={multiSelect ? 'checkbox' : 'radio'}
                    aria-checked={selected}
                    data-question-focus={optionIndex === 0 ? 'true' : undefined}
                    className={
                      'jx-askQ-option'
                      + (selected ? ' jx-askQ-option--selected' : '')
                    }
                    disabled={busy}
                    onClick={() => toggleOption(option.id)}
                  >
                    {multiSelect ? (
                      <span
                        className={
                          'jx-askQ-checkbox'
                          + (selected ? ' jx-askQ-checkbox--checked' : '')
                        }
                        aria-hidden="true"
                      >
                        {selected && <CheckOutlined />}
                      </span>
                    ) : (
                      <span className="jx-askQ-number" aria-hidden="true">
                        {optionIndex + 1}
                      </span>
                    )}
                    <span className="jx-askQ-optionCopy">
                      <span className="jx-askQ-optionLabel">{option.label}</span>
                      {option.recommended && (
                        <span className="jx-askQ-badge">{t('推荐')}</span>
                      )}
                      {option.description && (
                        <span className="jx-askQ-optionDesc">{option.description}</span>
                      )}
                    </span>
                  </button>
                );
              })}

              {hasOptions ? (
                <div
                  className={
                    'jx-askQ-customRow'
                    + (draft.custom ? ' jx-askQ-customRow--active' : '')
                  }
                >
                  {multiSelect ? (
                    <span
                      className={
                        'jx-askQ-checkbox'
                        + (draft.custom ? ' jx-askQ-checkbox--checked' : '')
                      }
                      aria-hidden="true"
                    >
                      {!!draft.custom && <CheckOutlined />}
                    </span>
                  ) : (
                    <span className="jx-askQ-number" aria-hidden="true">
                      <EditOutlined />
                    </span>
                  )}
                  <AnswerField
                    variant="inline"
                    value={draft.custom}
                    disabled={busy}
                    placeholder={t('可以补充选项之外的要求…')}
                    onChange={draftCustom}
                    onKeyDown={onFieldKeyDown}
                  />
                </div>
              ) : (
                <AnswerField
                  variant="block"
                  autoFocus
                  value={draft.custom}
                  disabled={busy}
                  placeholder={t('请输入回答…')}
                  onChange={draftCustom}
                  onKeyDown={onFieldKeyDown}
                />
              )}
            </div>
          </div>

          <footer className="jx-askQ-footer">
            {questions.length > 1 && (
              <div className="jx-askQ-pager">
                <button
                  type="button"
                  className="jx-askQ-iconButton"
                  aria-label={t('上一题')}
                  title={t('上一题')}
                  disabled={questionIndex === 0 || busy}
                  onClick={() => goTo(questionIndex - 1)}
                >
                  <LeftOutlined />
                </button>
                <span className="jx-askQ-progress">
                  {t('{current} / {total}', {
                    current: questionIndex + 1,
                    total: questions.length,
                  })}
                </span>
                <button
                  type="button"
                  className="jx-askQ-iconButton"
                  aria-label={t('下一题')}
                  title={t('下一题')}
                  disabled={isLast || busy}
                  onClick={() => goTo(questionIndex + 1)}
                >
                  <RightOutlined />
                </button>
              </div>
            )}
            <div className="jx-askQ-feedback" role="status">
              {feedback === 'unanswered' && t('请先作答，或跳过这一题。')}
              {feedback === 'incomplete' && t('还有问题没有作答。')}
            </div>
            <div className="jx-askQ-actions">
              <Button size="small" disabled={busy} onClick={skip}>
                {t('跳过此题')}
              </Button>
              <Button
                size="small"
                type="primary"
                loading={busy}
                disabled={busy}
                onClick={continueFlow}
              >
                {isLast ? t('提交回答') : t('下一题')}
              </Button>
            </div>
          </footer>
        </>
      )}
    </section>
  );
}
