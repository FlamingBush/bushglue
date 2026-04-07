#!/usr/bin/env python3
"""
Latency benchmark for the text-to-verse HTTP server.

Sends 1000 queries (cycling through a pool of diverse questions) and
reports detailed latency statistics: min, max, mean, median, stddev,
and percentiles (p50, p90, p95, p99).

By default the script ensures that both ChromaDB and the text-to-verse
server are running before the benchmark starts, launching them as child
processes if necessary.  Pass --no-chroma / --no-server to skip either.

Usage:
    python3 scripts/bench_latency.py
    python3 scripts/bench_latency.py --no-chroma --no-server
    python3 scripts/bench_latency.py --count 500 --concurrency 4

Requires no external dependencies — uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import atexit
import json
import math
import os
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── Project layout ───────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

CHROMA_BIN = PROJECT_ROOT / "preprocessing" / ".venv" / "bin" / "chroma"
CHROMA_PERSIST_DIR = PROJECT_ROOT / "preprocessing" / "output" / "chromadb"
CARGO_TOML = PROJECT_ROOT / "Cargo.toml"
RELEASE_BIN = PROJECT_ROOT / "target" / "release" / "text-to-verse"
DEFAULT_FLAMEGRAPH_OUTPUT = PROJECT_ROOT / "target" / "flamegraph.svg"


def _find_free_port() -> int:
    """Bind to port 0, let the OS pick a free port, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


# ── Question pool ────────────────────────────────────────────────────────
# Diverse questions someone might ask a bible-verse lookup system.

QUESTIONS = [
    "How do I find peace when I'm anxious?",
    "What should I do when I feel lost?",
    "How can I forgive someone who hurt me?",
    "Why do bad things happen to good people?",
    "How do I deal with grief and loss?",
    "What is the meaning of life?",
    "How can I be a better parent?",
    "How do I find strength in difficult times?",
    "What does it mean to love unconditionally?",
    "How should I handle anger?",
    "Is it okay to doubt my faith?",
    "How do I overcome fear?",
    "What should I do when I feel alone?",
    "How can I find hope in a dark world?",
    "What does it mean to be humble?",
    "How do I make wise decisions?",
    "What should I do when I'm tempted?",
    "How do I deal with jealousy?",
    "What is true happiness?",
    "How can I help those in need?",
    "How do I handle financial stress?",
    "What should I do when I feel inadequate?",
    "How do I build stronger relationships?",
    "What does courage look like?",
    "How can I find purpose in my work?",
    "How do I deal with betrayal?",
    "What should I teach my children?",
    "How do I stay patient?",
    "What does it mean to have integrity?",
    "How can I overcome addiction?",
    "How do I deal with loneliness?",
    "What should I do when facing injustice?",
    "How do I find rest when life is overwhelming?",
    "What does generosity look like?",
    "How can I be more compassionate?",
    "How do I handle criticism?",
    "What should I do when I feel guilty?",
    "How do I stop worrying about the future?",
    "What is the value of suffering?",
    "How can I grow as a person?",
    "What should I do when my marriage is struggling?",
    "How do I find contentment?",
    "What does faith look like in everyday life?",
    "How can I be a good friend?",
    "How do I deal with regret?",
    "What should I do when I feel burned out?",
    "How do I practice gratitude?",
    "What does mercy mean?",
    "How can I protect my mental health?",
    "How do I find joy in simple things?",
]


# ── Process management ───────────────────────────────────────────────────

# Child processes we launched that need cleanup.
_children: list[subprocess.Popen] = []

# If set, this process is the cargo-flamegraph wrapper and needs SIGINT
# (not SIGTERM) so it can finalize the SVG before exiting.
_flamegraph_proc: subprocess.Popen | None = None
_flamegraph_output: Path | None = None


def _cleanup_children() -> None:
    """Terminate then kill any child processes we started."""
    global _flamegraph_proc

    # Handle the flamegraph process first — it needs SIGINT + extra time
    # to fold stacks and write the SVG.
    if _flamegraph_proc is not None and _flamegraph_proc.poll() is None:
        print("\nSending SIGINT to cargo-flamegraph to finalize SVG …")
        _flamegraph_proc.send_signal(signal.SIGINT)
        try:
            _flamegraph_proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _flamegraph_proc.kill()
            _flamegraph_proc.wait()
        _flamegraph_proc = None

    for proc in _children:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


