import { useCallback, useEffect, useMemo, useState } from 'react';
import { Form, message } from 'antd';
import { t } from '../../i18n';
import type { MarketplaceFetchers, MarketplaceSubmission } from '../../types';
import { getMarketplaceSkills, getMarketplaceSkillDetail, installMarketplaceSkill, submitSkillToMarketplace, getMySkillSubmissions, withdrawSkillSubmission } from '../../api';
export function useSkillMarketplace(canAddSkill: boolean) {
  const [marketplaceOpen, setMarketplaceOpen] = useState(false);
  const marketplaceFetchers = useMemo<MarketplaceFetchers>(() => ({
    loadList: () => getMarketplaceSkills(),
    loadDetail: (slug) => getMarketplaceSkillDetail(slug),
    install: (slug, secrets) => installMarketplaceSkill(slug, secrets),
  }), []);

  // ── Apply to list a skill on the marketplace (only your own private skills) ──────────────────────────────
  const [mySubs, setMySubs] = useState<MarketplaceSubmission[]>([]);
  const [applySkillId, setApplySkillId] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const [applyForm] = Form.useForm();

  const reloadSubmissions = useCallback(async () => {
    try {
      setMySubs(await getMySkillSubmissions());
    } catch {
      // List load failure does not bother the user, only affects the status marker
    }
  }, []);

  useEffect(() => {
    if (canAddSkill) void reloadSubmissions();
  }, [canAddSkill, reloadSubmissions]);

  // Take the most recent application per skill (the API returns newest→oldest)
  const subBySkill = useMemo(() => {
    const map = new Map<string, MarketplaceSubmission>();
    for (const s of mySubs) {
      if (!map.has(s.skill_id)) map.set(s.skill_id, s);
    }
    return map;
  }, [mySubs]);

  const openApply = useCallback((skillId: string) => {
    applyForm.resetFields();
    setApplySkillId(skillId);
  }, [applyForm]);

  const handleApply = useCallback(async () => {
    if (!applySkillId) return;
    const v = await applyForm.validateFields();
    setApplying(true);
    try {
      await submitSkillToMarketplace({
        skill_id: applySkillId,
        note: (v.note || '').trim(),
        category: (v.category || '').trim(),
        summary: (v.summary || '').trim(),
      });
      message.success(t('上架申请已提交，等待管理员审核'));
      setApplySkillId(null);
      await reloadSubmissions();
    } catch (e) {
      message.error((e as Error).message || t('提交失败'));
    } finally {
      setApplying(false);
    }
  }, [applySkillId, applyForm, reloadSubmissions]);

  const handleWithdraw = useCallback(async (submissionId: string) => {
    try {
      await withdrawSkillSubmission(submissionId);
      message.success(t('申请已撤回'));
      await reloadSubmissions();
    } catch (e) {
      message.error((e as Error).message || t('撤回失败'));
    }
  }, [reloadSubmissions]);

  return { marketplaceOpen, setMarketplaceOpen, marketplaceFetchers, applySkillId, setApplySkillId, applying, applyForm, subBySkill, openApply, handleApply, handleWithdraw };
}
