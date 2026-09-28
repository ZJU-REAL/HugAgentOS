import { Modal, Typography, Form, Input, Select, Button, Popconfirm, Tag } from 'antd';
import { UploadOutlined, FileTextOutlined, EditOutlined, DeleteOutlined, PlusOutlined, SaveOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { SkillIconPicker } from './SkillIconPicker';
import { OntologyTagSelect } from '../common/OntologyTagSelect';
import { OntologyBuildValidationModal } from '../common/OntologyBuildValidationModal';
import type { useSkillEditor } from './useSkillEditor';
export function SkillEditorDialogs({ editor }: { editor: ReturnType<typeof useSkillEditor> }) {
  const { handwriteOpen, setHandwriteOpen, creatingSkill, editingSkillId, setEditingSkillId, loadingSkill, skillIcon, setSkillIcon, skillForm, ontologyTagOptions, ontologyTagsLoading, buildFailure, setBuildFailure, skillMcpOptions, skillFiles, editingFileName, setEditingFileName, editingFileContent, setEditingFileContent, fileLoading, fileSaving, newSkillFileName, setNewSkillFileName, skillFileInputRef, resetSkillFileState, openSkillFile, handleSaveSkillFile, handleDeleteSkillFile, handleUploadSkillFile, handleCreateSkillFile, handleCreateSkill } = editor;
  return <>
      {/* Hand-write new / edit skill modal */}
      <Modal
        title={editingSkillId ? t('编辑技能') : t('手写新建技能')}
        open={handwriteOpen}
        onCancel={() => { setHandwriteOpen(false); setEditingSkillId(null); resetSkillFileState(); }}
        onOk={() => void handleCreateSkill()}
        okText={editingSkillId ? t('保存') : t('创建')}
        cancelText={t('取消')}
        confirmLoading={creatingSkill}
        okButtonProps={{ disabled: loadingSkill }}
        width={640}
        destroyOnHidden
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          {editingSkillId
            ? t('修改技能信息后保存。技能 ID 不可更改，该技能仅你自己可见可用。')
            : t('直接填写技能信息即可创建，无需打包 zip。该技能仅你自己可见可用。')}
        </Typography.Paragraph>
        <Form form={skillForm} layout="vertical">
          <Form.Item label={t('图标')}>
            <SkillIconPicker
              value={skillIcon}
              name={skillForm.getFieldValue('display_name')}
              seed={editingSkillId || skillForm.getFieldValue('name') || ''}
              onChange={setSkillIcon}
            />
          </Form.Item>
          <Form.Item
            name="name"
            label={t('技能 ID')}
            rules={[
              { required: true, message: t('请输入技能 ID') },
              { pattern: /^[a-z0-9_-]{1,63}$/, message: t('仅小写字母、数字、- 和 _，最长 63 位') },
            ]}
          >
            <Input placeholder="如 my-weather-helper" disabled={!!editingSkillId} />
          </Form.Item>
          <Form.Item name="display_name" label={t('名称')} rules={[{ required: true, message: t('请输入名称') }]}>
            <Input placeholder={t('名称')} maxLength={255} />
          </Form.Item>
          <Form.Item
            name="description"
            label={t('一句话描述')}
            rules={[{ required: true, message: t('请输入描述，描述为空时技能无法被识别调用') }]}
            tooltip={t('描述是技能被智能体识别、检索的依据，不能为空')}
          >
            <Input placeholder={t('这个技能是做什么的、什么时候用')} maxLength={2000} />
          </Form.Item>
          <Form.Item
            name="tags"
            label={t('普通标签（可选）')}
          >
            <Select mode="tags" placeholder={t('回车添加标签')} tokenSeparators={[',']} />
          </Form.Item>
          <Form.Item
            name="ontology_tags"
            label={t('本体治理标签')}
            tooltip={t('标签来自当前激活领域包；实际调用技能时，会触发标签关联的本体工作流和评审级别。')}
          >
            <OntologyTagSelect options={ontologyTagOptions} loading={ontologyTagsLoading} />
          </Form.Item>
          <Form.Item
            name="mcp_server_ids"
            label={t('绑定工具 (MCP)')}
            tooltip={t('选择技能执行时依赖的 MCP。系统会读取 MCP 的实际工具清单，用于本体构建校验。')}
            extra={t('如果本体流程要求特定工具，请在这里绑定对应 MCP；绑定信息和工具清单会写入 SKILL.md。')}
          >
            <Select
              mode="multiple"
              allowClear
              showSearch
              optionFilterProp="label"
              maxTagCount="responsive"
              placeholder={t('选择可用的连接器')}
              options={skillMcpOptions}
            />
          </Form.Item>
          <Form.Item name="instructions" label={t('技能正文（指令）')} rules={[{ required: true, message: t('请输入技能正文') }]}>
            <Input.TextArea rows={10} placeholder={t('用 Markdown 写清楚这个技能怎么用、步骤、注意事项……')} />
          </Form.Item>
        </Form>

        {/* Skill file management —— shown only when editing an existing skill (for new skills, create first, then add files) */}
        {editingSkillId && (
          <div style={{ marginTop: 8, borderTop: '1px solid var(--color-border, #E3E6EA)', paddingTop: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
              <Typography.Text strong>{t('技能文件（{n}）', { n: skillFiles.length })}</Typography.Text>
              <div style={{ display: 'flex', gap: 8 }}>
                <input
                  ref={skillFileInputRef}
                  type="file"
                  multiple
                  style={{ display: 'none' }}
                  onChange={handleUploadSkillFile}
                />
                <Button size="small" icon={<UploadOutlined />} onClick={() => skillFileInputRef.current?.click()}>
                  {t('上传文件')}
                </Button>
              </div>
            </div>
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
              {t('技能文件夹内 SKILL.md 以外的文件（脚本、模板、配置等），保存即时生效。二进制文件仅支持上传/删除。')}
            </Typography.Paragraph>
            {skillFiles.length > 0 && (
              <div style={{ border: '1px solid var(--color-border, #E3E6EA)', borderRadius: 8, marginBottom: 8, maxHeight: 200, overflowY: 'auto' }}>
                {skillFiles.map((f) => (
                  <div
                    key={f.filename}
                    style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 12px', borderBottom: '1px solid var(--color-bg-gray, #F5F6F7)' }}
                  >
                    <FileTextOutlined style={{ color: 'var(--color-text-tertiary, #808080)' }} />
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 13 }}>{f.filename}</span>
                    <span style={{ fontSize: 12, color: 'var(--color-text-placeholder, #B3B3B3)' }}>
                      {f.size >= 1024 ? `${(f.size / 1024).toFixed(1)} KB` : `${f.size} B`}
                    </span>
                    {f.is_binary ? (
                      <Tag style={{ margin: 0 }}>{t('二进制')}</Tag>
                    ) : (
                      <Button type="link" size="small" icon={<EditOutlined />} onClick={() => void openSkillFile(f.filename, f.is_binary)}>
                        {t('编辑')}
                      </Button>
                    )}
                    <Popconfirm title={t('确定删除该文件？')} okText={t('删除')} cancelText={t('取消')} okButtonProps={{ danger: true }} onConfirm={() => void handleDeleteSkillFile(f.filename)}>
                      <Button type="link" size="small" danger icon={<DeleteOutlined />} />
                    </Popconfirm>
                  </div>
                ))}
              </div>
            )}
            <div style={{ display: 'flex', gap: 8 }}>
              <Input
                size="small"
                placeholder={t('新建文件名，如 scripts/run.py / config.json')}
                value={newSkillFileName}
                onChange={(e) => setNewSkillFileName(e.target.value)}
                onPressEnter={handleCreateSkillFile}
                style={{ width: 280 }}
              />
              <Button size="small" icon={<PlusOutlined />} onClick={handleCreateSkillFile}>{t('新建文件')}</Button>
            </div>
          </div>
        )}
      </Modal>

      <OntologyBuildValidationModal failure={buildFailure} onClose={() => setBuildFailure(null)} />

      {/* Skill file editor modal —— separate from the edit-skill modal to avoid stretching the card too long */}
      <Modal
        title={editingFileName || ''}
        open={!!editingFileName}
        onCancel={() => { setEditingFileName(null); setEditingFileContent(''); }}
        onOk={() => void handleSaveSkillFile()}
        okText={t('保存文件')}
        okButtonProps={{ icon: <SaveOutlined />, loading: fileSaving }}
        cancelText={t('取消')}
        width="min(760px, calc(100vw - 32px))"
        style={{ top: 40 }}
        destroyOnHidden
      >
        <Input.TextArea
          value={editingFileContent}
          onChange={(e) => setEditingFileContent(e.target.value)}
          rows={20}
          disabled={fileLoading}
          placeholder={fileLoading ? t('加载中…') : undefined}
          style={{ fontFamily: 'monospace', fontSize: 13 }}
        />
      </Modal>

  </>;
}
