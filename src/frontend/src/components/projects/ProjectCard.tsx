import { motion } from 'motion/react';
import { Tag } from 'antd';
import { StarFilled, StarOutlined, TeamOutlined } from '@ant-design/icons';
import type { ProjectItem } from '../../types';
import { EASE, staggerStyle } from '../../utils/motionTokens';
import { t } from '../../i18n';

interface Props {
  project: ProjectItem;
  onOpen: () => void;
  onToggleFavorite?: () => void;
  /** Entrance stagger index (driven by CSS .jx-anim-stagger; capping handled by staggerStyle) */
  staggerIndex?: number;
}

function relativeTime(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  const diff = Date.now() - d.getTime();
  const min = 60 * 1000;
  const hr = 60 * min;
  const day = 24 * hr;
  const wk = 7 * day;
  if (diff < min) return t('刚刚');
  if (diff < hr) return t('{n} 分钟前', { n: Math.floor(diff / min) });
  if (diff < day) return t('{n} 小时前', { n: Math.floor(diff / hr) });
  if (diff < wk) return t('{n} 天前', { n: Math.floor(diff / day) });
  return d.toLocaleDateString();
}

export default function ProjectCard({ project, onOpen, onToggleFavorite, staggerIndex }: Props) {
  return (
    <div
      className="jx-projectCard"
      onClick={onOpen}
      style={staggerIndex !== undefined ? staggerStyle(staggerIndex) : undefined}
    >
      <div className="jx-projectCard-header">
        <div className="jx-projectCard-title">{project.name}</div>
        <motion.div
          className="jx-projectCard-star"
          onClick={(e) => {
            e.stopPropagation();
            onToggleFavorite?.();
          }}
          whileTap={{ scale: 0.8 }}
          initial={false}
          animate={project.favorite ? { scale: [1, 1.25, 1] } : { scale: 1 }}
          transition={{ duration: 0.35, ease: EASE.brandOut }}
        >
          {project.favorite ? <StarFilled style={{ color: '#F8AB42' }} /> : <StarOutlined />}
        </motion.div>
      </div>
      {project.kind === 'team' && project.team_name && (
        <Tag icon={<TeamOutlined />} color="blue" className="jx-projectCard-teamTag">
          {project.team_name}
        </Tag>
      )}
      {project.description && (
        <div className="jx-projectCard-desc">{project.description}</div>
      )}
      <div className="jx-projectCard-footer">
        Updated {relativeTime(project.last_activity_at || project.updated_at)}
      </div>
    </div>
  );
}
