export interface QueuedRunHandoff {
  runId: string;
  messageId: string;
  userMessageId: string;
  message: string;
  queueId: string;
  steerId: string;
  deliveryMode: 'follow_up' | 'next_run';
}

/** Validate and normalize the durable handoff frame before the UI follows it. */
export function parseQueuedRunHandoff(
  event: Record<string, unknown>,
): QueuedRunHandoff | undefined {
  const runId = typeof event.run_id === 'string' ? event.run_id : '';
  const messageId = typeof event.message_id === 'string' ? event.message_id : '';
  const userMessageId = typeof event.user_message_id === 'string' ? event.user_message_id : '';
  const message = typeof event.message === 'string' ? event.message : '';
  const queueId = typeof event.queue_id === 'string' ? event.queue_id : '';
  const steerId = typeof event.steer_id === 'string' ? event.steer_id : '';
  const deliveryMode = event.delivery_mode;
  if (
    !runId || !messageId || !userMessageId || !message || !queueId || !steerId
    || (deliveryMode !== 'follow_up' && deliveryMode !== 'next_run')
  ) {
    return undefined;
  }
  return {
    runId,
    messageId,
    userMessageId,
    message,
    queueId,
    steerId,
    deliveryMode,
  };
}

/** Rebuild the same descriptor from the durable queue when the SSE projection was lost. */
export function parseAppliedQueueHandoff(
  item: Record<string, unknown>,
): QueuedRunHandoff | undefined {
  return parseQueuedRunHandoff({
    run_id: item.applied_run_id,
    message_id: item.applied_run_message_id,
    user_message_id: item.applied_user_message_id,
    message: item.message,
    queue_id: item.queue_id,
    steer_id: item.steer_id,
    delivery_mode: item.delivery_mode,
  });
}
