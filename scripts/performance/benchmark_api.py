"""Measure real local HTTP routes against an identical synthetic PostgreSQL dataset."""

import asyncio
import json
import statistics
import time
from pathlib import Path
import httpx


def summary(samples):
    ordered = sorted(samples)
    return {
        "p50_ms": round(statistics.median(samples), 2),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
        "samples": len(samples),
    }


async def measure(port, improved):
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=60) as client:
        await client.get("/__perf/health")
        report = {}
        for name, url in [
            ("search", "/v1/chats/search?q=needle&scope=all&page_size=20"),
            (
                "history",
                "/v1/chats/perf-history/messages?page_size=30&order=desc"
                + ("&cursor=true" if improved else ""),
            ),
        ]:
            for _ in range(2):
                response = await client.get(url)
                response.raise_for_status()
            await client.get("/__perf/sql")
            timings = []
            for _ in range(20):
                start = time.perf_counter()
                response = await client.get(url)
                response.raise_for_status()
                timings.append((time.perf_counter() - start) * 1000)
            statements = (await client.get("/__perf/sql")).json()
            report[name] = {
                **summary(timings),
                "sql_per_request": len(statements) / 20,
                "response_bytes": len(response.content),
                "selects_replay_payload": any("model_steps" in s for s in statements),
            }
            if name == "search":
                assert response.json()["data"]["total"] == 200
                assert len(response.json()["data"]["items"]) == 20
            else:
                data = response.json()["data"]
                assert len(data["items"]) == 30
                assert data["items"][0]["chat_seq"] == 371
                if improved:
                    assert not report[name]["selects_replay_payload"]
                    second = await client.get(url + "&before_seq=" + str(data["next_before_seq"]))
                    assert second.json()["data"]["items"][-1]["chat_seq"] == 370
                    denied = await client.get("/v1/chats/foreign-chat/messages?cursor=true")
                    assert denied.status_code in (403, 404)
        timings = []
        for _ in range(10):
            slow = asyncio.create_task(client.get("/v1/chats/slow-perf/pending-user-questions"))
            await asyncio.sleep(0.03)
            start = time.perf_counter()
            response = await client.get("/__perf/health")
            response.raise_for_status()
            timings.append((time.perf_counter() - start) * 1000)
            assert (await slow).status_code == 200
        report["health_during_200ms_database_query"] = summary(timings)
        return report


async def main():
    result = {"before": await measure(38171, False), "after": await measure(38172, True)}
    output = Path(".git/performance-172/api-results.json")
    output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
