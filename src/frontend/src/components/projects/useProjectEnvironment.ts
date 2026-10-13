import { useCallback, useEffect, useRef, useState } from 'react';
import {
  getEnvironmentOptions, previewEnvironment,
  type EnvironmentField, type EnvironmentOptions, type EnvironmentPreview,
  type ProjectEnvironment,
} from '../../api/projectEnvironment';
import { t } from '../../i18n';

export function useProjectEnvironment(
  value: ProjectEnvironment,
  onChange: (value: ProjectEnvironment) => void,
  projectId?: string,
) {
  const [options, setOptions] = useState<EnvironmentOptions | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [resolved, setResolved] = useState<{
    key: string; preview?: EnvironmentPreview; error?: string;
  } | null>(null);
  const [retry, setRetry] = useState(0);
  const generation = useRef(0);
  const valueRef = useRef(value);
  valueRef.current = value;
  const configKey = JSON.stringify(value);
  const previewKey = JSON.stringify([projectId, configKey, retry]);
  const preview = resolved?.key === previewKey ? resolved.preview : undefined;
  const previewError = resolved?.key === previewKey ? resolved.error || '' : '';

  const load = useCallback(async () => {
    const id = ++generation.current;
    setLoading(true);
    setError('');
    try {
      const result = await getEnvironmentOptions(projectId);
      if (id !== generation.current) return null;
      setOptions(result);
      return result;
    } catch (err) {
      if (id === generation.current) setError((err as Error).message || t('加载资源失败'));
      return null;
    } finally {
      if (id === generation.current) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (value.mode === 'custom') void load();
    return () => { generation.current += 1; };
  }, [load, value.mode]);

  useEffect(() => {
    if (value.mode === 'all') return;
    let active = true;
    const timer = window.setTimeout(() => {
      void previewEnvironment(JSON.parse(configKey), projectId).then(result => {
        if (active) setResolved({ key: previewKey, preview: result });
      }).catch(err => {
        if (active) setResolved({ key: previewKey, error: err.message || t('检查依赖失败') });
      });
    }, 250);
    return () => { active = false; window.clearTimeout(timer); };
  }, [value.mode, configKey, previewKey, projectId]);

  const installed = async (field: EnvironmentField, id?: string) => {
    const previous = new Set(options?.[field].map(item => item.id) || []);
    const updated = await load();
    if (!updated) return;
    const added = id ? [id] : updated[field].filter(item => !previous.has(item.id)).map(item => item.id);
    const current = valueRef.current;
    const next = { ...current, [field]: [...new Set([...current[field], ...added])] };
    valueRef.current = next;
    onChange(next);
  };

  const ready = value.mode === 'all' || (
    !loading && !error && !previewError && !!options && !!preview
  );
  return {
    options, loading, error, preview, previewError, ready, load, installed,
    retryPreview: () => setRetry(v => v + 1),
  };
}
