import { t } from '../../../i18n';
import { PreviewFileTooLargeError, formatPreviewBytes } from '../../../utils/filePreviewSafety';

export function previewErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof PreviewFileTooLargeError) {
    return t('文件超过 {limit} 的安全预览上限，请下载后在本地打开', {
      limit: formatPreviewBytes(error.limitBytes),
    });
  }
  return error instanceof Error ? error.message : fallback;
}
