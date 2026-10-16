import type { Connection } from './index';

export function updateBadge(connection: Connection, count: number, signal?: AbortSignal) {
  if (!Number.isInteger(count) || count < 0 || count > 9999) return Promise.reject(new Error('invalid_badge'));
  return connection.request<{count: number}>('badge.update', {count}, signal);
}

export function showNotification(connection: Connection, value: {title: string; body: string}, signal?: AbortSignal) {
  return connection.request<{id: string}>('notification.show', value, signal);
}
