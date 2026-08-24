const INTERVAL = 25_000;

export function startNotificationPoll({ Notification, http, proxyOrigin, state, brand = "HugAgentOS" }) {
  const startedAt = Date.now();
  const seen = new Set();
  const timer = setInterval(async () => {
    if (!state.token) return;
    try {
      const response = await http.fetch(`${proxyOrigin}/api/v1/automations/notifications/list`, { timeout: 10_000 });
      if (!response.ok) return;
      const body = await response.json();
      for (const item of body.data || []) {
        if (!item.id || seen.has(item.id)) continue;
        seen.add(item.id);
        if (Number(item.timestamp || 0) <= startedAt) continue;
        const title = `${brand} · ${item.status === "failed" ? "任务失败" : "任务完成"}`;
        const text = item.summary ? `${item.task_name || "任务"}：${item.summary}` : item.task_name || "任务";
        if (Notification.isSupported()) new Notification({ title, body: text }).show();
      }
      if (seen.size > 400) seen.clear();
    } catch {}
  }, INTERVAL);
  timer.unref();
  return () => clearInterval(timer);
}
