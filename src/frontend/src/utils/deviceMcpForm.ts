import type { DeviceLocalMcpSpec } from '../api';
import { t } from '../i18n';

export interface DeviceMcpFormValues {
  server_id: string; displayName?: string; description?: string;
  transport: 'stdio' | 'streamable_http' | 'sse';
  command?: string; args_text?: string; cwd?: string; env_text?: string;
  url?: string; secret_headers_text?: string;
}
function stringMap(text: string | undefined): Record<string, string> {
  if (!text?.trim()) return {};
  const data: unknown = JSON.parse(text);
  if (!data || typeof data !== 'object' || Array.isArray(data) || Object.values(data).some((value) => typeof value !== 'string')) {
    throw new Error(t('请填写键和值均为文本的 JSON 对象'));
  }
  return data as Record<string, string>;
}
export function deviceMcpSpec(values: DeviceMcpFormValues, enabled = true): DeviceLocalMcpSpec {
  const spec: DeviceLocalMcpSpec = { transport: values.transport, displayName: values.displayName,
    description: values.description, enabled };
  if (values.transport === 'stdio') {
    if (!values.command?.trim()) throw new Error(t('请填写启动命令'));
    spec.command = values.command.trim();
    spec.args = values.args_text?.split('\n').map((arg) => arg.trim()).filter(Boolean) || [];
    spec.cwd = values.cwd?.trim() || undefined;
    spec.env = stringMap(values.env_text);
  } else {
    if (!/^https?:\/\//.test(values.url || '')) throw new Error(t('请填写 HTTP 或 HTTPS 地址'));
    spec.url = values.url;
    if (values.secret_headers_text?.trim()) spec.secret_headers = stringMap(values.secret_headers_text);
  }
  return spec;
}
