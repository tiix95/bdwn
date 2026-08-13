"""Filesystem-level inspection of the restic repositories of a backup server.

Nothing here opens a repository: they are encrypted and this server holds no
passphrase (that is the point of the design). Everything reported comes from
directory entries:

  <root>/<server>/config      the repository exists (restic has initialised it)
  <root>/<server>/snapshots/  one file per snapshot, mtime = when it was taken
  <root>/<server>/locks/      one file while a client is backing up or pruning

Symbolic links are never followed. The backup clients own their repository
directory over SFTP and could drop a link to / in it; a scan following it
would walk the whole filesystem and report a nonsense size.
"""

from dataclasses import dataclass
from enum import StrEnum
from os import DirEntry, scandir, stat, statvfs
from pathlib import Path
from re import compile as re_compile
from time import monotonic, time
from typing import Any

from bdwn_webui.config import Config

# Same charset as the server user names created by bdwn-setup-backup-user.
_SERVER_NAME = re_compile(r"^[a-z0-9_][a-z0-9_-]*$")

_HOUR = 3600.0


class Status(StrEnum):
    """State of one repository, worst-first when sorting."""

    UNREADABLE = "unreadable"  # scan failed, permissions or I/O error
    STALE = "stale"  # last snapshot older than the critical threshold
    LATE = "late"  # last snapshot older than the stale threshold
    EMPTY = "empty"  # repository initialised, no snapshot yet
    UNINITIALIZED = "uninitialized"  # user declared, restic never ran
    OK = "ok"


@dataclass(frozen=True)
class Repository:
    """One backup client's repository, as seen from the filesystem."""

    name: str
    # Jail it was found in. Two jails may hold a repository of the same name,
    # so a repository is only identified by the pair.
    root: str
    status: Status
    size_bytes: int
    files: int
    snapshots: int
    locks: int
    last_snapshot: float | None
    error: str | None = None

    @property
    def age_seconds(self) -> float | None:
        return None if self.last_snapshot is None else max(0.0, time() - self.last_snapshot)


@dataclass(frozen=True)
class Volume:
    """Filesystem holding the repositories."""

    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int

    @property
    def used_percent(self) -> float:
        return 100.0 * self.used_bytes / self.total_bytes if self.total_bytes else 0.0


@dataclass(frozen=True)
class Report:
    """Result of one full scan, cached and served as HTML, JSON and metrics."""

    generated_at: float
    scan_seconds: float
    server_roots: tuple[str, ...]
    stale_hours: float
    critical_hours: float
    repositories: tuple[Repository, ...]
    # One entry per distinct filesystem behind the jails, not per jail.
    volumes: tuple[Volume, ...] = ()
    # Jails that could not be read at all, one message each.
    errors: tuple[str, ...] = ()

    @property
    def total_bytes(self) -> int:
        return sum(repo.size_bytes for repo in self.repositories)

    def count(self, *statuses: Status) -> int:
        return sum(1 for repo in self.repositories if repo.status in statuses)


def _tree_usage(path: Path) -> tuple[int, int]:
    """Return (bytes, files) under path, without following any symlink."""
    total = 0
    files = 0
    stack: list[str] = [str(path)]
    while stack:
        try:
            with scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                        files += 1
        except OSError:
            # A pack file removed by a prune mid-scan, or a directory we
            # cannot read: skip it rather than lose the whole report.
            continue
    return total, files


def _snapshots(path: Path) -> tuple[int, float | None]:
    """Return (count, newest mtime) of the snapshot files of a repository."""
    count = 0
    newest: float | None = None
    try:
        with scandir(path) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                count += 1
                mtime = entry.stat(follow_symlinks=False).st_mtime
                if newest is None or mtime > newest:
                    newest = mtime
    except FileNotFoundError:
        pass
    return count, newest


def _count_files(path: Path) -> int:
    try:
        with scandir(path) as entries:
            return sum(1 for entry in entries if entry.is_file(follow_symlinks=False))
    except FileNotFoundError:
        return 0


def _status(snapshots: int, last: float | None, config: Config) -> Status:
    if snapshots == 0 or last is None:
        return Status.EMPTY
    age = time() - last
    if age <= config.stale_hours * _HOUR:
        return Status.OK
    return Status.LATE if age <= config.critical_hours * _HOUR else Status.STALE


