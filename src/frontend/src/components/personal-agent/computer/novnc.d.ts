// The parts of noVNC that the Personal Agent live screen uses. noVNC ships no types.
declare module '@novnc/novnc' {
  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, url: string, options?: { shared?: boolean; wsProtocols?: string[] });
    viewOnly: boolean;
    scaleViewport: boolean;
    resizeSession: boolean;
    clipViewport: boolean;
    focusOnClick: boolean;
    showDotCursor: boolean;
    qualityLevel: number;
    compressionLevel: number;
    background: string;
    disconnect(): void;
    sendKey(keysym: number, code: string | null, down?: boolean): void;
  }
}
