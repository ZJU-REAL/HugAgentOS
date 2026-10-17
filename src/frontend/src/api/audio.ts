import { apiRequest } from '../api';

export async function getAudioStatus(signal?: AbortSignal): Promise<{ allowed: boolean; configured: boolean }> {
  const response = await apiRequest<{ data: { allowed: boolean; configured: boolean } }>('/v1/audio/status', { signal });
  return response.data;
}

export async function transcribeAudio(audio: Blob, signal: AbortSignal): Promise<string> {
  const response = await apiRequest<{ data: { text: string } }>('/v1/audio/transcriptions', { method: 'POST', body: audio, headers: { 'Content-Type': 'audio/wav' }, signal });
  return response.data.text;
}
