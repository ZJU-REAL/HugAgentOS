import { installRunSnapshot } from './snapshot';
import type { ChatStreamState } from './state';

import { reduceControls } from './controls';
import { reduceLifecycleAndTools } from './lifecycleAndTools';
import { finalizeRunningTools,processTextChunk } from './messageReducer';
import { appendOrUpdate } from './messageStore';
import { reduceToolResultsAndMeta } from './toolResultsAndMeta';
export function handleSsePayload(s: ChatStreamState, payload: string) {
    const trimmedPayload = payload.trim();
    if (!trimmedPayload)
        return;
    if (trimmedPayload === '[DONE]') {
        if (finalizeRunningTools(s))
            appendOrUpdate(s, true);
        s.streamEnded = true;
        return;
    }
    let textChunk = '';
    let parsed = false;
    try {
        const obj = JSON.parse(trimmedPayload);
        parsed = true;
        if (typeof obj === 'string') {
            // 裸文本帧没有服务端时刻可依，只能记本地钟——它仍要算作一次活动，
            // 否则"静默了多久"会停在上一个结构化事件上。
            s.lastEventTs = Date.now();
            textChunk = obj;
        }
        else if (obj && typeof obj === 'object') {
            const eventObj = obj as Record<string, unknown>;
            const eventType = typeof obj.type === 'string' ? obj.type : '';
            if (typeof eventObj.server_ts === 'number')
                s.lastEventTs = eventObj.server_ts;
            if (typeof eventObj.event_offset === 'number') s.eventOffset = eventObj.event_offset;
            if (eventType === 'run_snapshot') {
                installRunSnapshot(s, eventObj.state);
                return;
            }
            if (eventType === 'run_terminal') { s.streamEnded = true; return; }
            if (eventType === 'run_started') s.runId = String(eventObj.run_id || '');
            // Path-specific events (autonomous loop loop_* etc.) go to the hook first
            if (s.onEvent && s.onEvent(eventObj, s.hookApi))
                return;
            const ontologyRevisionTool = eventObj.scope === 'ontology_revision';
            if (reduceLifecycleAndTools(s,eventObj,obj,eventType,ontologyRevisionTool)) return;
if (reduceToolResultsAndMeta(s,eventObj,obj,eventType)) return;
if (reduceControls(s,eventObj,obj,eventType)) return;
if (eventType === 'content' || eventType === 'ai_message' || eventType === 'text' || eventType === 'delta') {
                textChunk = (obj.delta || obj.content || obj.text || '') as string;
            }
        }
    }
    catch (err) {
        if (parsed)
            throw err;
        s.lastEventTs = Date.now();
        textChunk = trimmedPayload;
    }
    if (textChunk) {
        processTextChunk(s, textChunk);
        appendOrUpdate(s, true);
    }
}