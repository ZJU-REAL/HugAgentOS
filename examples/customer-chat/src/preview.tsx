import { createRoot } from 'react-dom/client';
import { PreviewHost } from '@hugagent/chat-sdk/preview';
import customization from './customization.js';
import './developer.css';
createRoot(document.getElementById('root')!).render(<PreviewHost customization={customization} />);
