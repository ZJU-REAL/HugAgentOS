import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from 'antd';
import { ArrowLeftOutlined } from '@ant-design/icons';
import { useLoopStore } from '../../stores/loopStore';
import {
  createLoop,
  startLoop,
  cancelLoop,
  getLoopIterations,
} from '../../api';
import type { LoopIterationItem } from '../../types';
import { parseSSE } from '../../utils/sse';
import './loop.css';

interface LoopPanelProps {
  onBack?: () => void;
}

interface LiveEvent {
  type: string;
  seq?: number;
  verdict?: string;
  score?: number | null;
  reason?: string;
  tokens?: number;
  tool_calls?: number;
  name?: string;
  status?: string;
  final_score?: number | null;
  [k: string]: unknown;
}

export default function LoopPanel({ onBack }: LoopPanelProps) {
  const { loops, fetchLoops, selectedId, setSelectedId, refreshOne } = useLoopStore();
  const [objective, setObjective] = useState('');
  const [criteria, setCriteria] = useState('');
  const [maxIters, setMaxIters] = useState('20');
  const [running, setRunning] = useState(false);
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [iterations, setIterations] = useState<LoopIterationItem[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    fetchLoops();
  }, [fetchLoops]);

  const selected = loops.find((l) => l.loop_id === selectedId) || null;

  const loadIterations = useCallback(async (id: string) => {
    try {
      setIterations(await getLoopIterations(id));
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    if (selectedId) loadIterations(selectedId);
  }, [selectedId, loadIterations]);

  const handleCreate = async () => {
    if (!objective.trim()) return;
    const loop = await createLoop({
      title: objective.slice(0, 40),
      goal_spec: {
        objective,
        acceptance_criteria: criteria.split('\n').map((s) => s.trim()).filter(Boolean),
      },
      budget: { max_iters: Number(maxIters) || 20 },
    });
    await fetchLoops();
    setSelectedId(loop.loop_id);
    setEvents([]);
  };

  const handleStart = async () => {
    if (!selected) return;
    setRunning(true);
    setEvents([]);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const resp = await startLoop(selected.loop_id, { worker_max_iters: 15 }, ctrl.signal);
      for await (const ev of parseSSE<LiveEvent>(resp)) {
        setEvents((prev) => [...prev, ev]);
        if (ev.type === 'loop_completed') {
          await refreshOne(selected.loop_id);
          await loadIterations(selected.loop_id);
        }
      }
    } catch (e) {
      setEvents((prev) => [...prev, { type: 'error', reason: String(e) }]);
    } finally {
      setRunning(false);
    }
  };

  const handleCancel = async () => {
    if (!selected) return;
    await cancelLoop(selected.loop_id);
    abortRef.current?.abort();
    setRunning(false);
  };

  return (
    <div className="loop-root">
      {onBack && (
        <div className="loop-topbar">
          <Button type="text" icon={<ArrowLeftOutlined />} onClick={onBack}>返回</Button>
          <span className="loop-topbar-title">自主循环（Autonomous Loop）</span>
        </div>
      )}
      <div className="loop-panel">
      <div className="loop-sidebar">
        <h3>创建自主循环</h3>
        <label>目标（objective）</label>
        <textarea value={objective} onChange={(e) => setObjective(e.target.value)} rows={3} />
        <label>验收标准（每行一条，可留空由后端从目标抽取）</label>
        <textarea value={criteria} onChange={(e) => setCriteria(e.target.value)} rows={3} />
        <p className="loop-dim" style={{ margin: '4px 0 8px' }}>
          判定由只读评审子智能体打开产出的真实文件逐条核验——无需填写验证命令/评分。
        </p>
        <div className="loop-row">
          <div>
            <label>最大迭代</label>
            <input value={maxIters} onChange={(e) => setMaxIters(e.target.value)} />
          </div>
        </div>
        <button className="loop-btn primary" onClick={handleCreate}>创建</button>

        <h3 style={{ marginTop: 20 }}>循环列表</h3>
        <div className="loop-list">
          {loops.map((l) => (
            <div
              key={l.loop_id}
              className={`loop-list-item ${l.loop_id === selectedId ? 'active' : ''}`}
              onClick={() => setSelectedId(l.loop_id)}
            >
              <div className="loop-title">{l.title || l.goal_spec?.objective?.slice(0, 40) || l.loop_id}</div>
              <div className={`loop-status s-${l.status}`}>{l.status}</div>
              <div className="loop-meta">迭代 {l.iteration_count} · 分 {l.final_score ?? '—'}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="loop-main">
        {!selected ? (
          <div className="loop-empty">从左侧创建或选择一个循环</div>
        ) : (
          <>
            <div className="loop-header">
              <h2>{selected.title || selected.loop_id}</h2>
              <div className="loop-actions">
                {running ? (
                  <button className="loop-btn danger" onClick={handleCancel}>取消</button>
                ) : (
                  <button className="loop-btn primary" onClick={handleStart}>启动 / 续跑</button>
                )}
              </div>
            </div>
            <div className="loop-goal">
              <strong>目标：</strong>{selected.goal_spec?.objective}
              <div className="loop-budget">
                预算：最多 {selected.budget?.max_iters} 轮 · 已用 tokens {selected.tokens_spent} · 状态 {selected.status}
              </div>
            </div>

            <h3>实时事件</h3>
            <div className="loop-stream">
              {events.length === 0 && <div className="loop-dim">（点击「启动」开始）</div>}
              {events.map((ev, i) => (
                <div key={i} className={`loop-ev ev-${ev.type}`}>
                  {ev.type === 'iteration_started' && <span>▶ 第 {ev.seq} 轮开始</span>}
                  {ev.type === 'loop_tool_call' && <span className="loop-dim">  · 工具 {ev.name}</span>}
                  {ev.type === 'iteration_evaluated' && (
                    <span>
                      ◀ 第 {ev.seq} 轮 · <b>{ev.verdict}</b> · 工具 {ev.tool_calls} · 评审员核验
                      {ev.reason ? <div className="loop-reason">↳ {ev.reason}</div> : null}
                    </span>
                  )}
                  {ev.type === 'loop_stagnation' && <span className="loop-warn">⚠ 停滞，触发换策略</span>}
                  {ev.type === 'loop_awaiting_human' && <span className="loop-warn">⏸ 等待人工：{ev.reason}</span>}
                  {ev.type === 'loop_completed' && (
                    <span className="loop-done">■ 结束：{ev.status} · 终分 {ev.final_score ?? '—'} · {ev.reason}</span>
                  )}
                  {ev.type === 'error' && <span className="loop-err">✕ {ev.reason}</span>}
                </div>
              ))}
            </div>

            <h3>迭代轨迹（审计）</h3>
            <table className="loop-table">
              <thead>
                <tr><th>#</th><th>verdict</th><th>工具</th><th>tokens</th><th>判定源</th><th>理由</th></tr>
              </thead>
              <tbody>
                {iterations.map((it) => (
                  <tr key={it.seq}>
                    <td>{it.seq}</td><td>{it.verdict}</td>
                    <td>{it.tool_calls}</td><td>{it.tokens}</td><td>{it.decided_by}</td>
                    <td className="loop-reason-cell">{it.reasoning}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
      </div>
    </div>
  );
}
