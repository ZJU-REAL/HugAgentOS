import { Button, Alert, Space, Typography } from 'antd';
import { clearAssetUpload, retryAssetUpload, useAssetUploadStore } from '../../stores/assetUploadStore';
import { useAuthStore } from '../../stores/authStore';
import { getApiUrl } from '../../api';
import { t } from '../../i18n';

export function AssetUploadStatus() {
  const { task, busy } = useAssetUploadStore();
  const user = useAuthStore(s => s.authUser);
  if (!task || task.owner !== String(user?.user_id ?? '') || task.api !== getApiUrl()) return null;
  const failed = task.items.filter(item => item.status === 'failed');
  const done = task.items.filter(item => item.status === 'done').length;
  const phases = {
    reading: t('读取文件夹'), directories: t('创建目录'), uploading: t('上传中'),
    waiting: t('请求繁忙，等待后自动继续'), finished: t('上传完成'),
    failed: t('部分上传未完成'), reselect: t('请重新选择原文件夹以继续上传'),
  };
  return <Alert className="jx-mySpace-uploadStatus" type={failed.length || task.error ? 'warning' : 'info'} showIcon
    title={phases[task.phase]}
    description={<Space orientation="vertical">
      <Typography.Text>{t('上传进度：{done}/{total}，失败 {failed}', {done, total: task.items.length, failed: failed.length})}</Typography.Text>
      {task.error && <Typography.Text type="danger">{task.error}</Typography.Text>}
      {!task.persisted && <Typography.Text type="warning">{t('无法保存上传记录，请保持页面开启')}</Typography.Text>}
      {failed.length > 0 && <details><summary>{t('查看失败文件')}</summary>
        {failed.map(item => <div key={item.key}>{item.path}: {item.error}</div>)}
      </details>}
      {!busy && <Space>
        {task.phase !== 'finished' && <Button onClick={() => void retryAssetUpload()}>{t('重试未完成项')}</Button>}
        <Button onClick={clearAssetUpload}>{t('清除上传记录')}</Button>
      </Space>}
    </Space>} />;
}
