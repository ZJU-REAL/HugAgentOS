import { Modal, Typography, Form, Input, Select } from 'antd';
import { t } from '../../i18n';
import { MARKETPLACE_CATEGORIES } from '../../utils/constants';
import { SkillMarketplaceModal } from './SkillMarketplaceModal';
import type { useSkillMarketplace } from './useSkillMarketplace';
export function SkillMarketplaceDialogs({ marketplace, fetchCatalog }: {
  marketplace: ReturnType<typeof useSkillMarketplace>; fetchCatalog: () => Promise<unknown>;
}) {
  const { marketplaceOpen, setMarketplaceOpen, marketplaceFetchers, applySkillId, setApplySkillId, applying, applyForm, subBySkill, handleApply } = marketplace;
  return <>
      {/* Skill marketplace modal —— install as a private skill */}
      <SkillMarketplaceModal
        open={marketplaceOpen}
        onClose={() => setMarketplaceOpen(false)}
        fetchers={marketplaceFetchers}
        scopeLabel={t('仅自己可见可用')}
        onInstalled={() => { void fetchCatalog(); }}
      />

      {/* Apply-to-list-on-marketplace modal */}
      <Modal
        title={t('申请上架技能市场')}
        open={!!applySkillId}
        onCancel={() => setApplySkillId(null)}
        onOk={() => void handleApply()}
        okText={t('提交申请')}
        cancelText={t('取消')}
        confirmLoading={applying}
        width={520}
        destroyOnHidden
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          {t('提交后由管理员审核，通过后该技能将出现在技能市场，所有用户都可以安装使用。审核基于当前内容的快照，之后你对原技能的修改不会影响已上架版本。')}
        </Typography.Paragraph>
        {applySkillId && subBySkill.get(applySkillId)?.status === 'rejected' && (
          <Typography.Paragraph type="danger" style={{ fontSize: 12 }}>
            上次申请被驳回{subBySkill.get(applySkillId)?.review_note ? `：${subBySkill.get(applySkillId)?.review_note}` : ''}，建议调整后重新提交。
          </Typography.Paragraph>
        )}
        <Form form={applyForm} layout="vertical">
          <Form.Item name="summary" label={t('市场展示摘要（可选）')} tooltip={t('留空则使用技能的一句话描述')}>
            <Input.TextArea rows={2} maxLength={2000} placeholder={t('向其他用户介绍这个技能能做什么')} />
          </Form.Item>
          <Form.Item name="category" label={t('上架分类')} rules={[{ required: true, message: t('请选择上架分类') }]}>
            <Select
              placeholder={t('选择该技能在市场中的分类')}
              options={MARKETPLACE_CATEGORIES.map((c) => ({ value: c, label: c }))}
            />
          </Form.Item>
          <Form.Item name="note" label={t('给管理员的备注（可选）')}>
            <Input.TextArea rows={3} maxLength={2000} placeholder={t('补充说明使用场景、测试情况等，便于审核')} />
          </Form.Item>
        </Form>
      </Modal>
  </>;
}
