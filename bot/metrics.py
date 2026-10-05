"""متریک‌های داخل پردازه + خروجی Prometheus.

متریک‌های «حقیقت» (سفارش در دقیقه، نرخ برگشت، طول صف) از پایگاه داده خوانده می‌شوند تا بین چند نمونه
درست باشند؛ متریک‌های لحظه‌ای این پردازه (تأخیر API، خطاها، CPU/RAM) اینجا جمع می‌شوند.
"""
from __future__ import annotations

import os
import shutil
import time
from collections import defaultdict, deque
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


class Window:
    """نمونه‌های اخیر (حداکثر N) برای p50/p95."""

    def __init__(self, size: int = 500):
        self.samples: deque[tuple[float, float]] = deque(maxlen=size)  # (زمان، مقدار)
        self.count = 0
        self.sum = 0.0

    def add(self, v: float) -> None:
        self.samples.append((time.time(), v))
        self.count += 1
        self.sum += v

    def recent(self, seconds: float = 300) -> list[float]:
        t = time.time() - seconds
        return [v for ts, v in self.samples if ts >= t]

    def pct(self, p: float, seconds: float = 300) -> float | None:
        vals = sorted(self.recent(seconds))
        if not vals:
            return None
        k = min(len(vals) - 1, max(0, int(round(p / 100 * (len(vals) - 1)))))
        return vals[k]


class Metrics:
    def __init__(self):
        self.started = time.time()
        self.counters: dict[str, float] = defaultdict(float)
        self.events: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=5000))  # زمان رخدادها برای نرخ
        self.api_latency = Window()
        self.job_latency: dict[str, Window] = defaultdict(Window)
        self.api_errors: deque[tuple[float, str]] = deque(maxlen=50)  # (زمان، کد) برای صفحه‌ی عیب‌یابی
        self.rate_limit: dict[str, Any] = {}
        self.gauges: dict[str, float] = {}

    def inc(self, name: str, n: float = 1) -> None:
        self.counters[name] += n
        self.events[name].append(time.time())

    def rate(self, name: str, seconds: float = 300) -> int:
        t = time.time() - seconds
        return sum(1 for x in self.events[name] if x >= t)

    def observe_api(self, method: str, path: str, status: int, seconds: float, headers: Any = None,
                    code: str | None = None) -> None:
        self.api_latency.add(seconds)
        self.inc("api_requests_total")
        if status == 0 or status >= 500 or status == 429:
            self.inc("api_errors_total")
            self.api_errors.append((time.time(), f"{method} {_route(path)} → {status} {code or ''}".strip()))
        elif status >= 400:
            self.inc("api_client_errors_total")
            self.api_errors.append((time.time(), f"{method} {_route(path)} → {status} {code or ''}".strip()))
        if headers is not None:
            for h in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"):
                if h in headers:
                    self.rate_limit[h] = headers[h]
            self.rate_limit["at"] = time.time()

    def observe_job(self, kind: str, seconds: float) -> None:
        self.job_latency[kind].add(seconds)
        self.inc("jobs_processed_total")

    def uptime(self) -> float:
        return time.time() - self.started


def _route(path: str) -> str:
    # شناسه‌ها در متریک‌ها جمع شوند (orders/ord_x → orders/{id})
    parts = path.split("?")[0].strip("/").split("/")
    return "/" + "/".join("{id}" if any(ch.isdigit() for ch in p) and len(p) > 3 else p for p in parts)


def system_snapshot(path: str = ".") -> dict[str, Any]:
    """CPU، RAM، دیسک، شبکه و پردازه."""
    out: dict[str, Any] = {}
    try:
        du = shutil.disk_usage(os.path.abspath(path))
        out.update(disk_total=du.total, disk_used=du.used, disk_free=du.free,
                   disk_percent=round(du.used / du.total * 100, 1) if du.total else 0)
    except OSError:
        pass
    if psutil is None:
        return out
    try:
        vm = psutil.virtual_memory()
        net = psutil.net_io_counters()
        proc = psutil.Process()
        out.update(
            cpu_percent=psutil.cpu_percent(interval=None),
            cpu_count=psutil.cpu_count(),
            load_avg=list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            mem_total=vm.total, mem_used=vm.total - vm.available, mem_percent=vm.percent,
            net_sent=net.bytes_sent, net_recv=net.bytes_recv,
            proc_rss=proc.memory_info().rss, proc_cpu=proc.cpu_percent(interval=None),
            proc_threads=proc.num_threads(), boot_time=psutil.boot_time(),
        )
    except Exception:
        pass
    return out


def human_bytes(n: float | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def prometheus(metrics: Metrics, extra: dict[str, float]) -> str:
    """خروجی متنی Prometheus."""
    lines = []

    def g(name: str, value: Any, help_: str = "", type_: str = "gauge", labels: str = "") -> None:
        if value is None:
            return
        if help_:
            lines.append(f"# HELP stard_{name} {help_}")
            lines.append(f"# TYPE stard_{name} {type_}")
        lines.append(f"stard_{name}{labels} {float(value)}")

    g("uptime_seconds", metrics.uptime(), "Process uptime")
    for name, v in sorted(metrics.counters.items()):
        g(name, v, f"Counter {name}", "counter")
    p95 = metrics.api_latency.pct(95)
    p50 = metrics.api_latency.pct(50)
    g("api_latency_p50_seconds", p50, "Stard API latency p50 (5m)")
    g("api_latency_p95_seconds", p95, "Stard API latency p95 (5m)")
    if metrics.job_latency:
        lines.append("# HELP stard_job_duration_p95_seconds Job duration p95 (5m) by kind")
        lines.append("# TYPE stard_job_duration_p95_seconds gauge")
    for kind, w in sorted(metrics.job_latency.items()):
        v = w.pct(95)
        if v is not None:
            g("job_duration_p95_seconds", v, labels=f'{{kind="{kind}"}}')
    for k, v in sorted(extra.items()):
        g(k, v)
    return "\n".join(lines) + "\n"
