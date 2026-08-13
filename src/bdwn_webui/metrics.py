"""Prometheus text exposition of a report, served by /metrics.

Scraped straight from the backup server, this gives alerting on backups that
stopped arriving without needing any passphrase.
"""

from collections.abc import Callable

from bdwn_webui.scan import Report, Repository, Status

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# One metric per line of this table: (name, type, help, value getter).
_REPO_METRICS: tuple[tuple[str, str, str, Callable[[Repository], int]], ...] = (
    ("bdwn_repo_size_bytes", "gauge", "Size of the repository on disk", lambda r: r.size_bytes),
    ("bdwn_repo_files", "gauge", "Number of files in the repository", lambda r: r.files),
    ("bdwn_repo_snapshots", "gauge", "Number of snapshots held", lambda r: r.snapshots),
    ("bdwn_repo_locks", "gauge", "Number of restic locks held", lambda r: r.locks),
)


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(repo: Repository) -> str:
    """Labels of one repository. Two jails can hold the same server name, so
    the pair is what identifies a series.
    """
    return f'{{server="{_label(repo.name)}",root="{_label(repo.root)}"}}'


def _metric(name: str, kind: str, description: str, samples: list[str]) -> list[str]:
    return [f"# HELP {name} {description}", f"# TYPE {name} {kind}", *samples]


def render(report: Report) -> str:
    """Return the whole report in the Prometheus text format."""
    lines: list[str] = []
    for name, kind, description, getter in _REPO_METRICS:
        samples = [f"{name}{_labels(repo)} {getter(repo)}" for repo in report.repositories]
        lines += _metric(name, kind, description, samples)

    lines += _metric(
        "bdwn_repo_last_snapshot_timestamp_seconds",
        "gauge",
        "Time of the most recent snapshot, absent when there is none",
        [
            f"bdwn_repo_last_snapshot_timestamp_seconds{_labels(repo)} {repo.last_snapshot:.0f}"
            for repo in report.repositories
            if repo.last_snapshot is not None
        ],
    )
    lines += _metric(
        "bdwn_repo_status",
        "gauge",
        "Repository state, 1 on the current status of each server",
        [
            f'bdwn_repo_status{{server="{_label(repo.name)}",root="{_label(repo.root)}"'
            f',status="{status}"}} {int(repo.status == status)}'
            for repo in report.repositories
            for status in Status
        ],
    )

    # One sample per filesystem, but HELP and TYPE only once per metric name.
    if report.volumes:
        lines += _metric(
            "bdwn_volume_size_bytes",
            "gauge",
            "Size of a filesystem holding repositories",
            [
                f'bdwn_volume_size_bytes{{path="{_label(v.path)}"}} {v.total_bytes}'
                for v in report.volumes
            ],
        )
        lines += _metric(
            "bdwn_volume_free_bytes",
            "gauge",
            "Free space on a filesystem holding repositories",
            [
                f'bdwn_volume_free_bytes{{path="{_label(v.path)}"}} {v.free_bytes}'
                for v in report.volumes
            ],
        )

    lines += _metric(
        "bdwn_scan_duration_seconds",
        "gauge",
        "Time taken by the last scan of the repositories",
        [f"bdwn_scan_duration_seconds {report.scan_seconds:.3f}"],
    )
    lines += _metric(
        "bdwn_scan_timestamp_seconds",
        "gauge",
        "Time of the last scan of the repositories",
        [f"bdwn_scan_timestamp_seconds {report.generated_at:.0f}"],
    )
    return "\n".join(lines) + "\n"
