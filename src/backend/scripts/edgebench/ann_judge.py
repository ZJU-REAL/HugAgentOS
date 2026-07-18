"""Local deterministic judge for EdgeBench `ann_vector_search_qps` (a minimal faithful reproduction of SForge).

Original task: replace the **brute-force nearest-neighbor baseline** with a high-performance
nearest neighbor and compare QPS under a recall constraint.
This judge runs inside the sandbox and is the loop's verify_cmd = ground truth source:

  - Deterministically generate base/query vectors (fixed seed), and compute the true top-k with a
    NumPy brute force (evaluation ground truth, not timed).
  - Time a **naive pure-Python brute-force baseline** to obtain the baseline QPS (this is the object
    to be replaced/surpassed).
  - import the contestant's /workspace/solution.py (must expose build(base)->index and
    search(index, queries, k)->ids), and measure its recall@k and QPS.
  - Scoring: recall >= RECALL_MIN counts as a valid solution (exit 0); SCORE = contestant QPS / baseline QPS (speedup).

Stable timing: call search repeatedly until the cumulative time >= MIN_TIME seconds, then compute QPS
as total queries processed / total elapsed to eliminate small-workload noise.

Output (for the loop's score_regex / evaluator to parse):
  RECALL=<r> QPS=<agent_qps> BASELINE_QPS=<b> SCORE=<speedup>
exit 0 if and only if recall meets the threshold. Whether speed meets the bar is decided by the loop's target_score.
"""
import sys
import time
import traceback

RECALL_MIN = 0.90
N_BASE = 2000
DIM = 32
N_QUERY = 200
K = 10
SEED = 1234
MIN_TIME = 0.30  # stable timing window


def _fail(msg: str) -> None:
    print("RECALL=0.0 QPS=0.0 BASELINE_QPS=0.0 SCORE=0.0")
    sys.stderr.write(f"JUDGE_ERROR: {msg}\n")
    sys.exit(1)


def _timed_qps(fn, n_items: int) -> float:
    """Run fn() repeatedly until cumulative time >= MIN_TIME, returning items/sec (stable, noise-resistant)."""
    reps = 0
    t0 = time.perf_counter()
    while True:
        fn()
        reps += 1
        elapsed = time.perf_counter() - t0
        if elapsed >= MIN_TIME or reps >= 10000:
            break
    return (reps * n_items) / max(elapsed, 1e-9)


def main() -> None:
    try:
        import numpy as np
    except Exception as e:  # noqa: BLE001
        _fail(f"numpy 不可用: {e}")

    rng = np.random.RandomState(SEED)
    base = rng.rand(N_BASE, DIM).astype("float32")
    queries = rng.rand(N_QUERY, DIM).astype("float32")

    # Ground truth: vectorized brute-force top-k (for evaluation, not timed)
    b2 = (base ** 2).sum(1)
    truth = np.empty((N_QUERY, K), dtype=np.int64)
    for i in range(N_QUERY):
        d = b2 - 2.0 * base.dot(queries[i])
        truth[i] = np.argpartition(d, K)[:K]

    # Baseline: naive pure-Python brute force (the object to surpass). Timed on a query subset, then normalized.
    base_list = base.tolist()
    qsub = queries[:20].tolist()

    def naive_pass():
        for q in qsub:
            best = []
            for j, b in enumerate(base_list):
                s = 0.0
                for a, c in zip(q, b):
                    diff = a - c
                    s += diff * diff
                best.append((s, j))
            best.sort()
            _ = [j for _, j in best[:K]]

    baseline_qps = _timed_qps(naive_pass, len(qsub))

    # Load the contestant's solution (fresh process → import bypasses the Jupyter cache)
    sys.path.insert(0, "/workspace")
    try:
        import solution  # type: ignore
    except Exception:  # noqa: BLE001
        _fail("无法 import /workspace/solution.py（文件缺失或有语法错误）:\n" + traceback.format_exc())

    if not hasattr(solution, "build") or not hasattr(solution, "search"):
        _fail("solution.py 必须提供 build(base) 和 search(index, queries, k) 两个函数")

    try:
        index = solution.build(base)
    except Exception:  # noqa: BLE001
        _fail("solution.build(base) 抛异常:\n" + traceback.format_exc())

    # Run once first to verify correctness (and serve as warmup)
    try:
        pred = solution.search(index, queries, K)
    except Exception:  # noqa: BLE001
        _fail("solution.search(...) 抛异常:\n" + traceback.format_exc())
    try:
        pred_arr = [list(row) for row in pred]
        assert len(pred_arr) == N_QUERY
    except Exception:  # noqa: BLE001
        _fail(f"search 返回值形状错误：应为 {N_QUERY} 行、每行至少 {K} 个 id 的列表")

    hit = 0
    for i in range(N_QUERY):
        hit += len(set(int(x) for x in pred_arr[i][:K]) & set(int(x) for x in truth[i]))
    recall = hit / (N_QUERY * K)

    agent_qps = _timed_qps(lambda: solution.search(index, queries, K), N_QUERY)
    speedup = agent_qps / max(baseline_qps, 1e-9)

    print(
        f"RECALL={recall:.4f} QPS={agent_qps:.1f} "
        f"BASELINE_QPS={baseline_qps:.1f} SCORE={speedup:.4f}"
    )
    if recall < RECALL_MIN:
        sys.stderr.write(
            f"JUDGE_ERROR: recall {recall:.3f} < {RECALL_MIN}（近似太糙，召回不足）\n"
        )
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
