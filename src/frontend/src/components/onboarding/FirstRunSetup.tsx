import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  CheckCircleFilled,
  ExclamationCircleFilled,
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
  Modal,
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

type AuxiliaryModelType = 'embedding' | 'reranker';

const AUXILIARY_MODEL_META: Record<AuxiliaryModelType, {
  roleKey: AuxiliaryModelType;
  title: string;
  description: string;
  defaultDisplayName: string;
}> = {
  embedding: {
    roleKey: 'embedding',
    title: '索引模型',
    description: '用于长期记忆和知识库向量检索；配置后初始化向导中的长期记忆开关即可启用。',
    defaultDisplayName: '索引模型',
  },
  reranker: {
    roleKey: 'reranker',
    title: '重排模型',
    description: '用于对检索结果二次排序，提高长期记忆与知识库召回结果的相关性。',
    defaultDisplayName: '重排模型',
  },
};

function createModelDraft(displayName: string): ModelDraft {
  return {
    displayName: t(displayName),
    provider: 'openai_compatible',
    baseUrl: '',
    apiKey: '',
    modelName: '',
    contextLength: CHAT_CONTEXT_DEFAULT,
  };
}

const STEP_META = [
  { title: '语言与区域', hint: '选择界面语言' },
  { title: '模型配置', hint: '连接推理与检索服务' },
  { title: '联网搜索', hint: '可选服务' },
  { title: '文件解析', hint: '可选服务' },
  { title: '智能增强', hint: '记忆与本体' },
  { title: '准备就绪', hint: '检查并进入' },
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
  const loggingOut = useAuthStore((state) => state.loggingOut);
  const [step, setStep] = useState(() => safeStoredStep(user.user_id));
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [testingGroup, setTestingGroup] = useState<string | null>(null);
  const [loadError, setLoadError] = useState('');
  const [submitError, setSubmitError] = useState('');
  const [logoutConfirmOpen, setLogoutConfirmOpen] = useState(false);
  const cardBodyRef = useRef<HTMLDivElement>(null);
  const [language, setLanguage] = useState<Lang>(getLang());
  const [preferences, setPreferences] = useState<UserPreferences>({});
  const [providers, setProviders] = useState<ModelProviderItem[]>([]);
  const [roles, setRoles] = useState<ModelRoleAssignment[]>([]);
  const [schemas, setSchemas] = useState<ProviderSchema[]>([]);
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(null);
  const [creatingModel, setCreatingModel] = useState(false);
  const [modelDraft, setModelDraft] = useState<ModelDraft>(() => createModelDraft('主对话模型'));
  const [selectedAuxProviderIds, setSelectedAuxProviderIds] = useState<
    Record<AuxiliaryModelType, string | null>
  >({ embedding: null, reranker: null });
  const [creatingAuxModels, setCreatingAuxModels] = useState<
    Record<AuxiliaryModelType, boolean>
  >({ embedding: true, reranker: true });
  const [auxModelDrafts, setAuxModelDrafts] = useState<Record<AuxiliaryModelType, ModelDraft>>(() => ({
    embedding: createModelDraft(AUXILIARY_MODEL_META.embedding.defaultDisplayName),
    reranker: createModelDraft(AUXILIARY_MODEL_META.reranker.defaultDisplayName),
  }));
  const [serviceGroups, setServiceGroups] = useState<ServiceConfigGroup[]>([]);
  const [serviceValues, setServiceValues] = useState<Record<string, string>>({});
  const [dirtyServiceKeys, setDirtyServiceKeys] = useState<Record<string, boolean>>({});
  const [memoryEnabled, setMemoryEnabled] = useState(false);
  const [memoryWriteEnabled, setMemoryWriteEnabled] = useState(false);
  const [memoryAvailable, setMemoryAvailable] = useState(false);
  const [embeddingAvailable, setEmbeddingAvailable] = useState(false);
  const [ontologyEnabled, setOntologyEnabled] = useState(false);
  const [ontologyAvailable, setOntologyAvailable] = useState(false);
  const [activeOntologyCount, setActiveOntologyCount] = useState(0);

  const persistStep = useCallback((next: number) => {
    setSubmitError('');
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

      const nextAuxProviderIds = {} as Record<AuxiliaryModelType, string | null>;
      const nextCreatingAuxModels = {} as Record<AuxiliaryModelType, boolean>;
      (Object.keys(AUXILIARY_MODEL_META) as AuxiliaryModelType[]).forEach((providerType) => {
        const role = modelRoles.find(
          (item) => item.role_key === AUXILIARY_MODEL_META[providerType].roleKey,
        );
        const assigned = modelProviders.find(
          (provider) => provider.provider_id === role?.provider_id
            && provider.provider_type === providerType
            && provider.is_active,
        );
        const firstAvailable = modelProviders.find(
          (provider) => provider.provider_type === providerType && provider.is_active,
        );
        nextAuxProviderIds[providerType] = assigned?.provider_id
          || firstAvailable?.provider_id
          || null;
        nextCreatingAuxModels[providerType] = !assigned && !firstAvailable;
      });
      setSelectedAuxProviderIds(nextAuxProviderIds);
      setCreatingAuxModels(nextCreatingAuxModels);

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
      setEmbeddingAvailable(memory.embedding_available);
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

  useEffect(() => {
    cardBodyRef.current?.scrollTo({ top: 0 });
  }, [step]);

  const currentMainProvider = useMemo(() => {
    const mainRole = roles.find((role) => role.role_key === 'main_agent');
    return providers.find(
      (provider) => provider.provider_id === mainRole?.provider_id && provider.is_active,
    ) || null;
  }, [providers, roles]);

  const providerOptionsForType = useCallback(
    (providerType: ModelProviderItem['provider_type']) => schemas
      .filter((schema) => !schema.supports_types || schema.supports_types.includes(providerType))
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

  const saveAuxiliaryModel = async (providerType: AuxiliaryModelType): Promise<string | null> => {
    let providerId = selectedAuxProviderIds[providerType];
    const draft = auxModelDrafts[providerType];
    if (creatingAuxModels[providerType]) {
      const hasDraftValues = Boolean(
        draft.baseUrl.trim() || draft.apiKey.trim() || draft.modelName.trim(),
      );
      if (!hasDraftValues) return null;
      if (!draft.baseUrl.trim() || !draft.modelName.trim()) {
        throw new Error(t('请完整填写{model}的模型地址和模型名', {
          model: t(AUXILIARY_MODEL_META[providerType].title),
        }));
      }
      const created = await createModelProvider({
        display_name: draft.displayName.trim() || draft.modelName.trim(),
        provider_type: providerType,
        provider: draft.provider,
        base_url: draft.baseUrl.trim(),
        api_key: draft.apiKey.trim(),
        model_name: draft.modelName.trim(),
        is_active: true,
      });
      providerId = created.provider_id;
      setProviders((previous) => [created, ...previous]);
      setSelectedAuxProviderIds((previous) => ({
        ...previous,
        [providerType]: created.provider_id,
      }));
      setCreatingAuxModels((previous) => ({
        ...previous,
        [providerType]: false,
      }));
    }
    if (providerId) {
      await assignModelRole(AUXILIARY_MODEL_META[providerType].roleKey, providerId);
    }
    return providerId;
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

    await saveAuxiliaryModel('embedding');
    await saveAuxiliaryModel('reranker');

    const [nextProviders, nextRoles, memory] = await Promise.all([
      listModelProviders(),
      listModelRoles(),
      getMemorySettings(),
    ]);
    setProviders(nextProviders);
    setRoles(nextRoles);
    setMemoryAvailable(memory.mem0_available);
    setEmbeddingAvailable(memory.embedding_available);
    setMemoryEnabled(memory.memory_enabled);
    setMemoryWriteEnabled(memory.memory_write_enabled);

    const nextAuxProviderIds = { ...selectedAuxProviderIds };
    (Object.keys(AUXILIARY_MODEL_META) as AuxiliaryModelType[]).forEach((providerType) => {
      const role = nextRoles.find(
        (item) => item.role_key === AUXILIARY_MODEL_META[providerType].roleKey,
      );
      nextAuxProviderIds[providerType] = role?.provider_id || null;
    });
    setSelectedAuxProviderIds(nextAuxProviderIds);
    setCreatingAuxModels({
      embedding: !nextAuxProviderIds.embedding,
      reranker: !nextAuxProviderIds.reranker,
    });
    void useModelCapabilitiesStore.getState().fetchCapabilities();
  };

  const handleNext = async () => {
    setSubmitError('');
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
          updateMemorySettings(memoryAvailable && embeddingAvailable && memoryEnabled),
          updateMemoryWriteSettings(
            memoryAvailable && embeddingAvailable && memoryEnabled && memoryWriteEnabled,
          ),
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
      setSubmitError((error as Error).message || t('保存失败，请重试'));
    } finally {
      setBusy(false);
    }
  };

  const handleMemoryToggle = (checked: boolean) => {
    if (checked && !embeddingAvailable) {
      message.warning(t('开启记忆前请先配置并分配 embedding 模型'));
      setMemoryEnabled(false);
      return;
    }
    setMemoryEnabled(checked);
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

  const updateAuxModelDraft = (
    providerType: AuxiliaryModelType,
    patch: Partial<ModelDraft>,
  ) => {
    setAuxModelDrafts((previous) => ({
      ...previous,
      [providerType]: { ...previous[providerType], ...patch },
    }));
  };

  const selectAuxProviderSchema = (providerType: AuxiliaryModelType, provider: string) => {
    const schema = schemas.find((item) => item.id === provider);
    updateAuxModelDraft(providerType, {
      provider,
      baseUrl: schema?.autofill_base_url && schema.base_url_template
        ? schema.base_url_template
        : auxModelDrafts[providerType].baseUrl,
    });
  };

  const renderLanguage = () => (
    <div className="jx-firstRun-choiceGrid">
      {([
        { value: 'zh-CN' as Lang, title: '简体中文', desc: '使用简体中文浏览界面与设置' },
        { value: 'en' as Lang, title: 'English', desc: 'Use English for the interface and settings' },
      ]).map((option) => (
        <button
          key={option.value}
          type="button"
          className={`jx-firstRun-choice${language === option.value ? ' jx-firstRun-choice--active' : ''}`}
          aria-pressed={language === option.value}
          onClick={() => setLanguage(option.value)}
        >
          <span className="jx-firstRun-choiceText">
            <strong>{option.title}</strong>
            <span>{option.desc}</span>
          </span>
          <span className="jx-firstRun-choiceControl" aria-hidden="true">
            {language === option.value ? <CheckCircleFilled /> : null}
          </span>
        </button>
      ))}
    </div>
  );

  const renderModelFields = (
    providerType: ModelProviderItem['provider_type'],
    draft: ModelDraft,
    onPatch: (patch: Partial<ModelDraft>) => void,
    onProviderChange: (provider: string) => void,
    includeContext = false,
  ) => {
    const options = providerOptionsForType(providerType);
    return (
      <div className="jx-firstRun-modelGrid">
        <label className="jx-firstRun-field">
          <Text strong>{t('厂商或协议')}</Text>
          <Select
            size="large"
            value={draft.provider}
            options={options.length ? options : [
              { value: 'openai_compatible', label: 'OpenAI Compatible' },
            ]}
            onChange={onProviderChange}
          />
        </label>
        <label className="jx-firstRun-field">
          <Text strong>{t('显示名称')}</Text>
          <Input
            size="large"
            value={draft.displayName}
            onChange={(event) => onPatch({ displayName: event.target.value })}
          />
        </label>
        <label className="jx-firstRun-field jx-firstRun-field--wide">
          <Text strong>{t('模型接口地址')}</Text>
          <Input
            size="large"
            placeholder="https://api.example.com/v1"
            value={draft.baseUrl}
            onChange={(event) => onPatch({ baseUrl: event.target.value })}
          />
        </label>
        <label className="jx-firstRun-field">
          <Text strong>{t('模型名')}</Text>
          <Input
            size="large"
            placeholder="model-name"
            value={draft.modelName}
            onChange={(event) => onPatch({ modelName: event.target.value })}
          />
        </label>
        <label className="jx-firstRun-field">
          <Text strong>API Key</Text>
          <Input.Password
            size="large"
            placeholder="sk-..."
            autoComplete="new-password"
            value={draft.apiKey}
            onChange={(event) => onPatch({ apiKey: event.target.value })}
          />
        </label>
        {includeContext && (
          <label className="jx-firstRun-field">
            <Text strong>{t('上下文窗口')}</Text>
            <InputNumber
              size="large"
              min={1024}
              step={1024}
              value={draft.contextLength}
              onChange={(value) => onPatch({
                contextLength: Number(value || CHAT_CONTEXT_DEFAULT),
              })}
            />
          </label>
        )}
      </div>
    );
  };

  const renderAuxiliaryModel = (providerType: AuxiliaryModelType) => {
    const meta = AUXILIARY_MODEL_META[providerType];
    const activeProviders = providers.filter(
      (provider) => provider.provider_type === providerType && provider.is_active,
    );
    const selected = activeProviders.find(
      (provider) => provider.provider_id === selectedAuxProviderIds[providerType],
    );
    const role = roles.find((item) => item.role_key === meta.roleKey);
    const creating = creatingAuxModels[providerType];
    const draft = auxModelDrafts[providerType];
    return (
      <section className="jx-firstRun-modelSection" key={providerType}>
        <div className="jx-firstRun-modelSectionHeader">
          <div>
            <Space size={8}>
              <Text strong>{t(meta.title)}</Text>
              <Tag>{t('可选')}</Tag>
              {role?.provider_id && <Tag color="success">{t('已配置')}</Tag>}
            </Space>
            <Paragraph>{t(meta.description)}</Paragraph>
          </div>
          {activeProviders.length > 0 && (
            <Button
              type="link"
              onClick={() => setCreatingAuxModels((previous) => ({
                ...previous,
                [providerType]: !previous[providerType],
              }))}
            >
              {creating ? t('返回已有模型') : t('添加新模型')}
            </Button>
          )}
        </div>
        {!creating && activeProviders.length > 0 && (
          <Select
            size="large"
            value={selected?.provider_id}
            placeholder={t('选择已有{model}', { model: t(meta.title) })}
            options={activeProviders.map((provider) => ({
              value: provider.provider_id,
              label: `${provider.display_name} · ${provider.model_name}`,
            }))}
            onChange={(providerId) => setSelectedAuxProviderIds((previous) => ({
              ...previous,
              [providerType]: providerId,
            }))}
          />
        )}
        {creating && renderModelFields(
          providerType,
          draft,
          (patch) => updateAuxModelDraft(providerType, patch),
          (provider) => selectAuxProviderSchema(providerType, provider),
        )}
      </section>
    );
  };

  const renderModel = () => (
    <div className="jx-firstRun-form">
      <section className="jx-firstRun-modelSection jx-firstRun-modelSection--required">
        <div className="jx-firstRun-modelSectionHeader">
          <div>
            <Space size={8}>
              <Text strong>{t('主对话模型')}</Text>
              <Tag color="blue">{t('必填')}</Tag>
            </Space>
            <Paragraph>{t('用于对话、智能体任务和内容生成，保存后会绑定全部对话角色。')}</Paragraph>
          </div>
        </div>
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
        {creatingModel && renderModelFields(
          'chat',
          modelDraft,
          (patch) => setModelDraft((previous) => ({ ...previous, ...patch })),
          selectProviderSchema,
          true,
        )}
      </section>
      {renderAuxiliaryModel('embedding')}
      {renderAuxiliaryModel('reranker')}
      <div className="jx-firstRun-note">
        <ThunderboltOutlined />
        <span>{t('保存时会实际测试已填写模型的连通性并自动指派角色；索引模型和重排模型留空即可跳过。')}</span>
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
      <div className={`jx-firstRun-feature${memoryAvailable && embeddingAvailable && memoryEnabled
        ? ' jx-firstRun-feature--active'
        : ''}`}>
        <div className="jx-firstRun-featureIcon"><RobotOutlined /></div>
        <div className="jx-firstRun-featureBody">
          <div className="jx-firstRun-labelRow">
            <Text strong>{t('永久记忆')}</Text>
            <Switch
              checked={memoryAvailable && embeddingAvailable && memoryEnabled}
              disabled={!memoryAvailable}
              onChange={handleMemoryToggle}
            />
          </div>
          <Paragraph>{t('跨会话保留你的偏好与背景，让回答逐渐更贴合你的工作方式。')}</Paragraph>
          {!memoryAvailable && <Tag>{t('当前实例未配置记忆服务')}</Tag>}
          {memoryAvailable && !embeddingAvailable && (
            <Tag color="warning">
              {t('开启记忆前请先配置并分配 embedding 模型')}
            </Tag>
          )}
          {memoryAvailable && embeddingAvailable && memoryEnabled && (
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
        <div><span>{t('索引模型')}</span><strong>{selectedAuxProviderIds.embedding ? t('已配置') : t('未配置')}</strong></div>
        <div><span>{t('重排模型')}</span><strong>{selectedAuxProviderIds.reranker ? t('已配置') : t('未配置')}</strong></div>
        <div><span>{t('永久记忆')}</span><strong>{memoryAvailable && embeddingAvailable && memoryEnabled ? t('开启') : t('关闭')}</strong></div>
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
    ['连接你的 AI 模型', '主对话模型为必填项；索引模型和重排模型可按需配置。'],
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
      <header className="jx-firstRun-topbar">
        <div className="jx-firstRun-brand">
          <img
            className="jx-firstRun-wordmark"
            src="/home/hugagentos-logo.png"
            alt={brandName}
          />
          <span className="jx-firstRun-editionLabel">Community Edition</span>
        </div>
        <Button type="text" onClick={() => setLogoutConfirmOpen(true)}>
          {t('退出登录')}
        </Button>
      </header>

      <main className="jx-firstRun-shell">
        <aside className="jx-firstRun-rail" aria-label={t('首次设置')}>
          <div className="jx-firstRun-railIntro">
            <span>{t('首次设置')}</span>
            <strong>{t('让我们完成最后几项配置')}</strong>
          </div>
          <ol>
            {STEP_META.map((item, index) => (
              <li
                key={item.title}
                className={`${index === step ? 'is-active' : ''}${index < step ? ' is-done' : ''}`}
                aria-current={index === step ? 'step' : undefined}
              >
                <span className="jx-firstRun-stepIndex" aria-hidden="true">
                  {index < step ? <CheckCircleFilled /> : String(index + 1).padStart(2, '0')}
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
            <div
              role="progressbar"
              aria-valuemin={1}
              aria-valuemax={STEP_META.length}
              aria-valuenow={step + 1}
            >
              <i style={{ width: `${((step + 1) / STEP_META.length) * 100}%` }} />
            </div>
          </div>
        </aside>

        <section className="jx-firstRun-card">
          <div className="jx-firstRun-cardHeader">
            <Text className="jx-firstRun-kicker">
              {t('步骤 {current} / {total}', { current: step + 1, total: STEP_META.length })}
            </Text>
            <Title level={1}>{t(stepDescriptions[step][0])}</Title>
            <Paragraph>{t(stepDescriptions[step][1])}</Paragraph>
          </div>

          <div ref={cardBodyRef} className="jx-firstRun-cardBody">
            <AnimatePresence mode="wait" initial={false}>
              <motion.div
                key={step}
                initial={{ opacity: 0, x: 12 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -8 }}
                transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
              >
                {panels[step]}
              </motion.div>
            </AnimatePresence>
          </div>

          {submitError && (
            <Alert
              className="jx-firstRun-submitError"
              type="error"
              showIcon
              closable
              message={t('无法继续')}
              description={submitError}
              onClose={() => setSubmitError('')}
            />
          )}

          <footer className="jx-firstRun-actions">
            <Button
              type="text"
              size="large"
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
                className="jx-firstRun-primaryButton"
                loading={busy}
                icon={step === STEP_META.length - 1 ? <CheckCircleFilled /> : undefined}
                onClick={() => void handleNext()}
              >
                {step === STEP_META.length - 1 ? t('完成并进入工作台') : t('继续')}
              </Button>
            </Space>
          </footer>
        </section>
      </main>

      <Modal
        title={<Space><ExclamationCircleFilled style={{ color: '#F8AB42' }} />{t('确认退出登录？')}</Space>}
        open={logoutConfirmOpen}
        okText={t('退出登录')}
        cancelText={t('取消')}
        okButtonProps={{ danger: true }}
        confirmLoading={loggingOut}
        maskClosable={!loggingOut}
        closable={!loggingOut}
        onCancel={() => setLogoutConfirmOpen(false)}
        onOk={() => void doLogout()}
      >
        {t('退出登录不会丢失任何数据，你仍可以登录此账号。')}
      </Modal>
    </div>
  );
}
