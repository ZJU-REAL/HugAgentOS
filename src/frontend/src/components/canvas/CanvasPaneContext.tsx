import { createContext } from 'react';
import type { CanvasPaneId } from '../../stores/canvasStore';

export const CanvasPaneContext = createContext<CanvasPaneId>('primary');
