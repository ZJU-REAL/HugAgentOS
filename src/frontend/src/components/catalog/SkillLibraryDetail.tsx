import { motion } from 'motion/react';
import { Tag, Switch, Typography } from 'antd';
import { LeftOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { mdToHtml, stripMarkdown } from '../../utils/markdown';
import { DRILL_IN_DETAIL } from '../../utils/motionVariants';
import { SkillAvatar } from './skillIcons';
import type { SkillItem } from '../../types';
export function SkillLibraryDetail({ selectedItem, selectedKind, navDir, closeDetail, toggleEnabled }: {
  selectedItem: SkillItem; selectedKind: 'skills' | 'agents'; navDir: 'detail' | 'list' | null;
  closeDetail: () => void; toggleEnabled: (kind: 'skills' | 'agents', id: string, enabled: boolean) => void;
}) {
  // ``detail`` is now the user-facing user_intro markdown (managed via
    // admin DB + configs/user_intros.py defaults). It is NOT the raw
    // SKILL.md body anymore — no frontmatter to parse.
    const markdownBody = selectedItem.detail || '';
    const version = selectedItem.version || '';
    const tags = selectedItem.tags || [];

    return (
      <motion.div
        key="detail"
        className="jx-sk-detailPage"
        {...(navDir === 'detail' ? DRILL_IN_DETAIL : { initial: false })}
      >
        {/* Sticky header: identity remains compact while the enable switch stays on the first row */}
        <div className="jx-sk-stickyHeader">
          <button className="jx-sk-backBtn jx-sk-backBtn--inline" onClick={closeDetail}>
            <LeftOutlined style={{ fontSize: 14 }} />
          </button>
          <div className="jx-sk-stickyHeaderIdentity">
            <SkillAvatar icon={selectedItem.icon} name={selectedItem.name} seed={selectedItem.id} size={28} round />
            <div className="jx-sk-detailHeading">
              <div className="jx-sk-detailHeadingMain">
                <span className="jx-sk-detailName">{selectedItem.name}</span>
                <Tag className="jx-sk-tag" color={selectedItem.enabled ? 'blue' : 'default'}>
                  {selectedItem.enabled ? t('已启用') : t('未启用')}
                </Tag>
              </div>
              {version && <span className="jx-sk-version">v{version}</span>}
            </div>
          </div>
          <div className="jx-sk-detailHeaderRight">
            <span className="jx-sk-enableLabel">{t('启用')}</span>
            <Switch
              checked={!!selectedItem.enabled}
              onChange={(v) => toggleEnabled(selectedKind, selectedItem.id, v)}
            />
          </div>
        </div>

        {/* Scrollable body */}
        <div className="jx-sk-stickyBody">

          {/* Metadata card */}
          <div className="jx-sk-metaCard">
            <h4 className="jx-sk-metaName">{selectedItem.name}</h4>
            <p className="jx-sk-metaDesc">{stripMarkdown(selectedItem.desc)}</p>
            {tags.length > 0 && (
              <div className="jx-sk-metaTags">
                {tags.map((tag: string, i: number) => (
                  <Tag key={i} className="jx-sk-metaTag">{tag}</Tag>
                ))}
              </div>
            )}
          </div>

          {/* Body: markdown content */}
          <div className="jx-sk-detailBody">
            {markdownBody ? (
              <div className="jx-md jx-sk-detailMarkdown" dangerouslySetInnerHTML={{ __html: mdToHtml(markdownBody) }} />
            ) : (
              <Typography.Text type="secondary">{t('暂无详情')}</Typography.Text>
            )}
          </div>
        </div>
      </motion.div>
    );
}
