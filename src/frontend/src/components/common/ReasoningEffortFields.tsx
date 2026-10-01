import { useState } from 'react';
import { Button, Form, Input, Select, Alert, message } from 'antd';
import { apiRequest } from '../../api';
import { t } from '../../i18n';
import { REASONING_KEYS, type ReasoningKey, type ReasoningProbeResult, reasoningFormValues, effortLabels } from '../../utils/reasoningEffort';


/** Shared by Config and the instance-admin model editor. Values are kept per model. */
export function ReasoningEffortFields({ providerId, detect }: {
  providerId?: string;
  detect?: (body: Record<string, unknown>) => Promise<ReasoningProbeResult>;
}) {
  const form = Form.useFormInstance();
  const [probing, setProbing] = useState(false);
  const [hint, setHint] = useState('');
  const runProbe = async () => {
    const values = form.getFieldsValue(true);
    if (!values.model_name) { message.warning(t('请先填写模型名')); return; }
    const body = {
      provider: values.provider || 'openai_compatible', model_name: values.model_name,
      base_url: values.base_url || '', api_key: values.api_key || '',
      api_protocol: values.api_protocol, provider_id: providerId,
    };
    const snapshot = (v: Record<string, unknown>) => JSON.stringify([
      v.provider, v.model_name, v.base_url, v.api_key, v.api_protocol,
      v.supports_reasoning_effort, v.reasoning_effort_keys, v.reasoning_effort_values, v.default_reasoning_effort,
    ]);
    const fingerprint = snapshot(values);
    setProbing(true);
    setHint('');
    try {
      const result = detect ? await detect(body) : (
        await apiRequest<{ data: ReasoningProbeResult }>('/v1/models/providers/detect-reasoning', {
          method: 'POST', body: JSON.stringify(body),
        })
      ).data;
      const current = form.getFieldsValue(true);
      if (snapshot(current) !== fingerprint) return;
      const range = result.numeric_range
        ? ' ' + t('上游声明的整数范围：{min}–{max}', { min: result.numeric_range[0], max: result.numeric_range[1] }) : '';
      if (!result.levels.length || result.complete === false) {
        setHint(t('未能确认思考档位，请检查连接或手动填写；现有配置未改变。') + range);
        return;
      }
      form.setFieldsValue(reasoningFormValues({
        reasoning_effort_levels: result.levels, default_reasoning_effort: result.default,
      }));
      form.setFieldValue('reasoning_effort_detected', true);
      const evidence = result.source === 'accepted_probe'
        ? t('测试请求已被接受，但上游可能忽略思考参数。')
        : t('档位来自上游明确声明。');
      setHint(evidence + range + ' ' + t('请核对后保存；探测不会自动修改模型配置。'));
    } catch (error) {
      message.error((error as Error).message);
    } finally { setProbing(false); }
  };
  const enabled = Form.useWatch('supports_reasoning_effort', form);
  const keys = (Form.useWatch('reasoning_effort_keys', form) ?? []) as ReasoningKey[];
  if (!enabled) return null;
  return (
    <>
      <Form.Item extra={t('探测可能产生少量模型用量；只回填表单，不自动保存。')}>
        <Button loading={probing} onClick={() => void runProbe()}>{t('自动探测思考档位')}</Button>
      </Form.Item>
      {hint && <Alert type="info" showIcon message={hint} style={{ marginBottom: 12 }} />}
      <Form.Item name="reasoning_effort_keys" label={t('支持的思考档位')}
        rules={[{ required: true, type: 'array', min: 1, message: t('请至少选择一个思考档位') }]}>
        <Select mode="multiple" options={REASONING_KEYS.map((key) => ({ value: key, label: t(effortLabels[key]) }))}
          onChange={(next: ReasoningKey[]) => {
            const values = form.getFieldValue('reasoning_effort_values') ?? {};
            next.forEach((key) => { if (values[key] == null) values[key] = key; });
            const current = form.getFieldValue('default_reasoning_effort');
            form.setFieldsValue({
              reasoning_effort_values: { ...values },
              default_reasoning_effort: next.includes(current) ? current : next[0],
            });
          }} />
      </Form.Item>
      {keys.map((key) => (
        <Form.Item key={key} name={['reasoning_effort_values', key]}
          label={t('思考参数：{level}', { level: t(effortLabels[key]) })}
          rules={[{ required: true, whitespace: true, message: t('请填写上游支持的参数值') }]}
          extra={t('填写上游接受的字符串或正整数；整数将作为数值发送。')}>
          <Input placeholder="low / high / xhigh / max / 50" maxLength={64} />
        </Form.Item>
      ))}
      <Form.Item name="default_reasoning_effort" label={t('默认思考档位')}
        dependencies={['reasoning_effort_keys']}
        rules={[{ required: true }, {
          validator: (_, value) => keys.includes(value)
            ? Promise.resolve() : Promise.reject(new Error(t('默认档位必须属于已启用的档位'))),
        }]}>
        <Select options={keys.map((key) => ({ value: key, label: t(effortLabels[key]) }))} />
      </Form.Item>
    </>
  );
}
