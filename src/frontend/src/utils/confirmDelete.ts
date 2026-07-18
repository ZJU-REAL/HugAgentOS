import { Modal } from 'antd';
import { t, tCtx } from '../i18n';

export function confirmDelete(name: string, onOk: () => void | Promise<void>, kind = ''): void {
  Modal.confirm({
    title: t('确认删除'),
    // kind is translated via the #unit context (English needs singular lowercase, e.g. image); the English translation carries its own trailing space for spacing
    content: t('确定要删除{kind}「{name}」吗？此操作不可撤销。', { kind: kind ? tCtx('unit', kind) : '', name }),
    okText: t('删除'),
    cancelText: t('取消'),
    okButtonProps: { danger: true },
    onOk,
  });
}
