"""Métricas Prometheus (formato de texto 0.0.4) sin dependencias, un proceso por contenedor.

GET /metrics expone: peticiones HTTP por ruta plantilla/método/estado y su latencia (histograma), conexiones
WebSocket, eventos en vivo publicados/recibidos/derramados, líderes de tareas de fondo y edad del latido.
"""

import threading
import time
from collections.abc import Callable

_lock = threading.Lock()


def _labels(names: tuple[str, ...], values: tuple) -> str:
    if not names:
        return ""
    esc = (str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") for v in values)
    return "{" + ",".join(f'{n}="{v}"' for n, v in zip(names, esc, strict=True)) + "}"


class Counter:
    def __init__(self, name: str, help_: str, labels: tuple[str, ...] = ()):
        self.name, self.help, self.labelnames = name, help_, labels
        self.values: dict[tuple, float] = {}
        REGISTRY.append(self)

    def inc(self, *labels, amount: float = 1.0) -> None:
        with _lock:
            self.values[labels] = self.values.get(labels, 0.0) + amount

    def get(self, *labels) -> float:
        return self.values.get(labels, 0.0)

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        out += [f"{self.name}{_labels(self.labelnames, k)} {v}" for k, v in sorted(self.values.items())]
        return out


class Gauge:
    """Valor fijado a mano o calculado al exportar (fn devuelve {labels: valor} o un número)."""

    def __init__(self, name: str, help_: str, labels: tuple[str, ...] = (), fn: Callable | None = None):
        self.name, self.help, self.labelnames, self.fn = name, help_, labels, fn
        self.values: dict[tuple, float] = {}
        REGISTRY.append(self)

    def set(self, value: float, *labels) -> None:
        with _lock:
            self.values[labels] = value

    def render(self) -> list[str]:
        values = dict(self.values)
        if self.fn:
            got = self.fn()
            values = got if isinstance(got, dict) else {(): got}
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        out += [f"{self.name}{_labels(self.labelnames, k)} {float(v)}" for k, v in sorted(values.items())]
        return out


class Histogram:
    def __init__(self, name: str, help_: str, labels: tuple[str, ...] = (),
                 buckets: tuple[float, ...] = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)):
        self.name, self.help, self.labelnames, self.buckets = name, help_, labels, buckets
        self.series: dict[tuple, list] = {}  # labels -> [counts por bucket..., suma, total]
        REGISTRY.append(self)

    def observe(self, value: float, *labels) -> None:
        with _lock:
            s = self.series.setdefault(labels, [0] * len(self.buckets) + [0.0, 0])
            for i, b in enumerate(self.buckets):
                if value <= b:
                    s[i] += 1
            s[-2] += value
            s[-1] += 1

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for k, s in sorted(self.series.items()):
            for i, b in enumerate(self.buckets):
                out.append(f"{self.name}_bucket{_labels(self.labelnames + ('le',), k + (b,))} {s[i]}")
            out.append(f"{self.name}_bucket{_labels(self.labelnames + ('le',), k + ('+Inf',))} {s[-1]}")
            out.append(f"{self.name}_sum{_labels(self.labelnames, k)} {s[-2]}")
            out.append(f"{self.name}_count{_labels(self.labelnames, k)} {s[-1]}")
        return out


REGISTRY: list = []

http_requests = Counter("wa_http_requests_total", "Peticiones HTTP", ("method", "route", "status"))
http_latency = Histogram("wa_http_request_duration_seconds", "Latencia de peticiones HTTP", ("method", "route"))
realtime_published = Counter("wa_realtime_published_total", "Eventos en vivo publicados por esta réplica", ("mode",))
realtime_received = Counter("wa_realtime_received_total", "Eventos en vivo recibidos de otras réplicas")
realtime_spilled = Counter("wa_realtime_spilled_total", "Eventos grandes enviados por realtime_spill")
realtime_errors = Counter("wa_realtime_errors_total", "Errores publicando o escuchando eventos", ("stage",))
rate_limited = Counter("wa_rate_limited_total", "Peticiones rechazadas por límite de uso", ("scope",))
loop_restarts = Counter("wa_loop_restarts_total", "Reinicios de tareas de fondo por error", ("loop",))
started_at = time.time()
Gauge("wa_process_uptime_seconds", "Segundos desde que arrancó el proceso", fn=lambda: time.time() - started_at)


def render() -> str:
    lines: list[str] = []
    for m in REGISTRY:
        lines += m.render()
    return "\n".join(lines) + "\n"
