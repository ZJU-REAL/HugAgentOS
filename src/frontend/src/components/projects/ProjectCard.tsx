import { StarFilled, StarOutlined } from '@ant-design/icons';
import { motion } from 'motion/react';

import { t } from '../../i18n';
import type { ProjectItem } from '../../types';
import { EASE, staggerStyle } from '../../utils/motionTokens';

interface Props {
  project: ProjectItem;
  onOpen: () => void;
  onToggleFavorite?: () => void;
  staggerIndex?: number;
}

function relativeTime(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  const diff = Date.now() - date.getTime();
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;
  const week = 7 * day;
  if (diff < minute) return t('刚刚');
  if (diff < hour) return t('{n} 分钟前', { n: Math.floor(diff / minute) });
  if (diff < day) return t('{n} 小时前', { n: Math.floor(diff / hour) });
  if (diff < week) return t('{n} 天前', { n: Math.floor(diff / day) });
  return date.toLocaleDateString();
}

export default function ProjectCard({ project, onOpen, onToggleFavorite, staggerIndex }: Props) {
  return (
    <div
      className="jx-projectCard"
      onClick={onOpen}
      style={staggerIndex === undefined ? undefined : staggerStyle(staggerIndex)}
    >
      <div className="jx-projectCard-header">
        <div className="jx-projectCard-title">{project.name}</div>
        <motion.div
          className="jx-projectCard-star"
          onClick={(event) => { event.stopPropagation(); onToggleFavorite?.(); }}
          whileTap={{ scale: 0.8 }}
          initial={false}
          animate={project.favorite ? { scale: [1, 1.25, 1] } : { scale: 1 }}
          transition={{ duration: 0.35, ease: EASE.brandOut }}
        >
          {project.favorite ? <StarFilled style={{ color: '#F8AB42' }} /> : <StarOutlined />}
        </motion.div>
      </div>
      {project.description && <div className="jx-projectCard-desc">{project.description}</div>}
      <div className="jx-projectCard-footer">
        Updated {relativeTime(project.last_activity_at || project.updated_at)}
      </div>
    </div>
  );
}
