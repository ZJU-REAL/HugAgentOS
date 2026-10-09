import { handleSsePayload } from './payload';
import type { ChatStreamState } from './state';
export function processSseBlock(s: ChatStreamState, block: string) {
    if (!block.trim())
        return;
    const lines = block.split(/\r?\n/);
    const dataLines: string[] = [];
    for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed.startsWith('data:'))
            dataLines.push(trimmed.slice(5).trim());
    }
    if (dataLines.length === 0)
        return;
    handleSsePayload(s, dataLines.join('\n'));
}