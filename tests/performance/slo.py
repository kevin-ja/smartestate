"""
SLO of the inference API and the reports printed at the end of a load test (tests/performance/README.md).

The target comes from new_arch.md §5: up to 1 second per click. It is judged on the time spent inside the service
(Server-Timing header), because from the laptop the network to us-east-1 adds hundreds of milliseconds that are not
the infrastructure's. The client time is reported too, next to the /health heartbeat, which is almost pure network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

P95_TARGET_MS = float(os.getenv("SLO_P95_TARGET_MS", "1000"))
MIN_SUCCESS_RATIO = float(os.getenv("SLO_MIN_SUCCESS_RATIO", "0.99"))

# Names the locustfile records under; the reports look them up with these same names.
CLICK_METRIC = "GET /recommendations"
SERVER_METRIC = "server time (Server-Timing)"
HEALTH_METRIC = "GET /health (heartbeat)"


@dataclass(frozen=True)
class Measure:
    """What one stretch of the test measured."""

    users: int
    seconds: float
    clicks: int
    failures: int
    server_samples: int
    client_p50: float
    client_p95: float
    server_p50: float
    server_p95: float
    server_p99: float
    health_p50: float

    @property
    def rps(self) -> float:
        return self.clicks / self.seconds if self.seconds else 0.0

    @property
    def success_ratio(self) -> float:
        return (self.clicks - self.failures) / self.clicks if self.clicks else 0.0

    @property
    def passed(self) -> bool:
        # Without Server-Timing there is nothing to judge: an empty p95 would read as 0 ms and pass.
        return self.server_samples > 0 and self.success_ratio >= MIN_SUCCESS_RATIO and self.server_p95 < P95_TARGET_MS


def measure(stats, users: int, seconds: float) -> Measure:
    """Reads Locust's stats (environment.stats) into a Measure."""
    click = stats.get(CLICK_METRIC, "GET")
    server = stats.get(SERVER_METRIC, "TIMING")
    health = stats.get(HEALTH_METRIC, "GET")

    def p(entry, q: float) -> float:
        return float(entry.get_response_time_percentile(q) or 0) if entry.num_requests else 0.0

    return Measure(
        users=users,
        seconds=seconds,
        clicks=click.num_requests,
        failures=click.num_failures,
        server_samples=server.num_requests,
        client_p50=p(click, 0.50),
        client_p95=p(click, 0.95),
        server_p50=p(server, 0.50),
        server_p95=p(server, 0.95),
        server_p99=p(server, 0.99),
        health_p50=p(health, 0.50),
    )


def _line(char: str = "-") -> str:
    return char * 78


def baseline_report(m: Measure) -> str:
    return "\n".join([
        "",
        _line("="),
        " LÍNEA BASE",
        _line("="),
        f" Usuarios                  : {m.users}",
        f" Clics                     : {m.clicks}   ({m.rps:.1f} req/s)",
        f" Tasa de éxito             : {m.success_ratio:.2%}   (mínimo {MIN_SUCCESS_RATIO:.0%})",
        f" Mediciones del servidor   : {m.server_samples}{'   (la API no envía Server-Timing)' if not m.server_samples else ''}",
        f" Servidor   p50 / p95 / p99: {m.server_p50:.0f} / {m.server_p95:.0f} / {m.server_p99:.0f} ms"
        f"   (objetivo p95 < {P95_TARGET_MS:.0f} ms)",
        f" Cliente    p50 / p95      : {m.client_p50:.0f} / {m.client_p95:.0f} ms   (incluye la red)",
        f" Red (/health) p50         : {m.health_p50:.0f} ms",
        _line("="),
        f" RESULTADO: {'SLO CUMPLIDO' if m.passed else 'SLO NO CUMPLIDO'}",
        _line("="),
        "",
    ])


def stress_report(steps: list[Measure]) -> tuple[str, Measure | None]:
    """One row per step and the capacity: the last step that still meets the SLO, before the first that fails."""
    rows = [
        "",
        _line("="),
        " ESTRÉS POR ESCALONES",
        _line("="),
        " usuarios  req/s   éxito    servidor p50/p95   cliente p95   red p50   SLO",
    ]
    capacity = None
    broken = False
    for m in steps:
        rows.append(
            f" {m.users:>8}  {m.rps:>5.1f}  {m.success_ratio:>6.1%}   {m.server_p50:>6.0f} / {m.server_p95:>6.0f} ms"
            f"   {m.client_p95:>8.0f} ms  {m.health_p50:>5.0f} ms   {'✓' if m.passed else '✗'}"
        )
        if m.passed and not broken:
            capacity = m
        elif not m.passed:
            broken = True
    rows.append(_line())
    if capacity is None:
        rows.append(" CAPACIDAD: ningún escalón cumple el SLO.")
    else:
        rows.append(
            f" CAPACIDAD: {capacity.users} usuarios, {capacity.rps:.1f} req/s,"
            f" con p95 de servidor {capacity.server_p95:.0f} ms (< {P95_TARGET_MS:.0f} ms)."
        )
        if not broken:
            rows.append(" Ningún escalón falló: el límite está más arriba. Subir STRESS_STEPS.")
    rows += [_line("="), ""]
    return "\n".join(rows), capacity
