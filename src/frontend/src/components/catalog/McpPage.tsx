import { useCallback, useEffect, useMemo, useState } from 'react';
import { motion } from 'motion/react';
import { Switch, Tag, Input, Typography, Button, Modal, Form, Select, Popconfirm, message, Pagination } from 'antd';
import { t } from '../../i18n';
import { SearchOutlined, LeftOutlined, PlusOutlined, DeleteOutlined } from '@ant-design/icons';
import { useCatalogStore, useAuthStore } from '../../stores';
import { mdToHtml } from '../../utils/markdown';
import { staggerStyle } from '../../utils/motionTokens';
import { DRILL_IN_BACK, DRILL_IN_DETAIL } from '../../utils/motionVariants';
import { usePanelHeader } from '../../hooks/usePageConfig';
import { createMyMcpServer, deleteMyMcpServer } from '../../api';

// Icons all come from the backend catalog API (admin DB custom value → DEFAULT_MCP_ICONS fallback,
// see src/backend/api/routes/v1/admin_mcp_servers.py). Here we only show a first-letter placeholder
// when the API provides no value.
// Number of cards per page in the grid (2-column layout, 6 rows)
const MCP_PAGE_SIZE = 12;

function McpIcon({ id, icon }: { id: string; icon?: string }) {
  if (icon) {
    return (
      <div className="jx-mcp-iconWrap">
        <img src={icon} alt="" className="jx-mcp-iconImg" />
      </div>
    );
  }
  return (
    <div className="jx-mcp-iconWrap jx-mcp-iconFallback">
      <span>{(id || '?')[0].toUpperCase()}</span>
    </div>
  );
}

