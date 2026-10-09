import { useEffect, useState } from 'react';
import { Button, Form, Input, Modal, Popconfirm, Select, Table, Tabs, Tag, message } from 'antd';
import { getSiteDetail,
  rollbackSite, updateSite, type SiteItem, type SiteVersionItem } from '../../api';
import { EditionSiteVisibilityFields, editionSiteFormValues, editionSiteUpdateFields,
  getSiteVisibilityOptions, type SiteVisibility } from '../../editionSiteVisibility';
import { SitePasswordField } from './SitePasswordField';
import { ApplicationDataPanel } from './ApplicationDataPanel';
import { stablePublicOrigin } from '../../stores/deploymentModeStore';
import { formatSize } from './siteFormatting';
import { t } from '../../i18n';
import { formatDateTime } from '../../utils/date';

/** Site management modal: settings, version rollback, application data and MCP */
export function SiteManageModal({
  site, onClose, onChanged,
}: {
  site: SiteItem;
  onClose: () => void;
  onChanged: (updated: SiteItem) => void;
}) {
  const [form] = Form.useForm();
  const [applicationBusy, setApplicationBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [visibility, setVisibility] = useState<SiteVisibility>(site.visibility);
  const [versions, setVersions] = useState<SiteVersionItem[]>([]);
  const [currentVersion, setCurrentVersion] = useState(site.current_version);
  const [rollingBack, setRollingBack] = useState<number | null>(null);

  useEffect(() => {
    form.setFieldsValue({
      title: site.title, slug: site.slug,
      visibility: site.visibility,
      ...editionSiteFormValues(site),
    });
    void getSiteDetail(site.site_id, site.origin)
      .then((d) => { setVersions([...d.versions].reverse()); setCurrentVersion(d.current_version); })
      .catch(() => {});
    // 依赖用 site_id 而不是整个 site 对象：站点被 onChanged 刷新后（例如刚改完密码）
    // 不该重置表单，否则会冲掉用户没保存的标题/地址改动。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [site.site_id, form]);

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      setSaving(true);
      const updated = await updateSite(site.site_id, {
        title: values.title?.trim(),
        slug: values.slug !== site.slug ? values.slug : undefined,
        visibility: values.visibility,
        ...editionSiteUpdateFields(values.visibility, values),
      }, site.origin);
      onChanged(updated);
      message.success(t('已保存'));
      onClose();
    } catch (e) {
      if (e instanceof Error) message.error(t('保存失败：') + e.message);
    } finally {
      setSaving(false);
    }
  };

  const handleRollback = async (version: number) => {
    setRollingBack(version);
    try {
      const updated = await rollbackSite(site.site_id, version, site.origin);
      setCurrentVersion(updated.current_version);
      onChanged(updated);
      message.success(t('已回滚到版本') + ` v${version}`);
    } catch (e) {
      message.error(t('回滚失败：') + (e as Error).message);
    } finally {
      setRollingBack(null);
    }
  };

  return (
    <Modal
      wrapClassName="jx-sites-manageModal"
      title={`${t('站点管理')} — ${site.title}`}
      open
      onCancel={onClose}
      closable={!applicationBusy}
      maskClosable={!applicationBusy}
      keyboard={!applicationBusy}
      footer={null}
      width={720}
      destroyOnClose
    >
      <Tabs
        items={[
          {
            key: 'settings',
            label: t('设置'),
            children: (
              <Form form={form} layout="vertical">
                {/* whitespace 校验：只敲空格时 required 是满足的（值非空串），
                    过去要等提交后后端拒掉才报「保存失败」——校验放在输入框上。 */}
                <Form.Item
                  name="title"
                  label={t('站点标题')}
                  rules={[
                    { required: true, message: t('请输入站点标题') },
                    { whitespace: true, message: t('站点标题不能只包含空格') },
                  ]}
                >
                  <Input maxLength={200} />
                </Form.Item>
                <Form.Item
                  name="slug"
                  label={t('访问地址')}
                  extra={t('仅支持 3-50 位小写字母、数字、连字符；修改后旧链接会失效')}
                  rules={[
                    { required: true, message: t('请输入访问地址') },
                    { pattern: /^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$/, message: t('格式不正确') },
                  ]}
                >
                  <Input addonBefore={`${stablePublicOrigin()}/site/`} addonAfter="/" />
                </Form.Item>
                <Form.Item name="visibility" label={t('可见性')}>
                  <Select
                    onChange={(v) => setVisibility(v)}
                    options={getSiteVisibilityOptions()}
                  />
                </Form.Item>
                <EditionSiteVisibilityFields visibility={visibility} />
                <Form.Item label={t('访问密码')} extra={t('开启后访客需输入密码才能打开站点')}>
                  <SitePasswordField site={site} onChanged={onChanged} />
                </Form.Item>
                <Button type="primary" onClick={handleSave} loading={saving}>{t('保存')}</Button>
              </Form>
            ),
          },
          {
            key: 'versions',
            label: `${t('版本')} (${versions.length})`,
            children: (
              <Table
                size="small"
                rowKey="version"
                pagination={false}
                scroll={{ x: 620 }}
                dataSource={versions}
                columns={[
                  {
                    title: t('版本'), dataIndex: 'version', width: 100,
                    render: (v: number) => (
                      <span>
                        v{v}{' '}
                        {v === currentVersion ? <Tag color="green">{t('当前线上')}</Tag> : null}
                      </span>
                    ),
                  },
                  { title: t('发布时间'), dataIndex: 'created_at', render: (v: string) => formatDateTime(v, '') },
                  { title: t('文件数'), dataIndex: 'file_count', width: 90 },
                  { title: t('大小'), dataIndex: 'total_size_bytes', width: 100, render: (v: number) => formatSize(v) },
                  {
                    title: '', key: 'op', width: 100,
                    render: (_: unknown, row: SiteVersionItem) =>
                      row.version === currentVersion ? null : (
                        <Popconfirm
                          title={t('回滚站点')}
                          description={t('线上内容将立即切换到该版本，确定回滚？')}
                          okText={t('回滚')}
                          cancelText={t('取消')}
                          onConfirm={() => handleRollback(row.version)}
                        >
                          <Button size="small" loading={rollingBack === row.version}>{t('回滚')}</Button>
                        </Popconfirm>
                      ),
                  },
                ]}
              />
            ),
          },
          {
            key: 'data',
            label: t('数据与 MCP'),
            children: <ApplicationDataPanel siteId={site.site_id} target={site.origin === 'local' ? 'local' : 'cloud'} onBusyChange={setApplicationBusy} />,
          },
        ]}
      />
    </Modal>
  );
}