def _scan_repository(path: Path, root: Path, config: Config) -> Repository:
    try:
        initialised = (path / "config").is_file()
        snapshots, last = _snapshots(path / "snapshots")
        locks = _count_files(path / "locks")
        size, files = _tree_usage(path)
    except OSError as exc:
        return Repository(
            name=path.name,
            root=str(root),
            status=Status.UNREADABLE,
            size_bytes=0,
            files=0,
            snapshots=0,
            locks=0,
            last_snapshot=None,
            error=str(exc),
        )
    status = _status(snapshots, last, config) if initialised else Status.UNINITIALIZED
    return Repository(
        name=path.name,
        root=str(root),
        status=status,
        size_bytes=size,
        files=files,
        snapshots=snapshots,
        locks=locks,
        last_snapshot=last,
    )


def _repository_dirs(root: Path) -> list[Path]:
    """Client directories under the jail, skipping links and odd names."""

    def keep(entry: DirEntry[str]) -> bool:
        return entry.is_dir(follow_symlinks=False) and bool(_SERVER_NAME.match(entry.name))

    with scandir(root) as entries:
        return sorted((Path(entry.path) for entry in entries if keep(entry)), key=lambda p: p.name)


def _volume(root: Path) -> tuple[int, Volume] | None:
    """Filesystem holding a jail, with the device id used to deduplicate it."""
    try:
        stats = statvfs(root)
        device = stat(root).st_dev
    except OSError:
        return None
    total = stats.f_blocks * stats.f_frsize
    return device, Volume(
        path=str(root),
        total_bytes=total,
        used_bytes=total - stats.f_bfree * stats.f_frsize,
        free_bytes=stats.f_bavail * stats.f_frsize,
    )


def scan(config: Config) -> Report:
    """Walk every repository of every jail and build a full report."""
    started = monotonic()
    repositories: list[Repository] = []
    volumes: dict[int, Volume] = {}
    errors: list[str] = []
    for root in config.server_roots:
        try:
            directories = _repository_dirs(root)
        except OSError as exc:
            # One unreadable jail (unmounted disk, typo) must not hide the
            # repositories of the others.
            errors.append(f"cannot read {root}: {exc}")
            continue
        repositories += [_scan_repository(path, root, config) for path in directories]
        found = _volume(root)
        # Several jails often sit on the same filesystem: report it once.
        if found is not None and found[0] not in volumes:
            volumes[found[0]] = found[1]
    return Report(
        generated_at=time(),
        scan_seconds=monotonic() - started,
        server_roots=tuple(str(root) for root in config.server_roots),
        stale_hours=config.stale_hours,
        critical_hours=config.critical_hours,
        repositories=tuple(repositories),
        volumes=tuple(volumes.values()),
        errors=tuple(errors),
    )


def as_dict(report: Report) -> dict[str, Any]:
    """JSON-friendly view of a report, served by /api/status."""
    return {
        "generated_at": report.generated_at,
        "scan_seconds": round(report.scan_seconds, 3),
        "server_roots": list(report.server_roots),
        "stale_hours": report.stale_hours,
        "critical_hours": report.critical_hours,
        "errors": list(report.errors),
        "totals": {
            "repositories": len(report.repositories),
            "size_bytes": report.total_bytes,
            "ok": report.count(Status.OK),
            "late": report.count(Status.LATE),
            "stale": report.count(Status.STALE),
            "other": report.count(Status.EMPTY, Status.UNINITIALIZED, Status.UNREADABLE),
        },
        "volumes": [
            {
                "path": volume.path,
                "total_bytes": volume.total_bytes,
                "used_bytes": volume.used_bytes,
                "free_bytes": volume.free_bytes,
                "used_percent": round(volume.used_percent, 1),
            }
            for volume in report.volumes
        ],
        "repositories": [
            {
                "name": repo.name,
                "root": repo.root,
                "status": str(repo.status),
                "size_bytes": repo.size_bytes,
                "files": repo.files,
                "snapshots": repo.snapshots,
                "locks": repo.locks,
                "last_snapshot": repo.last_snapshot,
                "age_seconds": None if repo.age_seconds is None else round(repo.age_seconds),
                "error": repo.error,
            }
            for repo in report.repositories
        ],
    }
