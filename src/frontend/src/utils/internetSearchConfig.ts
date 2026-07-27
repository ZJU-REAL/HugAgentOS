export type InternetSearchEngine = 'tavily' | 'baidu' | 'langsearch';

export interface InternetSearchEngineMeta {
  value: InternetSearchEngine;
  label: string;
  apiKeyConfigKey: string;
  apiKeyLabel: string;
}

export const INTERNET_SEARCH_ENGINES: InternetSearchEngineMeta[] = [
  {
    value: 'tavily',
    label: 'Tavily',
    apiKeyConfigKey: 'internet_search.tavily_api_key',
    apiKeyLabel: 'Tavily API Key',
  },
  {
    value: 'baidu',
    label: '百度千帆',
    apiKeyConfigKey: 'internet_search.baidu_api_key',
    apiKeyLabel: '百度搜索 API Key',
  },
  {
    value: 'langsearch',
    label: 'LangSearch',
    apiKeyConfigKey: 'internet_search.langsearch_api_key',
    apiKeyLabel: 'LangSearch API Key',
  },
];

const INTERNET_SEARCH_ENGINE_VALUES = new Set(
  INTERNET_SEARCH_ENGINES.map((engine) => engine.value),
);

const INTERNET_SEARCH_API_KEY_CONFIG_KEYS = new Set(
  INTERNET_SEARCH_ENGINES.map((engine) => engine.apiKeyConfigKey),
);

export function normalizeInternetSearchEngine(value?: string | null): InternetSearchEngine {
  const normalized = (value || '').trim().toLowerCase() as InternetSearchEngine;
  return INTERNET_SEARCH_ENGINE_VALUES.has(normalized) ? normalized : 'tavily';
}

export function getInternetSearchEngineMeta(value?: string | null): InternetSearchEngineMeta {
  const engine = normalizeInternetSearchEngine(value);
  return INTERNET_SEARCH_ENGINES.find((item) => item.value === engine) || INTERNET_SEARCH_ENGINES[0];
}

export function isInternetSearchConfigVisible(
  configKey: string,
  engineValue?: string | null,
): boolean {
  if (!INTERNET_SEARCH_API_KEY_CONFIG_KEYS.has(configKey)) return true;
  return configKey === getInternetSearchEngineMeta(engineValue).apiKeyConfigKey;
}
