"""Human readable rendering of the figures shown on the status page."""

from time import localtime, strftime

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_bytes(size: int) -> str:
    """Binary size with one decimal above the kibibyte: 1536 -> '1.5 KiB'."""
    value = float(size)
    for unit in _UNITS:
        if value < 1024.0 or unit == _UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} {_UNITS[-1]}"


def human_age(seconds: float | None) -> str:
    """Coarse age of a snapshot: '3d 04h', '12h 30m', '45m', 'never'."""
    if seconds is None:
        return "never"
    if seconds < 60:
        return "just now"
    minutes, hours = int(seconds // 60) % 60, int(seconds // 3600)
    if hours < 1:
        return f"{minutes}m"
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    return f"{hours // 24}d {hours % 24:02d}h"


def local_time(epoch: float | None) -> str:
    """Timestamp in the server's local time, or a dash when there is none."""
    return "-" if epoch is None else strftime("%Y-%m-%d %H:%M", localtime(epoch))