export function McpPage({ embedded = false, onDetailChange }: { embedded?: boolean; onDetailChange?: (hasDetail: boolean) => void }) {
  const {
    catalog,
    panel,
    panelEntryNonce,
    manageQuery, setManageQuery,
    toggleItem,
  } = useCatalogStore();
  const { title: mcpTitle, subtitle: mcpSubtitle } = usePanelHeader('mcp', {
    title: 'MCP工具库',
    subtitle: '管理 MCP 工具服务，并查看其作用范围与可靠性影响。',
  });

  const fetchCatalog = useCatalogStore((s) => s.fetchCatalog);
  const canAddMcp = useAuthStore((s) => s.authUser?.can_add_mcp === true);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [searchVisible, setSearchVisible] = useState(false);
  const [page, setPage] = useState(1);
  // Distinguish "user-clicked navigation" from "panel reset": only the former plays the list↔detail transition
  const [navDir, setNavDir] = useState<'detail' | 'list' | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [form] = Form.useForm();

  const handleAddMcp = useCallback(async () => {
    const values = await form.validateFields();
    setAdding(true);
    try {
      await createMyMcpServer({
        display_name: values.display_name,
        transport: values.transport,
        url: values.url,
        description: values.description || '',
      });
      message.success(t('已添加'));
      setAddOpen(false);
      form.resetFields();
      await fetchCatalog();
    } catch (e) {
      message.error((e as Error).message || t('添加失败'));
    } finally {
      setAdding(false);
    }
  }, [form, fetchCatalog]);

  const handleDeleteMcp = useCallback(async (id: string) => {
    try {
      await deleteMyMcpServer(id);
      message.success(t('已删除'));
      await fetchCatalog();
    } catch (e) {
      message.error((e as Error).message || t('删除失败'));
    }
  }, [fetchCatalog]);

  const query = manageQuery.trim().toLowerCase();

  const filteredItems = useMemo(() => {
    const arr = catalog.mcp;
    return query ? arr.filter((x) => `${x.id} ${x.name} ${x.desc} ${(x.tags || []).join(' ')}`.toLowerCase().includes(query)) : arr;
  }, [catalog.mcp, query]);
  const totalMcpCount = catalog.mcp.length;

  const pagedItems = useMemo(
    () => filteredItems.slice((page - 1) * MCP_PAGE_SIZE, page * MCP_PAGE_SIZE),
    [filteredItems, page],
  );

  // Return to the first page when the keyword changes
  useEffect(() => { setPage(1); }, [query]);

  // Pull back to the first page when data changes push the page number out of bounds
  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(filteredItems.length / MCP_PAGE_SIZE));
    if (page > maxPage) setPage(1);
  }, [filteredItems.length, page]);

  const selectedItem = useMemo(() => {
    if (!selectedId) return null;
    return catalog.mcp.find((x) => x.id === selectedId) || null;
  }, [selectedId, catalog.mcp]);

  const toggleEnabled = (id: string, enabled: boolean) => {
    void toggleItem('mcp', id, enabled);
  };

  // Notify parent of detail state changes (covers all code paths including useEffect resets)
  useEffect(() => {
    onDetailChange?.(!!selectedId);
  }, [selectedId, onDetailChange]);

  const openDetail = useCallback((id: string) => {
    setNavDir('detail');
    setSelectedId(id);
  }, []);

  const closeDetail = useCallback(() => {
    setNavDir('list');
    setSelectedId(null);
  }, []);

  useEffect(() => {
    if (embedded) return;
    if (panel !== 'mcp') return;
    setSelectedId(null);
    setSearchVisible(false);
  }, [embedded, panel, panelEntryNonce]);

  useEffect(() => {
    if (!embedded) return;
    setSelectedId(null);
    setSearchVisible(false);
  }, [embedded]);

  // ── Detail View ──────────────────────────────────────────────
  if (selectedItem) {
    const version = (selectedItem as any).version || '';
    // ``detail`` is the user-facing user_intro markdown (managed via admin DB
    // + configs/user_intros.py defaults). No frontmatter; render as-is.
    const markdownBody = (selectedItem as any).detail || '';

    return (
      <motion.div
        key="detail"
        className="jx-mcp-detailPage"
        {...(navDir === 'detail' ? DRILL_IN_DETAIL : { initial: false })}
      >
        {/* Sticky header: back + icon + name + tag + toggle */}
        <div className="jx-mcp-stickyHeader">
          <button className="jx-mcp-backBtn jx-mcp-backBtn--inline" onClick={closeDetail}>
            <LeftOutlined style={{ fontSize: 14 }} />
          </button>
          <McpIcon id={selectedItem.id} icon={selectedItem.icon} />
          <span className="jx-mcp-detailName">{selectedItem.name}</span>
          <Tag className="jx-mcp-enabledTag"
            style={selectedItem.enabled
              ? { background: '#DBE9FF', color: '#126DFF', border: 'none' }
              : { background: '#F5F6F7', color: '#B3B3B3', border: 'none' }
            }>
            {selectedItem.enabled ? t('已启用') : t('未启用')}
          </Tag>
          {version && <span className="jx-mcp-version" style={{ marginLeft: 4 }}>v{version}</span>}
          <div style={{ flex: 1 }} />
          <span className="jx-mcp-enableLabel">{t('启用')}</span>
          <Switch
            checked={!!selectedItem.enabled}
            onChange={(v) => toggleEnabled(selectedItem.id, v)}
          />
        </div>

        {/* Scrollable body */}
        <div className="jx-mcp-stickyBody">
          {/* User intro body — single source of truth, managed via admin */}
          <div className="jx-mcp-detailBody">
            {markdownBody ? (
              <div className="jx-md jx-mcp-detailMarkdown" dangerouslySetInnerHTML={{ __html: mdToHtml(markdownBody) }} />
            ) : (
              <Typography.Text type="secondary">{t('暂无介绍')}</Typography.Text>
            )}
          </div>
        </div>
      </motion.div>
    );
  }

  // ── List View ────────────────────────────────────────────────
  return (
    <motion.div
      key="list"
      className="jx-mcp-page"
      {...(navDir === 'list' ? DRILL_IN_BACK : { initial: false })}
    >
      {/* Header */}
      <div className="jx-mcp-header">
        <div>
          <h2 className="jx-mcp-title">
            {mcpTitle}
            <span className="jx-sectionTitleCount">{t('（共 {n} 项）', { n: totalMcpCount })}</span>
          </h2>
          {mcpSubtitle ? <p className="jx-mcp-subtitle">{mcpSubtitle}</p> : null}
        </div>
        <div className="jx-mcp-headerRight">
          {searchVisible ? (
            <Input
              allowClear
              placeholder={t('搜索工具关键词')}
              className="jx-mcp-searchInput"
              value={manageQuery}
              onChange={(e) => setManageQuery(e.target.value)}
              prefix={<SearchOutlined style={{ color: '#B3B3B3' }} />}
              autoFocus
              onBlur={() => { if (!manageQuery) setSearchVisible(false); }}
            />
          ) : (
            <div className="jx-mcp-searchBox" onClick={() => setSearchVisible(true)}>
              <SearchOutlined style={{ color: '#B3B3B3', fontSize: 14 }} />
              <span className="jx-mcp-searchPlaceholder">{t('搜索工具关键词')}</span>
            </div>
          )}
          {canAddMcp && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setAddOpen(true)} style={{ marginLeft: 8 }}>
              {t('添加 MCP')}
            </Button>
          )}
        </div>
      </div>

      {/* Card grid — 2 columns (container key controls stagger replay: replay on entering the panel / paging, no replay on optimistic toggle updates) */}
      <div
        className="jx-mcp-grid jx-anim-stagger"
        style={{ '--stagger-step': '30ms' } as React.CSSProperties}
        key={`mcp-${panelEntryNonce}-${page}`}
      >
        {pagedItems.map((item, idx) => (
          <div
            key={item.id}
            className="jx-mcp-card jx-card-lift"
            style={staggerStyle(idx)}
            onClick={() => openDetail(item.id)}
          >
            <div className="jx-mcp-cardTop">
              <McpIcon id={item.id} icon={item.icon} />
              <div className="jx-mcp-cardNameGroup">
                <span className="jx-mcp-cardName">{item.name}</span>
                <Tag className="jx-mcp-enabledTag"
                  style={item.enabled
                    ? { background: '#DBE9FF', color: '#126DFF', border: 'none' }
                    : { background: '#F5F6F7', color: '#B3B3B3', border: 'none' }
                  }>
                  {item.enabled ? t('已启用') : t('未启用')}
                </Tag>
                {item.owner === 'self' && (
                  <Tag style={{ background: '#EBF2FF', color: '#126DFF', border: 'none' }}>{t('我的')}</Tag>
                )}
              </div>
              {item.owner === 'self' && (
                // Container-level stopPropagation: the Popconfirm confirm button is in a portal, but React synthetic events
                // bubble along the component tree, otherwise clicking "delete" would bubble to the card onClick and jump into the detail view.
                <span style={{ marginLeft: 'auto' }} onClick={(e) => e.stopPropagation()}>
                  <Popconfirm
                    title={t('删除这个私有 MCP？')}
                    okText={t('删除')}
                    cancelText={t('取消')}
                    okButtonProps={{ danger: true }}
                    onConfirm={() => handleDeleteMcp(item.id)}
                  >
                    <Button
                      type="text"
                      size="small"
                      danger
                      icon={<DeleteOutlined />}
                    />
                  </Popconfirm>
                </span>
              )}
            </div>
            <div className="jx-mcp-cardDesc">{item.desc}</div>
          </div>
        ))}
      </div>

      {filteredItems.length === 0 && (
        <div className="jx-anim-fadeIn" style={{ padding: '40px 0', textAlign: 'center' }}>
          <Typography.Text type="secondary">{t('没有匹配的工具')}</Typography.Text>
        </div>
      )}

      {filteredItems.length > MCP_PAGE_SIZE && (
        <div className="jx-mcp-pagination">
          <Pagination
            current={page}
            pageSize={MCP_PAGE_SIZE}
            total={filteredItems.length}
            onChange={setPage}
            showSizeChanger={false}
            size="small"
          />
        </div>
      )}

      {/* Add-private-MCP modal */}
      <Modal
        title={t('添加 MCP 工具')}
        open={addOpen}
        onCancel={() => setAddOpen(false)}
        onOk={() => void handleAddMcp()}
        okText={t('添加 MCP')}
        cancelText={t('取消')}
        confirmLoading={adding}
        destroyOnHidden
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          {t('仅支持远程 HTTP/SSE 类型的 MCP 服务，添加时会自动测试连通性。该工具仅你自己可见可用。')}
        </Typography.Paragraph>
        <Form form={form} layout="vertical" initialValues={{ transport: 'streamable_http' }}>
          <Form.Item name="display_name" label={t('名称')} rules={[{ required: true, message: t('请输入名称') }]}>
            <Input placeholder="如「我的天气服务」" maxLength={255} />
          </Form.Item>
          <Form.Item name="transport" label={t('类型')} rules={[{ required: true }]}>
            <Select
              options={[
                { label: 'Streamable HTTP', value: 'streamable_http' },
                { label: 'SSE', value: 'sse' },
              ]}
            />
          </Form.Item>
          <Form.Item name="url" label={t('服务地址 URL')} rules={[{ required: true, message: t('请输入 URL') }]}>
            <Input placeholder="https://example.com/mcp/" />
          </Form.Item>
          <Form.Item name="description" label={t('描述（可选）')}>
            <Input.TextArea rows={2} maxLength={2000} placeholder={t('简单说明这个工具的用途')} />
          </Form.Item>
        </Form>
      </Modal>
    </motion.div>
  );
}
