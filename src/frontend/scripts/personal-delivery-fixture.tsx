import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ConfigProvider } from 'antd';
import { PersonalMessageRow } from '../src/components/personal-agent/conversation/PersonalMessageRow';
import type { PersonalMessage } from '../src/components/personal-agent/api';
import { ImagePreview } from '../src/components/common';
import { FilePreviewPane } from '../src/components/file/FilePreviewPane';
import { useCanvasStore } from '../src/stores/canvasStore';
import '../src/styles/index';
import '../src/styles/variables.css';
import '../src/styles/common.css';
import '../src/styles/tool.css';
import '../src/styles/canvas.css';
import '../src/components/personal-agent/personal-agent.css';

export function Fixture() {
  const [rows, setRows] = useState<PersonalMessage[]>([]);
  const artifact = useCanvasStore(state => state.artifact);
  useEffect(() => { void fetch('/fixture-messages').then(r => r.json()).then(setRows); }, []);
  return <ConfigProvider theme={{ token: { motion: false } }}>
    <div className="pa-panel"><div className="pa-conversation" style={{ maxWidth: 760, margin: '20px auto', padding: 20 }} role="log">
      <p role="status">任务仍在执行</p>
      {rows.filter(row => row.role === 'assistant').map(row =>
        <PersonalMessageRow key={row.id} row={row} author="Personal Agent" />)}
    </div></div>
    <ImagePreview />
    {artifact && <aside aria-label="文件预览" style={{ margin: 20 }}>
      <FilePreviewPane item={{ ...artifact, id: artifact.file_id || '', type: 'document', name: artifact.name, created_at: '' }} />
    </aside>}
  </ConfigProvider>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);
