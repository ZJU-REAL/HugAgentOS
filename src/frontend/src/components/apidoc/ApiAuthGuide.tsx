import { Collapse, Space, Tag, Typography } from 'antd';
import { KeyOutlined, LockOutlined } from '@ant-design/icons';
import { CopyButton } from '../common/CopyButton';
import { t } from '../../i18n';
const { Text, Paragraph } = Typography;

function CodeLine({ children }: { children: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
      <pre style={{
        flex: 1,
        background: 'var(--color-bg-layout)',
        color: 'var(--color-text)',
        padding: '8px 12px',
        borderRadius: 4,
        fontSize: 12,
        margin: 0,
        overflow: 'auto',
        whiteSpace: 'pre',
      }}>{children}</pre>
      <CopyButton size="small" text={children} />
    </div>
  );
}

/** Top access guide: explains API-Key auth, 401 behavior and the unified response envelope — the OpenAPI schema does not carry these, so they are hand-written. */
export function AuthGuide() {
  return (
    <Collapse
      defaultActiveKey={['guide']}
      style={{ margin: '12px 16px 0', background: 'var(--color-primary-light)', border: '1px solid var(--color-border)' }}
      items={[{
        key: 'guide',
        label: (
          <Space>
            <KeyOutlined style={{ color: 'var(--color-primary)' }} />
            <Text strong>{t('接入指南 · 认证与调用约定')}</Text>
          </Space>
        ),
        children: (
          <div style={{ fontSize: 13, color: 'var(--color-text-secondary)' }}>
            <Paragraph style={{ marginBottom: 8 }}>
              所有 <Text code>/v1/*</Text> 接口都需要身份认证。支持两种方式，二者皆无（或无效）时返回 <Tag color="orange">401</Tag>，
              <b>绝不会匿名放行</b>。
            </Paragraph>

            <Text strong>{t('方式一 · API-Key（推荐，用于程序化 / 外部调用）')}</Text>
            <Paragraph style={{ marginTop: 4, marginBottom: 4 }}>
              在 <Text strong>「设置 → API-Key」</Text> 创建。明文形如 <Text code>sk-jx-xxxxxxxx</Text>，
              {t('请妥善保存，可在密钥列表按需再次复制。调用时放入请求头：')}
            </Paragraph>
            <CodeLine>{`Authorization: Bearer sk-jx-<YOUR_API_KEY>`}</CodeLine>
            <Paragraph type="secondary" style={{ marginTop: 6, marginBottom: 12, fontSize: 12 }}>
              Key 以调用者的用户身份执行，继承其全部能力（技能 / MCP / 知识库 / 项目权限等）。
              撤销、禁用或过期的 Key 会被拒绝（<Text code>code: 30002</Text>）。需管理员开通 <Text code>can_use_api_key</Text> 权限位。
            </Paragraph>

            <Paragraph type="secondary">
              {t('子智能体专属 Key 在智能体详情的 API-Key 弹窗创建，仅能访问绑定智能体及该 Key 的 API 会话。')}
            </Paragraph>
            <Text strong>{t('方式二 · 会话 Cookie')}</Text>
            <Paragraph style={{ marginTop: 4, marginBottom: 12 }}>
              浏览器 SSO 登录后自动携带 <Text code>jx_session</Text>，前端页面走此方式，无需手动设置。
            </Paragraph>

            <Text strong>{t('完整调用示例')}</Text>
            <CodeLine>{`curl -N -X POST 'https://<HOST>/api/v1/agents/responses' \\
  -H 'Authorization: Bearer sk-jx-<YOUR_API_KEY>' \\
  -H 'Content-Type: application/json' \\
  -d '{"chat_id":"demo","message":"你好","model_name":"qwen","stream":true}'`}</CodeLine>

            <Paragraph>{t('同一接口通过 stream: true 返回 SSE，通过 stream: false 直接返回 ChatResponse JSON，回复文本位于 response 字段。')}</Paragraph>
            <Paragraph style={{ marginTop: 12, marginBottom: 4 }}>
              <Text strong>{t('响应信封')}</Text>：{t('密钥管理和调用记录等管理接口使用以下响应信封；智能体回复接口直接返回 ChatResponse，不包含 data 包装。')}
            </Paragraph>
            <CodeLine>{`{ "code": 10000, "message": "Success", "data": { ... }, "trace_id": "...", "timestamp": 0 }`}</CodeLine>

            <Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0, fontSize: 12 }}>
              说明：浏览器实际请求带 <Text code>/api</Text> 前缀（经 nginx 反代到后端）。下方接口列表中标注
              <Tag color="orange" icon={<LockOutlined />} style={{ margin: '0 4px' }}>{t('需要鉴权')}</Tag>
              的均需携带上述任一凭证。
            </Paragraph>
          </div>
        ),
      }]}
    />
  );
}
