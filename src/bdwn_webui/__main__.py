"""Entry point: bdwn-webui [--config FILE] [--listen ADDR] [--dump]."""

from argparse import ArgumentParser
from dataclasses import replace
from json import dumps
from logging import INFO, basicConfig
from pathlib import Path
from sys import argv as sys_argv
from sys import stderr, stdout

from bdwn_webui import __version__
from bdwn_webui.config import DEFAULT_CONFIG_FILE, Config, ConfigError, load
from bdwn_webui.scan import as_dict, scan
from bdwn_webui.server import serve


def _parse_args(argv: list[str]) -> tuple[Config, bool]:
    parser = ArgumentParser(
        prog="bdwn-webui",
        description="Read-only status page for the repositories of a bdwn backup server.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_FILE,
        help=f"configuration file (default: {DEFAULT_CONFIG_FILE})",
    )
    parser.add_argument(
        "--listen",
        help='override BDWN_WEBUI_LISTEN, either "unix:/path/to.sock" or "host:port"',
    )
    parser.add_argument(
        "--dump",
        action="store_true",
        help="print the report as JSON on stdout and exit, without serving anything",
    )
    parser.add_argument("--version", action="version", version=f"bdwn-webui {__version__}")
    args = parser.parse_args(argv)
    config = load(args.config)
    if args.listen:
        config = replace(config, listen=args.listen)
    return config, args.dump


def main(argv: list[str] | None = None) -> int:
    """Serve the status page, or dump one report and exit."""
    basicConfig(level=INFO, format="%(message)s", stream=stderr)
    try:
        config, dump = _parse_args(sys_argv[1:] if argv is None else argv)
        if dump:
            stdout.write(dumps(as_dict(scan(config)), indent=2) + "\n")
            return 0
        serve(config)
    except (ConfigError, ValueError) as exc:
        stderr.write(f"bdwn-webui: {exc}\n")
        return 2
    except OSError as exc:
        stderr.write(f"bdwn-webui: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
