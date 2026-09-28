import { sourceLabel } from './pluginLabels';
import { motion } from 'motion/react';
import { Switch, Tag, Typography, Button, Popconfirm, Alert } from 'antd';
import { LeftOutlined, DeleteOutlined, BulbOutlined, ApiOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { stripMarkdown, mdToHtml } from '../../utils/markdown';
import { staggerStyle } from '../../utils/motionTokens';
import { DRILL_IN_DETAIL } from '../../utils/motionVariants';
import { DingTalkConnect } from '../settings/DingTalkConnect';
import { LarkConnect } from '../settings/LarkConnect';
import { EmailConnect } from '../settings/EmailConnect';
import { YidaConnect } from '../settings/YidaConnect';
import { LarkAppInitCard } from './LarkAppInitCard';
import { PluginAvatar } from './PluginIconPicker';
import type { InstalledPluginDetail, PluginSkillComponent, PluginMcpComponent } from '../../types';
export type SelectedComponent = { kind: 'skill'; data: PluginSkillComponent } | { kind: 'mcp'; data: PluginMcpComponent };
type NavDir = 'detail' | 'list' | null;

export function PluginIcon({ icon, size = 28, round = true }: { icon?: string | null; size?: number; round?: boolean }) {
  return <PluginAvatar icon={icon} size={size} round={round} />;
}

export function PluginComponentDetail({component, navDir, backToPlugin, setToolDetail}: {
  component: SelectedComponent; navDir: NavDir; backToPlugin: () => void;
  setToolDetail: (tool: { name: string; description?: string }) => void;
}) {
    const isSkill = component.kind === 'skill';
    const name = component.data.name;
    return (
      <motion.div key="component" className="jx-mcp-detailPage" {...(navDir === 'detail' ? DRILL_IN_DETAIL : { initial: false })}>
        <div className="jx-mcp-stickyHeader">
          <button className="jx-mcp-backBtn jx-mcp-backBtn--inline" onClick={backToPlugin}>
            <LeftOutlined style={{ fontSize: 14 }} />
          </button>
          <div className="jx-mcp-iconWrap jx-mcp-iconFallback">
            {isSkill ? <BulbOutlined style={{ color: 'var(--color-warning)' }} /> : <ApiOutlined style={{ color: 'var(--color-success)' }} />}
          </div>
          <span className="jx-mcp-detailName">{name}</span>
          <Tag style={component.data.enabled
            ? { background: 'var(--color-primary-bg)', color: 'var(--color-primary)', border: 'none' }
            : { background: 'var(--color-bg-gray)', color: 'var(--color-text-placeholder)', border: 'none' }}>
            {component.data.enabled ? t('已启用') : t('未启用')}
          </Tag>
          <div style={{ flex: 1 }} />
          <Tag>{isSkill ? t('技能') : 'MCP'}</Tag>
        </div>

        <div className="jx-mcp-stickyBody">
          <div className="jx-sk-metaCard">
            <p className="jx-sk-metaDesc">{stripMarkdown(component.data.description) || t('暂无描述')}</p>
          </div>

          {component.kind === 'skill' ? (
            (() => {
              const sk = component.data;
              return (
                <div className="jx-mcp-detailBody">
                  {sk.instructions ? (
                    <div className="jx-md jx-mcp-detailMarkdown"
                      dangerouslySetInnerHTML={{ __html: mdToHtml(sk.instructions) }} />
                  ) : <Typography.Text type="secondary">{t('暂无指令内容')}</Typography.Text>}
                  {sk.files.length > 0 && (
                    <div style={{ marginTop: 16 }}>
                      <h4>{t('附带文件')}</h4>
                      <ul style={{ paddingLeft: 20, color: 'var(--color-text-secondary)', fontSize: 13 }}>
                        {sk.files.map((f) => <li key={f}>{f}</li>)}
                      </ul>
                    </div>
                  )}
                </div>
              );
            })()
          ) : (
            (() => {
              const mc = component.data;
              return (
                <div className="jx-mcp-detailBody">
                  <h4>{t('工具列表')}（{mc.tools.length}）</h4>
                  {mc.needs_runtime && (
                    <Typography.Paragraph type="warning" style={{ fontSize: 13 }}>
                      {t('该 MCP 为 stdio 类型，需运行时环境，已安装但默认禁用。')}
                    </Typography.Paragraph>
                  )}
                  {mc.tools.length === 0 ? (
                    <Typography.Text type="secondary">{t('暂未发现工具（可能尚未连接）')}</Typography.Text>
                  ) : (
                    <div className="jx-mcp-grid" style={{ marginTop: 8 }}>
                      {mc.tools.map((tool) => (
                        <div key={tool.name} className="jx-mcp-card jx-card-lift" style={{ cursor: 'pointer' }}
                          onClick={() => setToolDetail(tool)}>
                          <div className="jx-mcp-cardTop">
                            <ApiOutlined style={{ color: 'var(--color-success)' }} />
                            <span className="jx-mcp-cardName" style={{ marginLeft: 8 }}>{tool.name}</span>
                          </div>
                          <div className="jx-mcp-cardDesc">{stripMarkdown(tool.description) || t('暂无描述')}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })()
          )}
        </div>
      </motion.div>
    );

}

export function PluginDetailView({pluginDetail, navDir, backToList, isCE, handleToggle, handleUninstall, openComponent}: {
  pluginDetail: InstalledPluginDetail; navDir: NavDir; backToList: () => void; isCE: boolean;
  handleToggle: (id: string, enabled: boolean) => Promise<void>;
  handleUninstall: (id: string, name: string) => Promise<void>;
  openComponent: (component: SelectedComponent) => void;
}) {
    const d = pluginDetail;
    const skills: PluginSkillComponent[] = d.skills;
    const mcps: PluginMcpComponent[] = d.mcp;
    const srcLabel = sourceLabel(d.source);
    const dropped = d.import_report?.dropped;
    const isInstalled = true;

    return (
      <motion.div key="plugin" className="jx-mcp-detailPage" {...(navDir === 'detail' ? DRILL_IN_DETAIL : { initial: false })}>
        <div className="jx-mcp-stickyHeader">
          <button className="jx-mcp-backBtn jx-mcp-backBtn--inline" onClick={backToList}>
            <LeftOutlined style={{ fontSize: 14 }} />
          </button>
          <PluginIcon icon={d.icon} size={28} />
          <span className="jx-mcp-detailName">{d.name}</span>
          <span className="jx-mcp-version" style={{ marginLeft: 4 }}>v{d.version}</span>
          {srcLabel && <Tag color="purple" style={{ marginLeft: 6 }}>{srcLabel}</Tag>}
          <div style={{ flex: 1 }} />
          {!isCE && d.is_global && <Tag color="gold" style={{ marginRight: 8 }}>{t('管理员')}</Tag>}
          <span className="jx-mcp-enableLabel">{t('启用')}</span>
          <Switch
            checked={d.skills.some((s) => s.enabled) || d.mcp.some((m) => m.enabled)}
            onChange={(v) => void handleToggle(d.install_id, v)}
            style={{ marginRight: 8 }}
          />
          {/* Global plugins are managed by the admin; users can't uninstall them (only disable for themselves) */}
          {!d.is_global && (
            <Popconfirm title={t('确定卸载该插件？其技能与 MCP 将一并移除')}
              onConfirm={() => void handleUninstall(d.install_id, d.name)}>
              <Button danger size="small" icon={<DeleteOutlined />}>{t('卸载')}</Button>
            </Popconfirm>
          )}
        </div>

        <div className="jx-mcp-stickyBody">
          <div className="jx-sk-metaCard">
            <h4 className="jx-sk-metaName">{d.name}</h4>
            <p className="jx-sk-metaDesc">{stripMarkdown(d.description) || t('暂无描述')}</p>
            {d.category && <Tag>{d.category}</Tag>}
          </div>

          {d.slug === 'feishu-cli' && <LarkAppInitCard />}

          {/* Account connection (per-user OAuth device flow): when a plugin declares a connection, the one-time authorization is completed here.
              It used to be under "Settings → Integrations"; now it's consolidated into the corresponding plugin detail page. */}
          {d.connection && (
            <div style={{ marginTop: 12 }}>
              <h4 className="jx-sectionTitle">{t('账号连接')}</h4>
              <div className="jx-settings-card">
                {d.connection === 'dingtalk' && <DingTalkConnect />}
                {d.connection === 'lark' && <LarkConnect />}
                {d.connection === 'email' && <EmailConnect />}
                {d.connection === 'yida' && <YidaConnect />}
              </div>
            </div>
          )}

          {/* Admin config (read-only on the user side: only see whether it's provisioned, can't modify; if not configured, prompt to contact the admin) */}
          {d.admin_config && (
            <div style={{ marginTop: 12 }}>
              <h4 className="jx-sectionTitle">{t('管理员配置')}</h4>
              {d.admin_config.configured ? (
                <Alert type="success" showIcon
                  message={t('该插件已由管理员配置，可直接使用。')} />
              ) : (
                <Alert type="warning" showIcon
                  message={t('该插件需要管理员配置后才能使用')}
                  description={t('请联系管理员在「插件」中为本插件开通相关配置（{items}）。', {
                    items: d.admin_config.fields.map((f) => f.label).join('、'),
                  })} />
              )}
            </div>
          )}

          {/* Skill components */}
          <div style={{ marginTop: 12 }}>
            <h4 className="jx-sectionTitle">{t('技能')}（{skills.length}）</h4>
            {skills.length === 0 ? <Typography.Text type="secondary">{t('该插件不含技能')}</Typography.Text> : (
              <div className="jx-mcp-grid">
                {skills.map((s, idx) => (
                  <ComponentCard
                    key={s.skill_id || s.name}
                    idx={idx}
                    icon={<BulbOutlined style={{ color: 'var(--color-warning)' }} />}
                    name={s.name}
                    desc={stripMarkdown(s.description) || t('点击查看详情')}
                    onClick={() => openComponent({ kind: 'skill', data: s })}
                    tags={isInstalled && (
                      <Tag style={{ marginLeft: 8, ...(s.enabled
                        ? { background: 'var(--color-primary-bg)', color: 'var(--color-primary)', border: 'none' }
                        : { background: 'var(--color-bg-gray)', color: 'var(--color-text-placeholder)', border: 'none' }) }}>
                        {s.enabled ? t('已启用') : t('未启用')}
                      </Tag>
                    )}
                  />
                ))}
              </div>
            )}
          </div>

          {/* MCP components */}
          {mcps.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <h4 className="jx-sectionTitle">{t('连接器')}（{mcps.length}）</h4>
              <div className="jx-mcp-grid">
                {mcps.map((m, idx) => (
                  <ComponentCard
                    key={m.server_id || m.name}
                    idx={idx}
                    icon={<ApiOutlined style={{ color: 'var(--color-success)' }} />}
                    name={m.name}
                    desc={isInstalled ? t('{n} 个工具', { n: m.tools?.length ?? 0 }) : (m.note || t('点击查看详情'))}
                    onClick={() => openComponent({ kind: 'mcp', data: m })}
                    tags={<>
                      <Tag style={{ marginLeft: 8 }}>{m.transport === 'stdio' ? 'stdio' : '远程'}</Tag>
                      {m.needs_runtime && <Tag color="orange" style={{ marginLeft: 4 }}>{t('需运行时')}</Tag>}
                    </>}
                  />
                ))}
              </div>
            </div>
          )}

          {/* Not-imported components */}
          {dropped && dropped.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <h4 className="jx-sectionTitle">{t('未导入组件')}（{dropped.length}）</h4>
              <ul style={{ paddingLeft: 20, color: 'var(--color-text-tertiary)', fontSize: 13 }}>
                {dropped.map((x, i) => <li key={i}>{x.type}/{x.name} — {x.reason}</li>)}
              </ul>
            </div>
          )}
        </div>
      </motion.div>
    );

}

// Skill/MCP component card inside plugin details (shared card skeleton; tags are injected by the caller per type).
function ComponentCard({ idx, icon, name, tags, desc, onClick }: {
  idx: number;
  icon: React.ReactNode;
  name: string;
  tags?: React.ReactNode;
  desc: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <div className="jx-mcp-card jx-card-lift" style={staggerStyle(idx)} onClick={onClick}>
      <div className="jx-mcp-cardTop">
        {icon}
        <span className="jx-mcp-cardName" style={{ marginLeft: 8 }}>{name}</span>
        {tags}
      </div>
      <div className="jx-mcp-cardDesc">{desc}</div>
    </div>
  );
}
