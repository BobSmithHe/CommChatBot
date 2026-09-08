"""Small dependency-free API concurrency probe.

Example: python scripts/load_test.py --url http://127.0.0.1:8765 --requests 500 --concurrency 40
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time

import httpx


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--path", default="/health")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--max-failures", type=int, default=0)
    parser.add_argument("--min-rps", type=float, default=0)
    parser.add_argument("--max-p95-ms", type=float, default=0)
    args = parser.parse_args()
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    latencies: list[float] = []
    failures = 0

    async with httpx.AsyncClient(base_url=args.url, timeout=20, trust_env=False) as client:
        async def one() -> None:
            nonlocal failures
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.get(args.path)
                    if response.status_code >= 400:
                        failures += 1
                except Exception:
                    failures += 1
                finally:
                    latencies.append((time.perf_counter() - started) * 1000)

        wall_started = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(max(1, args.requests))))
        wall = time.perf_counter() - wall_started
    ordered = sorted(latencies)
    percentile = lambda value: ordered[min(len(ordered) - 1, int(len(ordered) * value))]
    result = {
        "requests": len(latencies), "failures": failures,
        "rps": round(len(latencies) / wall, 2),
        "mean_ms": round(statistics.mean(latencies), 2),
        "p50_ms": round(percentile(0.50), 2), "p95_ms": round(percentile(0.95), 2),
        "p99_ms": round(percentile(0.99), 2),
    }
    print(result)
    failed_gate = (
        failures > max(0, args.max_failures)
        or (args.min_rps > 0 and result["rps"] < args.min_rps)
        or (args.max_p95_ms > 0 and result["p95_ms"] > args.max_p95_ms)
    )
    if failed_gate:
        print("Performance gate failed", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
