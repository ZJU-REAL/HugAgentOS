import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ArrowLeftOutlined,
  ArrowRightOutlined,
  CheckCircleFilled,
  CloudOutlined,
  FileTextOutlined,
  GlobalOutlined,
  LogoutOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  ThunderboltOutlined,
}
from '@ant-design/icons';
import {
  Alert,
  Button,
  Input,
  InputNumber,
  Select,
  Skeleton,
  Space,
  Switch,
  Tag,
  Typography,
  message,
} from 'antd';
import { AnimatePresence, motion } from 'motion/react';
import {
  assignModelRole,
  completeFirstRunSetup,
  createModelProvider,
  getMemorySettings,
  getModelProviderSchemas,
  getMyServiceConfigs,
  getOntologySettings,
  getUserPreferences,
  listModelProviders,
  listModelRoles,
  testMyServiceConfig,
  updateMemorySettings,
  updateMemoryWriteSettings,
  updateMyServiceConfigs,
  updateOntologySettings,
  updateUserPreferences,
  type AuthUser,
  type ModelProviderItem,
  type ModelRoleAssignment,
  type ProviderSchema,
  type ServiceConfigGroup,
  type UserPreferences,
} from '../../api';
import { getLang, setLang, t, type Lang } from '../../i18n';
import { usePageConfig } from '../../hooks/usePageConfig';
import { useAuthStore, useModelCapabilitiesStore, useSettingsStore } from '../../stores';

const { Paragraph, Text, Title } = Typography;

const CHAT_CONTEXT_DEFAULT = 32768;
const STEP_STORAGE_PREFIX = 'hugagent_ce_setup_step_';

interface FirstRunSetupProps {
  user: AuthUser;
  onComplete: () => void;
}

interface ModelDraft {
  displayName: string;
  provider: string;
  baseUrl: string;
  apiKey: string;
  modelName: string;
  contextLength: number;
}

const STEP_META = [
  { title: '语言与区域', hint: '选择界面语言', icon: <GlobalOutlined /> },
  { title: '主模型', hint: '连接推理服务', icon: <RobotOutlined /> },
  { title: '联网搜索', hint: '可选服务', icon: <CloudOutlined /> },
  { title: '文件解析', hint: '可选服务', icon: <FileTextOutlined /> },
  { title: '智能增强', hint: '记忆与本体', icon: <SafetyCertificateOutlined /> },
  { title: '准备就绪', hint: '检查并进入', icon: <CheckCircleFilled /> },
] as const;

function safeStoredStep(userId: string): number {
  try {
    const value = Number(window.localStorage.getItem(`${STEP_STORAGE_PREFIX}${userId}`));
    return Number.isInteger(value) && value >= 0 && value < STEP_META.length ? value : 0;
  } catch {
    return 0;
  }
}

function isChatRole(role: ModelRoleAssignment): boolean {
  return (role.required_type || role.type) === 'chat';
}

function configuredSecret(value: string | null | undefined): boolean {
  return Boolean(value && value.includes('****'));
}

