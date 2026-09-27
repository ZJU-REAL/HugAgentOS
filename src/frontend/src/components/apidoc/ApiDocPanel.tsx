import { useCallback, useEffect, useMemo, useState } from 'react';
import { motion } from 'motion/react';
import { Button, Empty, Input, Select, Space, Spin, Tag, Tooltip, Typography } from 'antd';
import { FileSearchOutlined, LockOutlined, ReloadOutlined } from '@ant-design/icons';
import { EASE } from '../../utils/motionTokens';
import { t } from '../../i18n';
import { METHOD_OPTIONS, normalizeOpenApi, type OpenApiSchema, type HttpMethod, type GroupBucket } from './apiDocModel';
import { ApiDocDetail } from './ApiDocDetail';
import { MethodTag } from './MethodTag';
import { AuthGuide } from './ApiAuthGuide';
const { Text } = Typography;
const DETAIL_ENTER = {
  initial: { opacity: 0, x: 6 }, animate: { opacity: 1, x: 0 },
  transition: { duration: 0.15, ease: EASE.standard },
} as const;

interface ApiDocPanelProps {
  /** A ref passed in by the parent component, filled with a reload function; lets the Header's "refresh" button trigger a re-fetch of the schema. */
  onReloadRef?: { current: (() => void) | null };
}

