import assert from 'node:assert/strict';

import {
  mergePendingUserQuestionRecovery,
  parseRecommendedLabel,
  toUserQuestionRequest,
} from '../src/api';
import { useUIStore } from '../src/stores/uiStore';


const first = toUserQuestionRequest({
  request_id: 'req-1',
  created_at: 10,
  expires_at: 20,
  questions: [
    {
      id: 'scope',
      header: '范围',
      question: '修改范围？',
      description: '这个选择会影响交付时间。',
      multi_select: false,
      options: [
        {
          id: 'current',
          label: '仅当前页面 (Recommended)',
          description: '更快完成。',
        },
        { id: 'all', label: '全部页面' },
      ],
    },
  ],
});

assert.equal(first.requestId, 'req-1');
assert.equal(first.questions[0].options[0].recommended, true);
assert.equal(first.questions[0].options[0].label, '仅当前页面');
assert.equal(first.questions[0].multiSelect, false);
assert.deepEqual(parseRecommendedLabel('Fast (Recommended)'), {
  label: 'Fast',
  recommended: true,
});
assert.deepEqual(parseRecommendedLabel('稳妥（推荐）'), {
  label: '稳妥',
  recommended: true,
});

const second = { ...first, requestId: 'req-2' };
const late = { ...first, requestId: 'req-late' };
assert.deepEqual(
  mergePendingUserQuestionRecovery(
    [first],
    [first, late],
    new Set(['req-1']),
  ).map((item) => item.requestId),
  ['req-1', 'req-late'],
);
useUIStore.setState({ pendingUserQuestions: {} });
const ui = useUIStore.getState();
ui.enqueuePendingUserQuestion('chat-1', first);
ui.enqueuePendingUserQuestion('chat-1', first);
ui.enqueuePendingUserQuestion('chat-1', second);
assert.deepEqual(
  useUIStore.getState().pendingUserQuestions['chat-1'].map((item) => item.requestId),
  ['req-1', 'req-2'],
);

useUIStore.getState().resolvePendingUserQuestion('chat-1', 'req-1');
assert.deepEqual(
  useUIStore.getState().pendingUserQuestions['chat-1'].map((item) => item.requestId),
  ['req-2'],
);

useUIStore.getState().hydratePendingUserQuestions([
  { chatId: 'chat-1', request: first },
]);
assert.deepEqual(
  useUIStore.getState().pendingUserQuestions['chat-1'].map((item) => item.requestId),
  ['req-2'],
);

// A server-resolved event may arrive before an older pending snapshot. The
// recovery response must not resurrect that settled request.
useUIStore.getState().resolvePendingUserQuestion('chat-2', 'req-raced');
useUIStore.getState().hydratePendingUserQuestionQueue('chat-2', [
  { ...first, requestId: 'req-raced' },
]);
assert.equal(useUIStore.getState().pendingUserQuestions['chat-2'], undefined);

console.log('user question mapping/store tests passed');