atexit.register(_cleanup_children)


# Also handle SIGINT / SIGTERM so Ctrl-C cleans up nicely.
def _signal_handler(sig: int, _frame: object) -> None:
    _cleanup_children()
    sys.exit(128 + sig)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def _wait_for_url(url: str, label: str, timeout: float = 60) -> None:
    """Poll *url* until it returns a 2xx, or give up after *timeout* seconds."""
    deadline = time.monotonic() + timeout
    last_err = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3):
                return
        except Exception as e:
            last_err = str(e)
            time.sleep(0.5)
    print(f"ERROR: {label} did not become ready at {url} within {timeout:.0f}s")
    print(f"       Last error: {last_err}")
    sys.exit(1)


def _is_listening(url: str) -> bool:
    """Quick check whether *url* is reachable."""
    try:
        with urllib.request.urlopen(url, timeout=3):
            return True
    except Exception:
        return False


# ── ChromaDB ─────────────────────────────────────────────────────────────


def ensure_chromadb(host: str, port: int) -> None:
    """Make sure ChromaDB is reachable, starting it if necessary."""
    heartbeat_url = f"http://{host}:{port}/api/v2/heartbeat"

    if _is_listening(heartbeat_url):
        print(f"ChromaDB already running at {host}:{port}")
        return

    # Validate prerequisites.
    if not CHROMA_BIN.exists():
        print(f"ERROR: chroma binary not found at {CHROMA_BIN}")
        print("       Run:  cd preprocessing && uv sync")
        sys.exit(1)
    if not CHROMA_PERSIST_DIR.exists():
        print(f"ERROR: ChromaDB persist directory not found at {CHROMA_PERSIST_DIR}")
        print("       Run the preprocessing pipeline first (stages 1–4).")
        sys.exit(1)

    print(f"Starting ChromaDB on {host}:{port} …")
    log = open(PROJECT_ROOT / "target" / "chromadb_bench.log", "w")
    proc = subprocess.Popen(
        [
            str(CHROMA_BIN),
            "run",
            "--path",
            str(CHROMA_PERSIST_DIR),
            "--host",
            host,
            "--port",
            str(port),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(PROJECT_ROOT),
    )
    _children.append(proc)

    _wait_for_url(heartbeat_url, "ChromaDB")
    print(f"ChromaDB ready (pid {proc.pid})")


# ── text-to-verse server ────────────────────────────────────────────────


def _server_trailing_args(
    chromadb_url: str,
    collection: str,
    ollama_url: str,
    embedding_model: str,
    server_port: int,
) -> list[str]:
    """Build the CLI arguments passed to the text-to-verse binary."""
    return [
        "--chromadb-url",
        chromadb_url,
        "--collection",
        collection,
        "--ollama-url",
        ollama_url,
        "--embedding-model",
        embedding_model,
        "serve",
        "--port",
        str(server_port),
    ]


def ensure_server(
    server_url: str,
    server_port: int,
    chromadb_url: str,
    collection: str,
    ollama_url: str,
    embedding_model: str,
    *,
    flamegraph: bool = False,
    flamegraph_output: Path = DEFAULT_FLAMEGRAPH_OUTPUT,
) -> None:
    """Make sure the text-to-verse HTTP server is reachable, building and
    starting it if necessary.

    When *flamegraph* is True the server is launched via
    ``cargo flamegraph`` so that a CPU flamegraph SVG is written on
    teardown.
    """
    global _flamegraph_proc, _flamegraph_output

    health_url = f"{server_url}/health"

    if _is_listening(health_url):
        print(f"text-to-verse server already running at {server_url}")
        if flamegraph:
            print("WARNING: --flamegraph ignored — using the already-running server")
        return

    trailing = _server_trailing_args(
        chromadb_url,
        collection,
        ollama_url,
        embedding_model,
        server_port,
    )

    log = open(PROJECT_ROOT / "target" / "text_to_verse_bench.log", "w")

    if flamegraph:
        print(f"Starting text-to-verse via cargo flamegraph on port {server_port} …")
        cmd = [
            "cargo",
            "flamegraph",
            "--root",
            "-o",
            str(flamegraph_output),
            "--",
        ] + trailing
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
        )
        _flamegraph_proc = proc
        _flamegraph_output = flamegraph_output
        # Also register so generic cleanup catches it if something goes
        # wrong before the benchmark finishes.
        _children.append(proc)
    else:
        # Build if needed.
        if not RELEASE_BIN.exists() or _source_newer_than_binary():
            print("Building text-to-verse (release) …")
            result = subprocess.run(
                ["cargo", "build", "--release"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print("ERROR: cargo build --release failed:")
                print(result.stderr)
                sys.exit(1)
            print("Build complete.")

        if not RELEASE_BIN.exists():
            print(f"ERROR: release binary not found at {RELEASE_BIN}")
            sys.exit(1)

        print(f"Starting text-to-verse server on port {server_port} …")
        proc = subprocess.Popen(
            [str(RELEASE_BIN)] + trailing,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
        )
        _children.append(proc)

    _wait_for_url(health_url, "text-to-verse server", timeout=120)
    print(f"text-to-verse server ready (pid {proc.pid})")


def _source_newer_than_binary() -> bool:
    """Return True if any .rs or Cargo.toml file is newer than the release binary."""
    if not RELEASE_BIN.exists():
        return True
    bin_mtime = RELEASE_BIN.stat().st_mtime
    for path in (PROJECT_ROOT / "src").rglob("*.rs"):
        if path.stat().st_mtime > bin_mtime:
            return True
    if CARGO_TOML.exists() and CARGO_TOML.stat().st_mtime > bin_mtime:
        return True
    return False


# ── Benchmark helpers ────────────────────────────────────────────────────


def percentile(sorted_data: list[float], p: float) -> float:
    """Compute the p-th percentile (0–100) from pre-sorted data."""
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    return sorted_data[int(f)] * (c - k) + sorted_data[int(c)] * (k - f)


def send_query(url: str, question: str) -> tuple[float, bool, str]:
    """
    Send a single query to the server.

    Returns (latency_seconds, success, detail).
    """
    encoded = urllib.parse.urlencode({"q": question})
    full_url = f"{url}/query?{encoded}"
    start = time.perf_counter()
    try:
        req = urllib.request.Request(full_url, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            elapsed = time.perf_counter() - start
            # Quick sanity check — response should be JSON with a verse_id.
            data = json.loads(body)
            verse_id = data.get("verse_id", "?")
            return (elapsed, True, verse_id)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        elapsed = time.perf_counter() - start
        return (elapsed, False, str(e))


def fmt_ms(seconds: float) -> str:
    """Format seconds as milliseconds with 1 decimal place."""
    return f"{seconds * 1000:.1f} ms"


# ── Benchmark ────────────────────────────────────────────────────────────


def run_benchmark(base_url: str, count: int, concurrency: int, warmup: int) -> None:
    url = base_url.rstrip("/")

    # ── Connectivity check ───────────────────────────────────────────
    print()
    print(f"Target:      {url}")
    print(f"Queries:     {count}")
    print(f"Concurrency: {concurrency}")
    print(f"Warmup:      {warmup}")
    print()

    try:
        urllib.request.urlopen(f"{url}/health", timeout=5)
    except Exception as e:
        print(f"ERROR: Cannot reach {url}/health — is the server running?")
        print(f"       {e}")
        sys.exit(1)

    # ── Warmup ───────────────────────────────────────────────────────
    if warmup > 0:
        print(f"Warming up ({warmup} queries) …", end="", flush=True)
        for i in range(warmup):
            send_query(url, QUESTIONS[i % len(QUESTIONS)])
        print(" done.\n")

    # ── Benchmark ────────────────────────────────────────────────────
    latencies: list[float] = []
    errors = 0
    unique_verses: set[str] = set()

    progress_interval = max(1, count // 20)  # update ~20 times

    print(f"Running {count} queries …\n")
    wall_start = time.perf_counter()

    if concurrency <= 1:
        # Sequential mode — simpler, no thread overhead.
        for i in range(count):
            question = QUESTIONS[i % len(QUESTIONS)]
            elapsed, ok, detail = send_query(url, question)
            if ok:
                latencies.append(elapsed)
                unique_verses.add(detail)
            else:
                errors += 1
            if (i + 1) % progress_interval == 0 or i == count - 1:
                pct = (i + 1) / count * 100
                print(
                    f"  [{pct:5.1f}%]  {i + 1:>{len(str(count))}}/{count}"
                    f"  last={fmt_ms(elapsed):>10}"
                    f"  errors={errors}",
                    flush=True,
                )
    else:
        # Concurrent mode.
        completed = 0
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(send_query, url, QUESTIONS[i % len(QUESTIONS)]): i
                for i in range(count)
            }
            for future in as_completed(futures):
                elapsed, ok, detail = future.result()
                if ok:
                    latencies.append(elapsed)
                    unique_verses.add(detail)
                else:
                    errors += 1
                completed += 1
                if completed % progress_interval == 0 or completed == count:
                    pct = completed / count * 100
                    print(
                        f"  [{pct:5.1f}%]  {completed:>{len(str(count))}}/{count}"
                        f"  last={fmt_ms(elapsed):>10}"
                        f"  errors={errors}",
                        flush=True,
                    )

    wall_elapsed = time.perf_counter() - wall_start

    # ── Results ──────────────────────────────────────────────────────
    print()
    print("=" * 56)
    print("  LATENCY BENCHMARK RESULTS")
    print("=" * 56)
    print()

    if not latencies:
        print("No successful queries — nothing to report.")
        sys.exit(1)

    latencies.sort()
    n = len(latencies)

    print(f"  Successful:     {n}")
    print(f"  Errors:         {errors}")
    print(f"  Unique verses:  {len(unique_verses)}")
    print(f"  Wall clock:     {wall_elapsed:.2f} s")
    print(f"  Throughput:     {n / wall_elapsed:.1f} queries/s")
    print()
    print("  Latency (per query):")
    print(f"    Min:          {fmt_ms(latencies[0]):>10}")
    print(f"    Max:          {fmt_ms(latencies[-1]):>10}")
    print(f"    Mean:         {fmt_ms(statistics.mean(latencies)):>10}")
    print(f"    Median (p50): {fmt_ms(percentile(latencies, 50)):>10}")
    print(
        f"    Stddev:       {fmt_ms(statistics.stdev(latencies) if n > 1 else 0):>10}"
    )
    print(f"    p90:          {fmt_ms(percentile(latencies, 90)):>10}")
    print(f"    p95:          {fmt_ms(percentile(latencies, 95)):>10}")
    print(f"    p99:          {fmt_ms(percentile(latencies, 99)):>10}")
    print()

    # ── Histogram ────────────────────────────────────────────────────
    print("  Latency distribution:")
    bucket_boundaries_ms = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000]
    buckets: list[tuple[str, int]] = []
    prev = 0.0
    for boundary in bucket_boundaries_ms:
        label = f"    {prev:>7.0f}-{boundary:<5.0f} ms"
        cnt = sum(1 for t in latencies if prev / 1000 <= t < boundary / 1000)
        buckets.append((label, cnt))
        prev = boundary
    # overflow bucket
    label = f"    {prev:>7.0f}+      ms"
    cnt = sum(1 for t in latencies if t >= prev / 1000)
    buckets.append((label, cnt))

    max_count = max(c for _, c in buckets) if buckets else 1
    bar_width = 30
    for label, cnt in buckets:
        if cnt == 0:
            continue
        bar_len = max(1, round(cnt / max_count * bar_width)) if cnt > 0 else 0
        bar = "#" * bar_len
        pct = cnt / n * 100
        print(f"  {label}  {bar:<{bar_width}}  {cnt:>5}  ({pct:5.1f}%)")

    print()
    print("=" * 56)


# ── CLI ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Latency benchmark for the text-to-verse HTTP server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "By default, the script launches ChromaDB and text-to-verse as\n"
            "child processes (and tears them down on exit). Use --no-chroma\n"
            "and/or --no-server if they are already running externally."
        ),
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Base URL of the text-to-verse server (derived from --server-port if not set)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1000,
        help="Number of queries to send (default: 1000)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of concurrent requests (default: 1, sequential)",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=5,
        help="Number of warmup queries before timing (default: 5)",
    )

    # ── Service management flags ─────────────────────────────────────
    parser.add_argument(
        "--no-chroma",
        action="store_true",
        help="Do not start ChromaDB — assume it is already running",
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="Do not start the text-to-verse server — assume it is already running",
    )
    parser.add_argument(
        "--flamegraph",
        action="store_true",
        help="Launch the server via cargo flamegraph and write an SVG on exit",
    )
    parser.add_argument(
        "--flamegraph-output",
        default=str(DEFAULT_FLAMEGRAPH_OUTPUT),
        help=f"Path for the flamegraph SVG (default: {DEFAULT_FLAMEGRAPH_OUTPUT.relative_to(PROJECT_ROOT)})",
    )
    parser.add_argument(
        "--chroma-host",
        default="localhost",
        help="ChromaDB listen host (default: localhost)",
    )
    parser.add_argument(
        "--chroma-port",
        type=int,
        default=0,
        help="ChromaDB listen port (default: random free port)",
    )
    parser.add_argument(
        "--server-port",
        type=int,
        default=0,
        help="text-to-verse listen port (default: random free port)",
    )
    parser.add_argument(
        "--collection",
        default="verse_embeddings",
        help="ChromaDB collection name (default: verse_embeddings)",
    )
    parser.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
        help="Ollama server URL for embeddings (default: http://localhost:11434)",
    )
    parser.add_argument(
        "--embedding-model",
        default="qwen3-embedding:0.6b",
        help="Embedding model name (default: qwen3-embedding:0.6b)",
    )

    args = parser.parse_args()

    if args.count < 1:
        print("ERROR: --count must be at least 1")
        sys.exit(1)
    if args.concurrency < 1:
        print("ERROR: --concurrency must be at least 1")
        sys.exit(1)

    # ── Assign random ports if not specified ─────────────────────────
    if not args.no_chroma and args.chroma_port == 0:
        args.chroma_port = _find_free_port()
    if not args.no_server and args.server_port == 0:
        args.server_port = _find_free_port()

    chromadb_url = f"http://{args.chroma_host}:{args.chroma_port}"

    if args.url is not None:
        server_url = args.url.rstrip("/")
    else:
        server_url = f"http://localhost:{args.server_port}"

    # ── Ensure services are up ───────────────────────────────────────
    if not args.no_chroma:
        ensure_chromadb(args.chroma_host, args.chroma_port)

    if args.flamegraph and args.no_server:
        print("ERROR: --flamegraph and --no-server are mutually exclusive")
        sys.exit(1)

    if not args.no_server:
        ensure_server(
            server_url=server_url,
            server_port=args.server_port,
            chromadb_url=chromadb_url,
            collection=args.collection,
            ollama_url=args.ollama_url,
            embedding_model=args.embedding_model,
            flamegraph=args.flamegraph,
            flamegraph_output=Path(args.flamegraph_output),
        )

    # ── Run the benchmark ────────────────────────────────────────────
    run_benchmark(server_url, args.count, args.concurrency, args.warmup)

    # ── Finalize flamegraph ──────────────────────────────────────────
    global _flamegraph_proc
    if args.flamegraph and _flamegraph_proc is not None:
        fg_output = Path(args.flamegraph_output)
        print(f"\nSending SIGINT to cargo-flamegraph to finalize SVG …")
        _flamegraph_proc.send_signal(signal.SIGINT)
        try:
            _flamegraph_proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            print("WARNING: cargo-flamegraph did not exit in time, killing")
            _flamegraph_proc.kill()
            _flamegraph_proc.wait()
        # Remove from _children so generic cleanup doesn't double-signal.
        if _flamegraph_proc in _children:
            _children.remove(_flamegraph_proc)
        _flamegraph_proc = None

        if fg_output.exists():
            print(f"Flamegraph written to: {fg_output}")
        else:
            print(f"WARNING: expected flamegraph at {fg_output} but file not found")


if __name__ == "__main__":
    main()