export function ApiDocPanel({ onReloadRef }: ApiDocPanelProps = {}) {
  const [spec, setSpec] = useState<OpenApiSchema | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [methods, setMethods] = useState<HttpMethod[]>([]);
  const [activeGroup, setActiveGroup] = useState<string>('');
  const [activeEndpointId, setActiveEndpointId] = useState<string>('');

  const loadSpec = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // /api/openapi.json: reverse-proxied to the backend's /openapi.json via nginx
      // /openapi.json: used when connecting directly to the backend locally
      const candidates = ['/api/openapi.json', '/openapi.json'];
      let data: OpenApiSchema | null = null;
      let lastErr: string | null = null;
      for (const url of candidates) {
        try {
          const res = await fetch(url);
          if (!res.ok) {
            lastErr = `${url}: HTTP ${res.status}`;
            continue;
          }
          data = await res.json();
          break;
        } catch (e: unknown) {
          lastErr = `${url}: ${e instanceof Error ? e.message : String(e)}`;
        }
      }
      if (!data) throw new Error(lastErr || '无法获取 openapi.json');
      setSpec(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSpec();
    if (onReloadRef) onReloadRef.current = loadSpec;
    return () => {
      if (onReloadRef) onReloadRef.current = null;
    };
  }, [loadSpec, onReloadRef]);

  const allEndpoints = useMemo(() => spec ? normalizeOpenApi(spec) : [], [spec]);

  const filteredEndpoints = useMemo(() => {
    const q = search.trim().toLowerCase();
    return allEndpoints.filter(ep => {
      if (methods.length > 0 && !methods.includes(ep.method)) return false;
      if (q) {
        const blob = `${ep.path} ${ep.summary} ${ep.description} ${ep.operationId || ''}`.toLowerCase();
        if (!blob.includes(q)) return false;
      }
      return true;
    });
  }, [allEndpoints, methods, search]);

  const groups = useMemo<GroupBucket[]>(() => {
    const map = new Map<string, GroupBucket>();
    for (const ep of filteredEndpoints) {
      let bucket = map.get(ep.group);
      if (!bucket) {
        bucket = { name: ep.group, order: ep.groupOrder, endpoints: [] };
        map.set(ep.group, bucket);
      }
      bucket.endpoints.push(ep);
    }
    return Array.from(map.values()).sort((a, b) => a.order - b.order);
  }, [filteredEndpoints]);

  useEffect(() => {
    setActiveGroup(prev => {
      if (groups.length === 0) return '';
      if (groups.some(g => g.name === prev)) return prev;
      return groups[0].name;
    });
  }, [groups]);

  const groupEndpoints = useMemo(
    () => groups.find(g => g.name === activeGroup)?.endpoints || [],
    [groups, activeGroup],
  );

  useEffect(() => {
    setActiveEndpointId(prev => {
      if (groupEndpoints.length === 0) return '';
      if (prev && groupEndpoints.some(e => e.id === prev)) return prev;
      return groupEndpoints[0].id;
    });
  }, [groupEndpoints]);

  const activeEndpoint = groupEndpoints.find(e => e.id === activeEndpointId) || null;

  const totalCount = allEndpoints.length;
  const groupCount = useMemo(() => {
    const set = new Set<string>();
    for (const ep of allEndpoints) set.add(ep.group);
    return set.size;
  }, [allEndpoints]);

  if (loading && !spec) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', minHeight: 400 }}>
        <Spin tip={t('加载接口文档…')} size="large" />
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <Empty
          description={
            <Space direction="vertical">
              <Text type="danger">{t('无法加载接口文档')}</Text>
              <Text type="secondary">{error}</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t('请确认后端服务可访问，且 /api/openapi.json 路径可用。')}
              </Text>
              <Button icon={<ReloadOutlined />} onClick={loadSpec}>{t('重试')}</Button>
            </Space>
          }
        />
      </div>
    );
  }

  const components = spec?.components || {};

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', background: 'var(--color-bg-container)' }}>
      {/* Access guide: authentication and call conventions */}
      <AuthGuide />

      {/* Filter bar */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        padding: '12px 16px',
        borderBottom: '1px solid #E3E6EA',
        background: 'var(--color-bg-container)',
        flexShrink: 0,
      }}>
        <Input
          allowClear
          prefix={<FileSearchOutlined />}
          placeholder={t('搜索路径、摘要、描述、operationId')}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ maxWidth: 360 }}
        />
        <Select
          mode="multiple"
          allowClear
          placeholder={t('筛选方法')}
          value={methods}
          onChange={(v) => setMethods(v as HttpMethod[])}
          style={{ minWidth: 240 }}
          options={METHOD_OPTIONS.map(m => ({ value: m, label: m }))}
        />
        <div style={{ marginLeft: 'auto', color: '#808080', fontSize: 13 }}>
          {/* Keyed fade transition when the count changes */}
          <span key={`${totalCount}-${filteredEndpoints.length}`} className="jx-apidoc-countFade">
            {t('总计 {total} 接口 / {groups} 分组', { total: totalCount, groups: groupCount })}
            {filteredEndpoints.length !== totalCount && (
              <> · {t('当前筛选 {n}', { n: filteredEndpoints.length })}</>
            )}
          </span>
        </div>
      </div>

      {/* Three-column body */}
      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        {/* Left: groups */}
        <div style={{
          width: 240,
          borderRight: '1px solid #E3E6EA',
          overflow: 'auto',
          background: 'var(--color-bg-gray)',
          flexShrink: 0,
        }}>
          {groups.map(g => (
            <div
              key={g.name}
              onClick={() => setActiveGroup(g.name)}
              className={`jx-apidoc-navItem jx-apidoc-groupItem${activeGroup === g.name ? ' active' : ''}`}
            >
              <span>{g.name}</span>
              <Tag color={activeGroup === g.name ? 'blue' : 'default'} style={{ margin: 0 }}>
                {g.endpoints.length}
              </Tag>
            </div>
          ))}
          {groups.length === 0 && (
            <div style={{ padding: 24 }}>
              <Empty description={t('无匹配分组')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            </div>
          )}
        </div>

        {/* Middle: endpoints */}
        <div style={{
          width: 420,
          borderRight: '1px solid #E3E6EA',
          overflow: 'auto',
          background: 'var(--color-bg-container)',
          flexShrink: 0,
        }}>
          {groupEndpoints.map(ep => (
            <div
              key={ep.id}
              onClick={() => setActiveEndpointId(ep.id)}
              className={`jx-apidoc-navItem jx-apidoc-epItem${activeEndpointId === ep.id ? ' active' : ''}`}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <MethodTag method={ep.method} />
                <Text style={{
                  fontFamily: 'monospace',
                  fontSize: 12,
                  flex: 1,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}>
                  {ep.path}
                </Text>
                {ep.requiresAuth && (
                  <Tooltip title={t('需要鉴权')}>
                    <LockOutlined style={{ color: '#fa8c16', fontSize: 12 }} />
                  </Tooltip>
                )}
              </div>
              {ep.summary && (
                <div style={{
                  marginTop: 4,
                  marginLeft: 64,
                  color: 'var(--color-text-secondary)',
                  fontSize: 12,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}>
                  {ep.summary}
                </div>
              )}
            </div>
          ))}
          {groupEndpoints.length === 0 && (
            <div style={{ padding: 24 }}>
              <Empty description={t('无匹配接口')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            </div>
          )}
        </div>

        {/* Right: detail */}
        <div style={{ flex: 1, overflow: 'auto', padding: 24, background: 'var(--color-bg-container)', minWidth: 0 }}>
          {activeEndpoint ? (
            /* Detail-switch keyed enter: animation plays only once on the outer container */
            <motion.div key={activeEndpoint.id} {...DETAIL_ENTER}>
              <ApiDocDetail endpoint={activeEndpoint} components={components} />
            </motion.div>
          ) : (
            <Empty description={t('请选择接口查看详情')} />
          )}
        </div>
      </div>
    </div>
  );
}
