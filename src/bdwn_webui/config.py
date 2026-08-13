"""Configuration of bdwn-webui.

The file is the same plain shell syntax as the other bdwn config files
(KEY="value", one per line), so an admin edits them all the same way. It is
not sourced by a shell here, it is parsed: only KEY=VALUE lines are read and
unknown keys are ignored.
"""

from dataclasses import dataclass, replace
from pathlib import Path
from re import compile as re_compile

DEFAULT_CONFIG_FILE = Path("/etc/bdwn/bdwn-webui.conf")

# KEY=value, as written in the shipped example. Anything else (comments,
# blank lines, shell constructs) is ignored.
_ASSIGNMENT = re_compile(r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")


class ConfigError(Exception):
    """Raised on an unusable value in the configuration file."""


@dataclass(frozen=True)
class Config:
    """Runtime settings, with the defaults used when the file is absent."""

    # Chroot jails holding one repository per backup client. Several of them
    # when the repositories are spread over more than one filesystem, given
    # space separated as the client config does for BACKUP_PATHS.
    server_roots: tuple[Path, ...] = (Path("/backups/servers"),)
    # "unix:/path/to.sock" or "host:port".
    listen: str = "unix:/run/bdwn-webui/bdwn-webui.sock"
    # Mode applied to the unix socket once bound.
    socket_mode: int = 0o660
    # Scanning walks every repository, so results are cached this long.
    cache_ttl: int = 300
    # A repository whose last snapshot is older than this is late, then stale.
    stale_hours: float = 26.0
    critical_hours: float = 72.0

    @property
    def is_unix(self) -> bool:
        return self.listen.startswith("unix:")


def _unquote(raw: str) -> str:
    """Return the value of a shell assignment: quoted string or bare word."""
    value = raw.strip()
    if value[:1] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end > 0 else value[1:]
    # Bare word: stops at the first space or trailing comment, as the shell does.
    words = value.split("#", 1)[0].split()
    return words[0] if words else ""


def _to_paths(value: str) -> tuple[Path, ...]:
    """Split a space separated list of jails, keeping the order, without
    duplicates (the same jail listed twice would show every repository twice).
    """
    paths = dict.fromkeys(Path(word) for word in value.split())
    return tuple(paths)


def _to_int(key: str, value: str, *, base: int = 10) -> int:
    try:
        return int(value, base)
    except ValueError as exc:
        raise ConfigError(f"{key}: not a number: {value}") from exc


def _to_float(key: str, value: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{key}: not a number: {value}") from exc


def parse(text: str) -> Config:
    """Build a Config from the contents of a config file."""
    config = Config()
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if not match:
            continue
        key = match.group("key")
        value = _unquote(match.group("value"))
        if not value:
            continue
        match key:
            case "BDWN_SERVER_ROOT":
                config = replace(config, server_roots=_to_paths(value))
            case "BDWN_WEBUI_LISTEN":
                config = replace(config, listen=value)
            case "BDWN_WEBUI_SOCKET_MODE":
                config = replace(config, socket_mode=_to_int(key, value, base=8))
            case "BDWN_WEBUI_CACHE_TTL":
                config = replace(config, cache_ttl=_to_int(key, value))
            case "BDWN_WEBUI_STALE_HOURS":
                config = replace(config, stale_hours=_to_float(key, value))
            case "BDWN_WEBUI_CRITICAL_HOURS":
                config = replace(config, critical_hours=_to_float(key, value))
    if config.critical_hours < config.stale_hours:
        raise ConfigError(
            "BDWN_WEBUI_CRITICAL_HOURS must not be lower than BDWN_WEBUI_STALE_HOURS"
        )
    return config


def load(path: Path = DEFAULT_CONFIG_FILE) -> Config:
    """Read the config file. A missing file is not an error: defaults apply."""
    try:
        return parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
