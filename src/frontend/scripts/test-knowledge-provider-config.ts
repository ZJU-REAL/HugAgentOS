import assert from 'node:assert/strict';

import {
  getKnowledgeProviderConfigKey,
  isKnowledgeProviderConfigVisible,
} from '../src/utils/knowledgeProviderConfig';

const values: Record<string, string> = {
  'knowledge_base.url': 'http://legacy-dify.test/v1',
  'knowledge_base.dify.url': 'http://dify.test/v1',
  'knowledge_base.fastgpt.url': 'http://fastgpt.test',
  'knowledge_base.weknora.url': 'http://weknora.test',
};

assert.equal(
  values[getKnowledgeProviderConfigKey('dify', 'url')],
  'http://dify.test/v1',
);
assert.equal(
  values[getKnowledgeProviderConfigKey('fastgpt', 'url')],
  'http://fastgpt.test',
);
assert.equal(
  values[getKnowledgeProviderConfigKey('weknora', 'url')],
  'http://weknora.test',
);

assert.equal(
  isKnowledgeProviderConfigVisible('knowledge_base.url', 'dify'),
  false,
);
assert.equal(
  isKnowledgeProviderConfigVisible('knowledge_base.dify.url', 'fastgpt'),
  false,
);
assert.equal(
  isKnowledgeProviderConfigVisible('knowledge_base.fastgpt.url', 'fastgpt'),
  true,
);
assert.equal(
  isKnowledgeProviderConfigVisible('knowledge_base.weknora.api_key', 'weknora'),
  true,
);
assert.equal(
  isKnowledgeProviderConfigVisible('knowledge_base.weknora.allowed_dataset_ids', 'custom'),
  false,
);
assert.equal(
  isKnowledgeProviderConfigVisible('knowledge_base.detail_max_chars', 'custom'),
  true,
);

console.log('knowledge provider config tests passed');