export function FirstRunSetup({ user, onComplete }: FirstRunSetupProps) {
  const brandName = usePageConfig('branding.product_name', 'HugAgentOS');
  const doLogout = useAuthStore((state) => state.doLogout);
  const [step, setStep] = useState(() => safeStoredStep(user.user_id));
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [testingGroup, setTestingGroup] = useState<string | null>(null);
  const [loadError, setLoadError] = useState('');
  const [language, setLanguage] = useState<Lang>(getLang());
  const [preferences, setPreferences] = useState<UserPreferences>({});
  const [providers, setProviders] = useState<ModelProviderItem[]>([]);
  const [roles, setRoles] = useState<ModelRoleAssignment[]>([]);
  const [schemas, setSchemas] = useState<ProviderSchema[]>([]);
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(null);
  const [creatingModel, setCreatingModel] = useState(false);
  const [modelDraft, setModelDraft] = useState<ModelDraft>({
    displayName: t('主对话模型'),
    provider: 'openai_compatible',
    baseUrl: '',
    apiKey: '',
    modelName: '',
    contextLength: CHAT_CONTEXT_DEFAULT,
  });
  const [serviceGroups, setServiceGroups] = useState<ServiceConfigGroup[]>([]);
  const [serviceValues, setServiceValues] = useState<Record<string, string>>({});
  const [dirtyServiceKeys, setDirtyServiceKeys] = useState<Record<string, boolean>>({});
  const [memoryEnabled, setMemoryEnabled] = useState(false);
  const [memoryWriteEnabled, setMemoryWriteEnabled] = useState(false);
  const [memoryAvailable, setMemoryAvailable] = useState(false);
  const [ontologyEnabled, setOntologyEnabled] = useState(false);
  const [ontologyAvailable, setOntologyAvailable] = useState(false);
  const [activeOntologyCount, setActiveOntologyCount] = useState(0);

  const persistStep = useCallback((next: number) => {
    setStep(next);
    try {
      window.localStorage.setItem(`${STEP_STORAGE_PREFIX}${user.user_id}`, String(next));
    } catch {
      // A disabled localStorage only removes resume support; the active setup still works.
    }
  }, [user.user_id]);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError('');
    try {
      const [prefs, modelProviders, modelRoles, providerSchemas, groups, memory, ontology] =
        await Promise.all([
          getUserPreferences(user.user_id),
          listModelProviders(),
          listModelRoles(),
          getModelProviderSchemas(),
          getMyServiceConfigs(),
          getMemorySettings(),
          getOntologySettings(),
        ]);

      setPreferences(prefs);
      setProviders(modelProviders);
      setRoles(modelRoles);
      setSchemas(providerSchemas);
      const mainRole = modelRoles.find((role) => role.role_key === 'main_agent');
      const currentProvider = modelProviders.find(
        (provider) => provider.provider_id === mainRole?.provider_id && provider.is_active,
      );
      const firstChatProvider = modelProviders.find(
        (provider) => provider.provider_type === 'chat' && provider.is_active,
      );
      setSelectedProviderId(currentProvider?.provider_id || firstChatProvider?.provider_id || null);
      setCreatingModel(!currentProvider && !firstChatProvider);

      setServiceGroups(groups);
      const values: Record<string, string> = {};
      for (const group of groups) {
        for (const item of group.items) {
          values[item.config_key] = item.is_secret && configuredSecret(item.config_value)
            ? ''
            : (item.config_value ?? '');
        }
      }
      setServiceValues(values);
      setDirtyServiceKeys({});

      setMemoryEnabled(memory.memory_enabled);
      setMemoryWriteEnabled(memory.memory_write_enabled);
      setMemoryAvailable(memory.mem0_available);
      setOntologyEnabled(ontology.ontology_enabled);
      setOntologyAvailable(ontology.available);
      setActiveOntologyCount(ontology.active_packs?.length || 0);
    } catch (error) {
      setLoadError((error as Error).message || t('初始化信息加载失败'));
    } finally {
      setLoading(false);
    }
  }, [user.user_id]);

  useEffect(() => {
    void load();
  }, [load]);

  const currentMainProvider = useMemo(() => {
    const mainRole = roles.find((role) => role.role_key === 'main_agent');
    return providers.find(
      (provider) => provider.provider_id === mainRole?.provider_id && provider.is_active,
    ) || null;
  }, [providers, roles]);

  const providerOptions = useMemo(
    () => schemas
      .filter((schema) => !schema.supports_types || schema.supports_types.includes('chat'))
      .map((schema) => ({ value: schema.id, label: schema.label || schema.id })),
    [schemas],
  );

  const internetGroup = useMemo(
    () => serviceGroups.find((group) => group.group_key === 'internet_search'),
    [serviceGroups],
  );
  const parserGroup = useMemo(
    () => serviceGroups.find((group) => group.group_key === 'file_parser'),
    [serviceGroups],
  );

  const updateServiceValue = (key: string, value: string) => {
    setServiceValues((previous) => ({ ...previous, [key]: value }));
    setDirtyServiceKeys((previous) => ({ ...previous, [key]: true }));
  };

  const saveServiceGroup = async (group?: ServiceConfigGroup): Promise<void> => {
    if (!group) return;
    const items = group.items
      .filter((item) => dirtyServiceKeys[item.config_key])
      .map((item) => ({ key: item.config_key, value: serviceValues[item.config_key] ?? '' }));
    if (!items.length) return;
    await updateMyServiceConfigs(items);
    setDirtyServiceKeys((previous) => {
      const next = { ...previous };
      items.forEach((item) => { delete next[item.key]; });
      return next;
    });
  };

  const handleTestGroup = async (group?: ServiceConfigGroup) => {
    if (!group) return;
    setTestingGroup(group.group_key);
    try {
      await saveServiceGroup(group);
      const result = await testMyServiceConfig(group.group_key);
      if (result.success) {
        message.success(t('连接正常，耗时 {ms}ms', { ms: result.latency_ms }));
      } else {
        message.error(t('连接失败：{msg}', { msg: result.error || t('未知错误') }));
      }
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setTestingGroup(null);
    }
  };

  const saveLanguage = async (): Promise<boolean> => {
    await updateUserPreferences(user.user_id, { ...preferences, language });
    setPreferences((previous) => ({ ...previous, language }));
    if (language !== getLang()) {
      persistStep(1);
      setLang(language);
      return true;
    }
    return false;
  };

  const saveModel = async () => {
    let providerId = selectedProviderId;
    if (creatingModel) {
      if (!modelDraft.baseUrl.trim() || !modelDraft.modelName.trim()) {
        throw new Error(t('请填写模型地址和模型名'));
      }
      const created = await createModelProvider({
        display_name: modelDraft.displayName.trim() || modelDraft.modelName.trim(),
        provider_type: 'chat',
        provider: modelDraft.provider,
        base_url: modelDraft.baseUrl.trim(),
        api_key: modelDraft.apiKey.trim(),
        model_name: modelDraft.modelName.trim(),
        extra_config: { context_length: modelDraft.contextLength },
        is_active: true,
      });
      providerId = created.provider_id;
      setProviders((previous) => [created, ...previous]);
      setSelectedProviderId(providerId);
      setCreatingModel(false);
    }
    if (!providerId) throw new Error(t('请选择或添加一个主模型'));

    const chatRoles = roles.filter(isChatRole);
    await Promise.all(chatRoles.map((role) => assignModelRole(role.role_key, providerId!)));
    setRoles((previous) => previous.map((role) => (
      isChatRole(role) ? { ...role, provider_id: providerId } : role
    )));
    void useModelCapabilitiesStore.getState().fetchCapabilities();
  };

  const handleNext = async () => {
    setBusy(true);
    try {
      if (step === 0) {
        const reloading = await saveLanguage();
        if (reloading) return;
      } else if (step === 1) {
        await saveModel();
      } else if (step === 2) {
        await saveServiceGroup(internetGroup);
      } else if (step === 3) {
        await saveServiceGroup(parserGroup);
      } else if (step === 4) {
        await Promise.all([
          updateMemorySettings(memoryAvailable && memoryEnabled),
          updateMemoryWriteSettings(memoryAvailable && memoryEnabled && memoryWriteEnabled),
          updateOntologySettings(ontologyAvailable && ontologyEnabled),
        ]);
      } else {
        await completeFirstRunSetup();
        try {
          window.localStorage.removeItem(`${STEP_STORAGE_PREFIX}${user.user_id}`);
        } catch {
          // Completion is server-side authoritative.
        }
        await Promise.all([
          useSettingsStore.getState().loadMemorySettings(),
          useSettingsStore.getState().loadOntologySettings(),
          useModelCapabilitiesStore.getState().fetchCapabilities(),
        ]);
        message.success(t('初始化完成，欢迎使用 {name}', { name: brandName }));
        onComplete();
        return;
      }
      persistStep(Math.min(step + 1, STEP_META.length - 1));
    } catch (error) {
      message.error((error as Error).message || t('保存失败，请重试'));
    } finally {
      setBusy(false);
    }
  };

  const selectProviderSchema = (provider: string) => {
    const schema = schemas.find((item) => item.id === provider);
    setModelDraft((previous) => ({
      ...previous,
      provider,
      baseUrl: schema?.autofill_base_url && schema.base_url_template
        ? schema.base_url_template
        : previous.baseUrl,
    }));
  };

  const renderLanguage = () => (
    <div className="jx-firstRun-choiceGrid">
      {([
        { value: 'zh-CN' as Lang, eyebrow: '中文', title: '简体中文', desc: '使用简体中文浏览界面与设置' },
        { value: 'en' as Lang, eyebrow: 'English', title: 'English', desc: 'Use English for the interface and settings' },
      ]).map((option) => (
        <button
          key={option.value}
          type="button"
          className={`jx-firstRun-choice${language === option.value ? ' jx-firstRun-choice--active' : ''}`}
          onClick={() => setLanguage(option.value)}
        >
          <span className="jx-firstRun-choiceEyebrow">{option.eyebrow}</span>
          <strong>{option.title}</strong>
          <span>{option.desc}</span>
          {language === option.value && <CheckCircleFilled className="jx-firstRun-choiceCheck" />}
        </button>
      ))}
    </div>
  );

  const renderModel = () => (
    <div className="jx-firstRun-form">
      {currentMainProvider && (
        <Alert
          type="success"
          showIcon
          message={t('已检测到可用主模型')}
          description={`${currentMainProvider.display_name} · ${currentMainProvider.model_name}`}
        />
      )}
      {providers.some((provider) => provider.provider_type === 'chat' && provider.is_active) && (
        <div className="jx-firstRun-field">
          <div className="jx-firstRun-labelRow">
            <Text strong>{t('使用已有模型')}</Text>
            <Button type="link" onClick={() => setCreatingModel((value) => !value)}>
              {creatingModel ? t('返回已有模型') : t('添加新模型')}
            </Button>
          </div>
          {!creatingModel && (
            <Select
              size="large"
              value={selectedProviderId || undefined}
              placeholder={t('选择一个对话模型')}
              options={providers
                .filter((provider) => provider.provider_type === 'chat' && provider.is_active)
                .map((provider) => ({
                  value: provider.provider_id,
                  label: `${provider.display_name} · ${provider.model_name}`,
                }))}
              onChange={setSelectedProviderId}
            />
          )}
        </div>
      )}
      {creatingModel && (
        <div className="jx-firstRun-modelGrid">
          <label className="jx-firstRun-field">
            <Text strong>{t('厂商或协议')}</Text>
            <Select
              size="large"
              value={modelDraft.provider}
              options={providerOptions.length ? providerOptions : [
                { value: 'openai_compatible', label: 'OpenAI Compatible' },
              ]}
              onChange={selectProviderSchema}
            />
          </label>
          <label className="jx-firstRun-field">
            <Text strong>{t('显示名称')}</Text>
            <Input
              size="large"
              value={modelDraft.displayName}
              onChange={(event) => setModelDraft((previous) => ({
                ...previous, displayName: event.target.value,
              }))}
            />
          </label>
          <label className="jx-firstRun-field jx-firstRun-field--wide">
            <Text strong>{t('模型接口地址')}</Text>
            <Input
              size="large"
              placeholder="https://api.example.com/v1"
              value={modelDraft.baseUrl}
              onChange={(event) => setModelDraft((previous) => ({
                ...previous, baseUrl: event.target.value,
              }))}
            />
          </label>
          <label className="jx-firstRun-field">
            <Text strong>{t('模型名')}</Text>
            <Input
              size="large"
              placeholder="model-name"
              value={modelDraft.modelName}
              onChange={(event) => setModelDraft((previous) => ({
                ...previous, modelName: event.target.value,
              }))}
            />
          </label>
          <label className="jx-firstRun-field">
            <Text strong>API Key</Text>
            <Input.Password
              size="large"
              placeholder="sk-..."
              autoComplete="new-password"
              value={modelDraft.apiKey}
              onChange={(event) => setModelDraft((previous) => ({
                ...previous, apiKey: event.target.value,
              }))}
            />
          </label>
          <label className="jx-firstRun-field">
            <Text strong>{t('上下文窗口')}</Text>
            <InputNumber
              size="large"
              min={1024}
              step={1024}
              value={modelDraft.contextLength}
              onChange={(value) => setModelDraft((previous) => ({
                ...previous, contextLength: Number(value || CHAT_CONTEXT_DEFAULT),
              }))}
            />
          </label>
        </div>
      )}
      <div className="jx-firstRun-note">
        <ThunderboltOutlined />
        <span>{t('保存时会实际测试模型连通性，并自动指派给全部对话角色。')}</span>
      </div>
    </div>
  );

  const renderInternet = () => {
    const engine = serviceValues['internet_search.engine'] || 'tavily';
    const keyName = engine === 'baidu'
      ? 'internet_search.baidu_api_key'
      : 'internet_search.tavily_api_key';
    const keyItem = internetGroup?.items.find((item) => item.config_key === keyName);
    return (
      <div className="jx-firstRun-form">
        <Alert
          type="info"
          showIcon
          message={t('这一步可以跳过')}
          description={t('配置后，智能体可以获取实时网络信息；你也可以稍后在设置中完成。')}
        />
        <label className="jx-firstRun-field">
          <Text strong>{t('搜索服务')}</Text>
          <Select
            size="large"
            value={engine}
            options={[
              { value: 'tavily', label: 'Tavily' },
              { value: 'baidu', label: t('百度千帆') },
            ]}
            onChange={(value) => updateServiceValue('internet_search.engine', value)}
          />
        </label>
        <label className="jx-firstRun-field">
          <div className="jx-firstRun-labelRow">
            <Text strong>{engine === 'baidu' ? t('百度搜索 API Key') : 'Tavily API Key'}</Text>
            {configuredSecret(keyItem?.config_value) && <Tag color="success">{t('已配置')}</Tag>}
          </div>
          <Input.Password
            size="large"
            value={serviceValues[keyName] || ''}
            placeholder={configuredSecret(keyItem?.config_value) ? t('留空保留现有密钥') : t('留空跳过')}
            autoComplete="new-password"
            onChange={(event) => updateServiceValue(keyName, event.target.value)}
          />
        </label>
        <Button
          icon={<ThunderboltOutlined />}
          loading={testingGroup === 'internet_search'}
          onClick={() => void handleTestGroup(internetGroup)}
        >
          {t('保存并测试连接')}
        </Button>
      </div>
    );
  };

  const renderParser = () => {
    const parserUrl = parserGroup?.items.find(
      (item) => item.config_key === 'file_parser.api_url',
    );
    return (
      <div className="jx-firstRun-form">
        <Alert
          type="info"
          showIcon
          message={t('PDF 与扫描件需要外部解析服务')}
          description={t('Excel、CSV、PPTX 和文本文件可以直接解析，不填写也能正常使用。')}
        />
        <label className="jx-firstRun-field">
          <Text strong>{t('文件解析 API 地址')}</Text>
          <Input
            size="large"
            value={serviceValues['file_parser.api_url'] || ''}
            placeholder="http://parser.example.com"
            onChange={(event) => updateServiceValue('file_parser.api_url', event.target.value)}
          />
          {parserUrl?.description && <Text type="secondary">{t(parserUrl.description)}</Text>}
        </label>
        <div className="jx-firstRun-inlineFields">
          <label className="jx-firstRun-field">
            <Text strong>{t('解析方式')}</Text>
            <Select
              size="large"
              value={serviceValues['file_parser.parse_method'] || 'auto'}
              options={['auto', 'ocr', 'txt'].map((value) => ({ value, label: value }))}
              onChange={(value) => updateServiceValue('file_parser.parse_method', value)}
            />
          </label>
          <label className="jx-firstRun-field">
            <Text strong>{t('OCR 语言')}</Text>
            <Input
              size="large"
              value={serviceValues['file_parser.lang_list'] || 'ch'}
              onChange={(event) => updateServiceValue('file_parser.lang_list', event.target.value)}
            />
          </label>
        </div>
        <div className="jx-firstRun-switchStrip">
          <span>{t('识别公式')}</span>
          <Switch
            checked={(serviceValues['file_parser.formula_enable'] || 'true') === 'true'}
            onChange={(checked) => updateServiceValue(
              'file_parser.formula_enable', String(checked),
            )}
          />
          <span>{t('识别表格')}</span>
          <Switch
            checked={(serviceValues['file_parser.table_enable'] || 'true') === 'true'}
            onChange={(checked) => updateServiceValue(
              'file_parser.table_enable', String(checked),
            )}
          />
        </div>
        <Button
          icon={<ThunderboltOutlined />}
          loading={testingGroup === 'file_parser'}
          disabled={!serviceValues['file_parser.api_url']}
          onClick={() => void handleTestGroup(parserGroup)}
        >
          {t('保存并测试连接')}
        </Button>
      </div>
    );
  };

  const renderIntelligence = () => (
    <div className="jx-firstRun-featureGrid">
      <div className={`jx-firstRun-feature${memoryEnabled ? ' jx-firstRun-feature--active' : ''}`}>
        <div className="jx-firstRun-featureIcon"><RobotOutlined /></div>
        <div className="jx-firstRun-featureBody">
          <div className="jx-firstRun-labelRow">
            <Text strong>{t('永久记忆')}</Text>
            <Switch
              checked={memoryAvailable && memoryEnabled}
              disabled={!memoryAvailable}
              onChange={setMemoryEnabled}
            />
          </div>
          <Paragraph>{t('跨会话保留你的偏好与背景，让回答逐渐更贴合你的工作方式。')}</Paragraph>
          {!memoryAvailable && <Tag>{t('当前实例未配置记忆服务')}</Tag>}
          {memoryAvailable && memoryEnabled && (
            <label className="jx-firstRun-subSwitch">
              <span>{t('允许对话自动沉淀新记忆')}</span>
              <Switch size="small" checked={memoryWriteEnabled} onChange={setMemoryWriteEnabled} />
            </label>
          )}
        </div>
      </div>
      <div className={`jx-firstRun-feature${ontologyEnabled ? ' jx-firstRun-feature--active' : ''}`}>
        <div className="jx-firstRun-featureIcon"><SafetyCertificateOutlined /></div>
        <div className="jx-firstRun-featureBody">
          <div className="jx-firstRun-labelRow">
            <Text strong>{t('本体核验')}</Text>
            <Switch
              checked={ontologyAvailable && ontologyEnabled}
              disabled={!ontologyAvailable}
              onChange={setOntologyEnabled}
            />
          </div>
          <Paragraph>{t('工具调用和高风险结果会经过领域规则检查，减少不符合业务约束的输出。')}</Paragraph>
          {ontologyAvailable
            ? <Tag color="blue">{t('已发布 {n} 个领域包', { n: activeOntologyCount })}</Tag>
            : <Tag>{t('尚无已发布的领域本体包')}</Tag>}
        </div>
      </div>
    </div>
  );

  const renderFinish = () => (
    <div className="jx-firstRun-finish">
      <div className="jx-firstRun-finishMark"><CheckCircleFilled /></div>
      <Title level={3}>{t('你的工作空间已经准备好了')}</Title>
      <Paragraph>{t('这些设置以后都可以在头像菜单的“设置”中修改。')}</Paragraph>
      <div className="jx-firstRun-summary">
        <div><span>{t('界面语言')}</span><strong>{language === 'en' ? 'English' : '简体中文'}</strong></div>
        <div><span>{t('主模型')}</span><strong>{currentMainProvider?.model_name || modelDraft.modelName || t('已配置')}</strong></div>
        <div><span>{t('永久记忆')}</span><strong>{memoryAvailable && memoryEnabled ? t('开启') : t('关闭')}</strong></div>
        <div><span>{t('本体核验')}</span><strong>{ontologyAvailable && ontologyEnabled ? t('开启') : t('关闭')}</strong></div>
      </div>
    </div>
  );

  const panels = [
    renderLanguage(),
    renderModel(),
    renderInternet(),
    renderParser(),
    renderIntelligence(),
    renderFinish(),
  ];
  const stepDescriptions = [
    ['先选择你最熟悉的语言', '界面、设置和引导说明会使用该语言。'],
    ['连接你的 AI 模型', '这是运行对话和智能体任务所必需的核心服务。'],
    ['让智能体访问实时信息', '配置搜索服务后，天气、新闻和公开资料查询会更加完整。'],
    ['决定如何读取复杂文档', '外部解析服务适合 PDF、扫描件和包含公式的文档。'],
    ['选择需要的智能增强', '你可以控制是否跨对话记忆，以及是否执行领域本体核验。'],
    ['一切就绪', '完成初始化后，你将进入 HugAgentOS 工作台。'],
  ];

  if (loading) {
    return (
      <div className="jx-firstRun-root">
        <div className="jx-firstRun-loading"><Skeleton active paragraph={{ rows: 5 }} /></div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="jx-firstRun-root">
        <div className="jx-firstRun-loading">
          <Alert type="error" showIcon message={t('初始化信息加载失败')} description={loadError} />
          <Button type="primary" onClick={() => void load()}>{t('重试')}</Button>
        </div>
      </div>
    );
  }

  return (
    <div className="jx-firstRun-root">
      <div className="jx-firstRun-aurora jx-firstRun-aurora--one" />
      <div className="jx-firstRun-aurora jx-firstRun-aurora--two" />
      <header className="jx-firstRun-topbar">
        <div className="jx-firstRun-brand">
          <span className="jx-firstRun-brandMark">H</span>
          <span>{brandName}</span>
          <Tag color="blue">CE</Tag>
        </div>
        <Button type="text" icon={<LogoutOutlined />} onClick={() => void doLogout()}>
          {t('退出登录')}
        </Button>
      </header>

      <main className="jx-firstRun-shell">
        <aside className="jx-firstRun-rail">
          <div className="jx-firstRun-railIntro">
            <span>{t('首次设置')}</span>
            <strong>{t('让我们完成最后几项配置')}</strong>
          </div>
          <ol>
            {STEP_META.map((item, index) => (
              <li
                key={item.title}
                className={`${index === step ? 'is-active' : ''}${index < step ? ' is-done' : ''}`}
              >
                <span className="jx-firstRun-stepIcon">
                  {index < step ? <CheckCircleFilled /> : item.icon}
                </span>
                <span>
                  <strong>{t(item.title)}</strong>
                  <small>{t(item.hint)}</small>
                </span>
              </li>
            ))}
          </ol>
          <div className="jx-firstRun-railFooter">
            <span>{t('步骤 {current} / {total}', { current: step + 1, total: STEP_META.length })}</span>
            <div><i style={{ width: `${((step + 1) / STEP_META.length) * 100}%` }} /></div>
          </div>
        </aside>

        <section className="jx-firstRun-card">
          <div className="jx-firstRun-cardHeader">
            <Text className="jx-firstRun-kicker">{t('HugAgentOS 社区版初始化')}</Text>
            <Title level={1}>{t(stepDescriptions[step][0])}</Title>
            <Paragraph>{t(stepDescriptions[step][1])}</Paragraph>
          </div>

          <div className="jx-firstRun-cardBody">
            <AnimatePresence mode="wait" initial={false}>
              <motion.div
                key={step}
                initial={{ opacity: 0, x: 24 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -16 }}
                transition={{ duration: 0.24, ease: [0.16, 1, 0.3, 1] }}
              >
                {panels[step]}
              </motion.div>
            </AnimatePresence>
          </div>

          <footer className="jx-firstRun-actions">
            <Button
              size="large"
              icon={<ArrowLeftOutlined />}
              disabled={step === 0 || busy}
              onClick={() => persistStep(Math.max(0, step - 1))}
            >
              {t('返回')}
            </Button>
            <Space>
              {(step === 2 || step === 3) && <Text type="secondary">{t('不填写即可跳过')}</Text>}
              <Button
                type="primary"
                size="large"
                loading={busy}
                icon={step === STEP_META.length - 1 ? <CheckCircleFilled /> : undefined}
                onClick={() => void handleNext()}
              >
                {step === STEP_META.length - 1 ? t('完成并进入工作台') : t('继续')}
                {step < STEP_META.length - 1 && <ArrowRightOutlined />}
              </Button>
            </Space>
          </footer>
        </section>
      </main>
    </div>
  );
}
