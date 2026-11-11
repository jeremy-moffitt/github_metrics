#!/usr/bin/env python3
"""Serve GitHub pull request metrics through a small JSON API."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

import github_pr_metrics

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_CACHE_TTL = 300
DEFAULT_CACHE_SIZE = 128
MAX_MONTHS = 36


class MetricsCache:
    """Keep a bounded, time-limited cache of successful metrics results."""

    def __init__(self, ttl_seconds: int, max_entries: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[
            tuple[str, int], tuple[float, list[dict]]
        ] = OrderedDict()
        self._entry_locks: dict[tuple[str, int], threading.Lock] = {}
        self._lock = threading.Lock()

    def _get(self, key: tuple[str, int]) -> list[dict] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None

            expires_at, result = entry
            if expires_at <= time.monotonic():
                del self._entries[key]
                return None

            self._entries.move_to_end(key)
            return result

    def get_or_compute(
        self, key: tuple[str, int], compute: Callable[[], list[dict]]
    ) -> tuple[list[dict], bool]:
        if self.ttl_seconds == 0:
            return compute(), False

        with self._lock:
            entry_lock = self._entry_locks.setdefault(key, threading.Lock())

        with entry_lock:
            cached = self._get(key)
            if cached is not None:
                return cached, True

            result = compute()
            expires_at = time.monotonic() + self.ttl_seconds
            with self._lock:
                self._entries[key] = (expires_at, result)
                self._entries.move_to_end(key)
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)
            return result, False


class MetricsApplication:
    def __init__(
        self,
        config_path: Path,
        default_months: int,
        cache_ttl: int,
        cache_size: int,
        cors_origin: str | None,
    ) -> None:
        global_token, configured_repositories = github_pr_metrics.load_config(
            config_path
        )
        self.repositories = [
            (repository, token if token is not None else global_token)
            for repository, token in configured_repositories
        ]
        self.repository_tokens = dict(self.repositories)
        self.default_months = default_months
        self.cache = MetricsCache(cache_ttl, cache_size)
        self.cors_origin = cors_origin

    def get_metrics(self, repository: str, months: int) -> tuple[list[dict], bool]:
        token = self.repository_tokens[repository]
        return self.cache.get_or_compute(
            (repository, months),
            lambda: github_pr_metrics.collect_metrics(
                repository, github_pr_metrics.GitHubClient(token), months
            ),
        )


class MetricsHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], application: MetricsApplication):
        self.application = application
        super().__init__(server_address, MetricsRequestHandler)


class MetricsRequestHandler(BaseHTTPRequestHandler):
    server: MetricsHTTPServer

    def _send_json(self, status: HTTPStatus, body: dict) -> None:
        response = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")

        allowed_origin = self.server.application.cors_origin
        request_origin = self.headers.get("Origin")
        if allowed_origin == "*" or request_origin == allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(response)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        allowed_origin = self.server.application.cors_origin
        request_origin = self.headers.get("Origin")
        if allowed_origin == "*" or request_origin == allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed_url = urlsplit(self.path)
        path_parts = [unquote(part) for part in parsed_url.path.strip("/").split("/")]

        if path_parts == ["api", "repositories"]:
            self._send_json(
                HTTPStatus.OK,
                {
                    "repositories": [
                        {
                            "name": repository,
                            "metrics_url": (
                                "/api/repositories/"
                                f"{quote(repository.split('/')[0], safe='')}/"
                                f"{quote(repository.split('/')[1], safe='')}/metrics"
                            ),
                        }
                        for repository, _ in self.server.application.repositories
                    ]
                },
            )
            return

        if (
            len(path_parts) != 5
            or path_parts[:2] != ["api", "repositories"]
            or path_parts[4] != "metrics"
        ):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return

        repository = f"{path_parts[2]}/{path_parts[3]}"
        if repository not in self.server.application.repository_tokens:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Repository not found."})
            return

        query = parse_qs(parsed_url.query)
        months_values = query.get("months", [str(self.server.application.default_months)])
        if len(months_values) != 1:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "'months' must be specified at most once."},
            )
            return

        try:
            months = int(months_values[0])
        except ValueError:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "'months' must be an integer."}
            )
            return
        if not 1 <= months <= MAX_MONTHS:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"'months' must be between 1 and {MAX_MONTHS}."},
            )
            return

        try:
            results, cached = self.server.application.get_metrics(repository, months)
        except github_pr_metrics.GitHubAPIError as error:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": str(error)},
            )
            return
        except KeyError:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": "GitHub returned an unexpected response."},
            )
            return

        self._send_json(
            HTTPStatus.OK,
            {
                "repository": repository,
                "months": months,
                "metrics": results,
                "cached": cached,
            },
        )

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write(f"{self.address_string()} - {format % args}\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Serve configured GitHub pull request metrics as a JSON API."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=github_pr_metrics.DEFAULT_CONFIG,
        help=(
            "path to the JSON configuration file "
            f"(default: {github_pr_metrics.DEFAULT_CONFIG})"
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"interface to bind (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=6,
        help="default number of calendar months to report (default: 6)",
    )
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=DEFAULT_CACHE_TTL,
        help=f"metrics cache lifetime in seconds (default: {DEFAULT_CACHE_TTL})",
    )
    parser.add_argument(
        "--cache-size",
        type=int,
        default=DEFAULT_CACHE_SIZE,
        help=f"maximum number of cached results (default: {DEFAULT_CACHE_SIZE})",
    )
    parser.add_argument(
        "--cors-origin",
        help="optional allowed browser origin, such as http://localhost:5173",
    )
    args = parser.parse_args()

    if not 1 <= args.months <= MAX_MONTHS:
        parser.error(f"--months must be between 1 and {MAX_MONTHS}")
    if args.cache_ttl < 0:
        parser.error("--cache-ttl must be zero or greater")
    if args.cache_size < 1:
        parser.error("--cache-size must be at least 1")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    try:
        application = MetricsApplication(
            args.config,
            args.months,
            args.cache_ttl,
            args.cache_size,
            args.cors_origin,
        )
        with MetricsHTTPServer((args.host, args.port), application) as server:
            print(f"Serving metrics API at http://{args.host}:{server.server_port}")
            print("Press Ctrl+C to stop.")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\nServer stopped.")
    except (OSError, github_pr_metrics.ConfigurationError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
