"""HTTP layer of bdwn-webui: three read-only routes, no state, no cookies.

    /            the status page
    /api/status  the same report as JSON
    /metrics     the same report in the Prometheus text format

It listens on a unix socket by default (put a reverse proxy in front of it, or
forward the socket over SSH), and on a TCP address when asked to.
"""

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import dumps
from logging import getLogger
from pathlib import Path
from signal import SIGINT, SIGTERM, signal
from socket import AF_UNIX
from socketserver import TCPServer
from stat import S_ISSOCK
from sys import exc_info
from threading import Lock, Thread
from time import monotonic
from typing import Any

from bdwn_webui import __version__
from bdwn_webui.config import Config
from bdwn_webui.metrics import CONTENT_TYPE as METRICS_TYPE
from bdwn_webui.metrics import render as render_metrics
from bdwn_webui.page import render as render_page
from bdwn_webui.scan import Report, as_dict, scan

_LOGGER = getLogger("bdwn-webui")

# The page reloads itself as often as the cache can hold new figures, kept
# within sane bounds whatever the configured TTL.
_MIN_REFRESH = 30
_MAX_REFRESH = 600


class ReportCache:
    """Last report, rebuilt at most every cache_ttl seconds.

    Scanning walks every pack file of every repository, which is far too
    expensive to redo on each request (and on each browser reload).
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = Lock()
        self._report: Report | None = None
        self._stamp = 0.0

    def get(self) -> Report:
        with self._lock:
            report = self._report
            if report is None or monotonic() - self._stamp >= self._config.cache_ttl:
                report = scan(self._config)
                self._report = report
                self._stamp = monotonic()
                _LOGGER.info(
                    "scanned %d repositories in %.2fs",
                    len(report.repositories),
                    report.scan_seconds,
                )
            return report


class _Handler(BaseHTTPRequestHandler):
    """Read-only request handler. Overridden per run to carry the cache."""

    report_cache: ReportCache
    refresh_seconds: int
    close_connection: bool  # set by BaseHTTPRequestHandler outside __init__

    server_version = f"bdwn-webui/{__version__}"
    sys_version = ""  # do not advertise the Python version
    protocol_version = "HTTP/1.1"
    timeout = 30

    # do_GET and do_HEAD: names imposed by BaseHTTPRequestHandler.
    def do_GET(self) -> None:
        self._dispatch(with_body=True)

    def do_HEAD(self) -> None:
        self._dispatch(with_body=False)

    def _dispatch(self, *, with_body: bool) -> None:
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            match route:
                case "/":
                    report = self.report_cache.get()
                    body = render_page(report, refresh=self.refresh_seconds).encode("utf-8")
                    self._send(HTTPStatus.OK, "text/html; charset=utf-8", body, with_body)
                case "/api/status":
                    payload = dumps(as_dict(self.report_cache.get()), indent=2) + "\n"
                    self._send(
                        HTTPStatus.OK,
                        "application/json; charset=utf-8",
                        payload.encode("utf-8"),
                        with_body,
                    )
                case "/metrics":
                    body = render_metrics(self.report_cache.get()).encode("utf-8")
                    self._send(HTTPStatus.OK, METRICS_TYPE, body, with_body)
                case _:
                    self.send_error(HTTPStatus.NOT_FOUND, "no such page")
        except (ConnectionError, TimeoutError) as exc:
            # Client gone before the answer was written: a browser reloading
            # the page, or a proxy giving up. There is nothing left to answer
            # on, and writing an error would raise the same thing again.
            _LOGGER.debug("client left during %s: %s", route, exc)
            self.close_connection = True
        except Exception:  # pylint: disable=broad-except
            # A broken scan must not kill the server: log it, answer 500.
            _LOGGER.exception("request failed: %s", route)
            try:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "report generation failed")
            except (ConnectionError, TimeoutError):
                self.close_connection = True

    def _send(self, status: HTTPStatus, content_type: str, body: bytes, with_body: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        # The page is self contained: no script, no image, no external asset.
        self.send_header(
            "Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'"
        )
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def version_string(self) -> str:
        # Default is "<server_version> <sys_version>", with a trailing space
        # once the Python version is blanked out.
        return self.server_version

    def address_string(self) -> str:
        # client_address is empty on a unix socket, which would break logging.
        return str(self.client_address[0]) if self.client_address else "unix"

    # Parameter name imposed by BaseHTTPRequestHandler, builtin or not.
    def log_message(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin
        _LOGGER.info("%s %s", self.address_string(), format % args)

    def log_error(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin
        # Mostly idle keep-alive connections hitting the handler timeout, which
        # is routine. Real failures are logged where they happen, and the
        # status code still shows up in the access line above.
        _LOGGER.debug("%s %s", self.address_string(), format % args)


class _QuietErrors:
    """Keeps a client hanging up out of the journal.

    socketserver prints a full traceback for any exception escaping a handler,
    which fills the journal with noise every time a browser or a proxy drops
    the connection.
    """

    def handle_error(self, _request: Any, _client_address: Any) -> None:
        error = exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            _LOGGER.debug("connection dropped: %s", error)
            return
        _LOGGER.exception("unhandled error while serving a request")


class _UnixHTTPServer(_QuietErrors, ThreadingHTTPServer):
    """ThreadingHTTPServer bound to a unix socket instead of a TCP port."""

    address_family = AF_UNIX
    allow_reuse_address = False

    def __init__(self, path: Path, handler: type[_Handler], *, mode: int) -> None:
        self._path = path
        self._mode = mode
        # The server address of an AF_UNIX socket is its path, not a host and
        # a port, which the socketserver type hints do not cover.
        super().__init__(str(path), handler)  # type: ignore[arg-type]

    def server_bind(self) -> None:
        self._unlink_stale()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # HTTPServer.server_bind would try to resolve a host name out of the
        # socket path, so bind through TCPServer and set the names by hand.
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = 0
        self._path.chmod(self._mode)

    def server_close(self) -> None:
        super().server_close()
        self._unlink_stale()

    def _unlink_stale(self) -> None:
        """Remove the socket file, left behind by a crash or by this run."""
        try:
            if S_ISSOCK(self._path.lstat().st_mode):
                self._path.unlink()
        except FileNotFoundError:
            pass


class _TCPHTTPServer(_QuietErrors, ThreadingHTTPServer):
    """Same server on a TCP address, with the same quiet error handling."""


def _tcp_address(listen: str) -> tuple[str, int]:
    host, _, port = listen.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"invalid listen address: {listen} (use host:port or unix:/path)")
    return host.strip("[]"), int(port)


def _build_server(config: Config, handler: type[_Handler]) -> ThreadingHTTPServer:
    server: ThreadingHTTPServer
    if config.is_unix:
        path = Path(config.listen.removeprefix("unix:"))
        server = _UnixHTTPServer(path, handler, mode=config.socket_mode)
        _LOGGER.info("listening on unix socket %s (mode %04o)", path, config.socket_mode)
        return server
    address = _tcp_address(config.listen)
    server = _TCPHTTPServer(address, handler)
    _LOGGER.info("listening on http://%s:%d/", *address)
    return server


def serve(config: Config) -> None:
    """Run the server until SIGTERM or SIGINT."""
    refresh = min(_MAX_REFRESH, max(_MIN_REFRESH, config.cache_ttl))

    class Handler(_Handler):
        """Handler bound to the cache and the refresh interval of this run."""

        report_cache = ReportCache(config)
        refresh_seconds = refresh

    server = _build_server(config, Handler)

    def stop(*_: Any) -> None:
        # shutdown() blocks until serve_forever() returns, so it cannot be
        # called from the thread running it.
        Thread(target=server.shutdown, daemon=True).start()

    signal(SIGTERM, stop)
    signal(SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        _LOGGER.info("stopped")
