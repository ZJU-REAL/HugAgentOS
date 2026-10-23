/**
 * Component template.
 *
 * Replace ${ComponentName}, ${componentName}, ${group} with actual names.
 * Create as components/${group}/${ComponentName}.tsx and export it from components/${group}/index.ts.
 *
 * Rules (see SKILL.md + references/ui-design-spec.md):
 *   - Every user-visible string goes through t() (npm run build fails otherwise)
 *   - Copy: buttons say the action, errors say cause + fix, success says where the result is
 *   - One primary button per view; secondary actions use the quiet fill, not outlined buttons
 *   - No colors in inline style — put them in the CSS file with tokens
 */

import { useCallback, useMemo, useState } from 'react';
import { Button, message } from 'antd';
import { t } from '../../i18n';
// import { useSomeStore } from '../../stores';
// import type { SomeItem } from '../../types';

interface ${ComponentName}Item {
  id: string;
  name: string;
  active: boolean;
}

interface ${ComponentName}Props {
  /** 所属对象 id */
  ownerId: string;
  items?: ${ComponentName}Item[];
  onSaved?: () => void;
}

export function ${ComponentName}({ ownerId, items = [], onSaved }: ${ComponentName}Props) {
  const [saving, setSaving] = useState(false);

  const activeItems = useMemo(() => items.filter((item) => item.active), [items]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      // await saveSomething(ownerId);
      message.success(t('已保存到我的空间'));
      onSaved?.();
    } catch (e) {
      // Error text: cause + what to do. Prefer the backend message when it is user-facing.
      message.error(t('保存失败：{reason}', { reason: (e as Error).message }));
    } finally {
      setSaving(false);
    }
  }, [ownerId, onSaved]);

  if (!activeItems.length) {
    return <div className="jx-${componentName}-empty">{t('还没有内容')}</div>;
  }

  return (
    <section className="jx-${componentName}">
      <header className="jx-${componentName}-header">
        <h3 className="jx-${componentName}-title">{t('{n} 项', { n: activeItems.length })}</h3>
        <Button type="primary" onClick={handleSave} loading={saving}>
          {saving ? t('保存中') : t('保存')}
        </Button>
      </header>

      <ul className="jx-${componentName}-list">
        {activeItems.map((item) => (
          <li key={item.id} className="jx-${componentName}-item">
            {item.name}
          </li>
        ))}
      </ul>
    </section>
  );
}
