import { useState } from 'react';
import { Spin } from 'antd';
import { CustomerServiceOutlined, ExclamationCircleOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { usePublicMediaUrl } from '../../hooks/usePublicMediaUrl';
import './MediaPlayer.css';

interface MediaPlayerProps {
  kind: 'video' | 'audio';
  src: string;
  name: string;
}

/**
 * Native audio/video player shared by the canvas sidebar and the file preview
 * pane. Authenticated surfaces stream through HTTP Range requests; public chat
 * buffers the file into a blob first (see usePublicMediaUrl).
 */
export function MediaPlayer({ kind, src, name }: MediaPlayerProps) {
  const media = usePublicMediaUrl(src);
  const [failedUrl, setFailedUrl] = useState('');

  if (media.error) {
    return (
      <div className="jx-mediaPlayer jx-mediaPlayer--status" role="alert">
        <ExclamationCircleOutlined className="jx-mediaPlayer-statusIcon" />
        <p className="jx-mediaPlayer-statusText">{media.error}</p>
      </div>
    );
  }
  if (media.url && failedUrl === media.url) {
    return (
      <div className="jx-mediaPlayer jx-mediaPlayer--status" role="alert">
        <ExclamationCircleOutlined className="jx-mediaPlayer-statusIcon" />
        <p className="jx-mediaPlayer-statusText">{t('浏览器无法播放此文件')}</p>
        <p className="jx-mediaPlayer-statusHint">{t('文件可能已损坏，或编码格式不受浏览器支持。请下载后用本机播放器打开')}</p>
      </div>
    );
  }
  if (!media.url) {
    return (
      <div className="jx-mediaPlayer jx-mediaPlayer--status">
        <Spin />
        <p className="jx-mediaPlayer-statusText">{t('正在加载预览…')}</p>
      </div>
    );
  }

  const onError = () => setFailedUrl(media.url);
  if (kind === 'video') {
    return (
      <div className="jx-mediaPlayer jx-mediaPlayer--video">
        <video className="jx-mediaPlayer-video" src={media.url} aria-label={name}
          controls playsInline preload="metadata" onError={onError} />
      </div>
    );
  }
  return (
    <div className="jx-mediaPlayer jx-mediaPlayer--audio">
      <div className="jx-mediaPlayer-cover" aria-hidden="true"><CustomerServiceOutlined /></div>
      <p className="jx-mediaPlayer-name" title={name}>{name}</p>
      <audio className="jx-mediaPlayer-audio" src={media.url} aria-label={name}
        controls preload="metadata" onError={onError} />
    </div>
  );
}
