"""Rendering of the HTML status page.

The template gets strings only: every figure is formatted here, so the page
itself stays a dumb layout.
"""

from pathlib import Path
from socket import gethostname
from typing import Any

from jinja2 import Environment, FileSystemLoader

from bdwn_webui.format import human_age, human_bytes, local_time
from bdwn_webui.scan import Report, Repository, Status

_TEMPLATE = "status.html.j2"
_ENVIRONMENT = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)

# Wording of each status on the page, in plain words rather than jargon.
_LABELS = {
    Status.OK: "ok",
    Status.LATE: "late",
    Status.STALE: "stale",
    Status.EMPTY: "no snapshot",
    Status.UNINITIALIZED: "no repo",
    Status.UNREADABLE: "error",
}
# Status is declared worst first, so its position is the display priority.
_SEVERITY = {status: rank for rank, status in enumerate(Status)}
_ALERTS = (Status.LATE, Status.STALE, Status.UNREADABLE)


def _repository_view(repo: Repository, largest: int) -> dict[str, Any]:
    return {
        "name": repo.name,
        "status": str(repo.status),
        "label": _LABELS[repo.status],
        "size": human_bytes(repo.size_bytes),
        # Bar scaled to the largest repository, so sizes compare at a glance.
        "bar": round(100.0 * repo.size_bytes / largest, 1) if largest else 0.0,
        "snapshots": repo.snapshots,
        "files": repo.files,
        "locks": repo.locks,
        "last": local_time(repo.last_snapshot),
        "age": human_age(repo.age_seconds),
        "error": repo.error,
    }


def _volume_view(report: Report) -> dict[str, Any] | None:
    volume = report.volume
    if volume is None:
        return None
    percent = round(volume.used_percent, 1)
    return {
        "total": human_bytes(volume.total_bytes),
        "used": human_bytes(volume.used_bytes),
        "free": human_bytes(volume.free_bytes),
        "used_percent": percent,
        "level": "crit" if percent >= 90 else "warn" if percent >= 75 else "",
    }


def render(report: Report, *, refresh: int) -> str:
    """Return the full status page for a report."""
    repositories = sorted(report.repositories, key=lambda r: (_SEVERITY[r.status], r.name))
    largest = max((repo.size_bytes for repo in report.repositories), default=0)
    context = {
        "hostname": gethostname(),
        "server_root": report.server_root,
        "generated": local_time(report.generated_at),
        "scan_seconds": f"{report.scan_seconds:.2f}",
        "refresh": refresh,
        "error": report.error,
        "total_size": human_bytes(report.total_bytes),
        "alerts": report.count(*_ALERTS),
        "totals": {
            "repositories": len(report.repositories),
            "ok": report.count(Status.OK),
        },
        "volume": _volume_view(report),
        "repositories": [_repository_view(repo, largest) for repo in repositories],
        "stale_hours": f"{report.stale_hours:g}",
        "critical_hours": f"{report.critical_hours:g}",
    }
    return _ENVIRONMENT.get_template(_TEMPLATE).render(**context)
