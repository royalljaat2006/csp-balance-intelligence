"""
Reproducible local/staging load-test harness (scalability foundation,
2026-09-21).

Deliberately dependency-free beyond `requests` (already a pinned project
dependency) — no locust/k6 install required to reproduce these numbers.
Drives one target URL with a fixed pool of worker threads for a fixed
duration, at a target concurrency level, and reports real p50/p95/p99/
error-rate/measured-RPS from the actual responses received. This is a
correctness-over-cleverness harness: it proves what this stack does under
concurrent load *today*, on *this* machine — see the scalability report
for why "highest successful step here" is not the same claim as "capacity
at scale," and for the recommended follow-up with a distributed tool
(k6/Locust/Gatling) against real staging infrastructure.

Usage:
    python scripts/load_test/run_load_test.py --url http://127.0.0.1:8210/health \
        --concurrency 100 --duration 15

    python scripts/load_test/run_load_test.py --url http://127.0.0.1:8210/api/v1/csps \
        --header "X-API-Key: <key>" --concurrency 100 --duration 15
"""

from __future__ import annotations

import argparse
import statistics
import threading
import time

import requests


def _worker(
    url: str,
    headers: dict[str, str],
    stop_at: float,
    latencies: list[float],
    errors: list[str],
    lock: threading.Lock,
    session: requests.Session,
) -> None:
    while time.monotonic() < stop_at:
        started = time.monotonic()
        try:
            response = session.get(url, headers=headers, timeout=10)
            elapsed = time.monotonic() - started
            with lock:
                latencies.append(elapsed)
                if response.status_code >= 400:
                    errors.append(str(response.status_code))
        except requests.RequestException as exc:
            elapsed = time.monotonic() - started
            with lock:
                latencies.append(elapsed)
                errors.append(type(exc).__name__)


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return float("nan")
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * pct))
    return sorted_values[idx]


def run_step(url: str, headers: dict[str, str], concurrency: int, duration: float) -> dict:
    latencies: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    stop_at = time.monotonic() + duration

    sessions = [requests.Session() for _ in range(concurrency)]
    threads = [
        threading.Thread(
            target=_worker, args=(url, headers, stop_at, latencies, errors, lock, sessions[i])
        )
        for i in range(concurrency)
    ]
    started_at = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_time = time.monotonic() - started_at

    sorted_latencies = sorted(latencies)
    total = len(sorted_latencies)
    return {
        "concurrency": concurrency,
        "duration_s": round(wall_time, 2),
        "requests": total,
        "measured_rps": round(total / wall_time, 1) if wall_time else 0.0,
        "errors": len(errors),
        "error_rate_pct": round(100 * len(errors) / total, 2) if total else 0.0,
        "p50_ms": round(_percentile(sorted_latencies, 0.50) * 1000, 1),
        "p95_ms": round(_percentile(sorted_latencies, 0.95) * 1000, 1),
        "p99_ms": round(_percentile(sorted_latencies, 0.99) * 1000, 1),
        "max_ms": round((sorted_latencies[-1] if sorted_latencies else 0) * 1000, 1),
        "mean_ms": round(statistics.mean(sorted_latencies) * 1000, 1) if sorted_latencies else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--header", action="append", default=[], help='e.g. "X-API-Key: abc123"')
    parser.add_argument(
        "--concurrency-steps",
        default="5,10,25,50,100,200",
        help="Comma-separated concurrent-worker counts to step through.",
    )
    parser.add_argument("--duration", type=float, default=10.0, help="Seconds per step.")
    parser.add_argument(
        "--stop-on-error-rate",
        type=float,
        default=5.0,
        help="Stop escalating once a step's error rate exceeds this percent.",
    )
    args = parser.parse_args()

    headers = {}
    for h in args.header:
        name, _, value = h.partition(":")
        headers[name.strip()] = value.strip()

    print(f"Target: {args.url}")
    print(f"{'concurrency':>11} {'req/s':>8} {'p50ms':>7} {'p95ms':>7} {'p99ms':>7} {'maxms':>7} {'errs':>6} {'err%':>6}")
    for step in [int(s) for s in args.concurrency_steps.split(",")]:
        result = run_step(args.url, headers, step, args.duration)
        print(
            f"{result['concurrency']:>11} {result['measured_rps']:>8} {result['p50_ms']:>7} "
            f"{result['p95_ms']:>7} {result['p99_ms']:>7} {result['max_ms']:>7} "
            f"{result['errors']:>6} {result['error_rate_pct']:>6}"
        )
        if result["error_rate_pct"] > args.stop_on_error_rate:
            print(
                f"Stopping: error rate {result['error_rate_pct']}% exceeded "
                f"--stop-on-error-rate {args.stop_on_error_rate}% at concurrency={step}."
            )
            break


if __name__ == "__main__":
    main()
