#!/usr/bin/env python3
"""Report monthly pull request metrics for configured GitHub repositories."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

API_ROOT = "https://api.github.com"
SEARCH_RESULT_LIMIT = 1000
DEFAULT_CONFIG = Path(__file__).with_name("github_pr_metrics.json")


class GitHubAPIError(RuntimeError):
    """Raised when a GitHub API request fails."""


class ConfigurationError(ValueError):
    """Raised when the JSON configuration is invalid."""


def parse_repository_url(value: str) -> str:
    ssh_match = re.fullmatch(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?", value)
    if ssh_match:
        owner, repository = ssh_match.groups()
        return f"{owner}/{repository}"

    parsed = urlparse(value)
    if parsed.scheme not in ("https", "http") or parsed.hostname not in (
        "github.com",
        "www.github.com",
    ):
        raise argparse.ArgumentTypeError(
            "expected a GitHub repository URL, such as https://github.com/owner/repo"
        )

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            "URL must point to a repository, such as https://github.com/owner/repo"
        )

    repository = parts[1]
    if repository.endswith(".git"):
        repository = repository[:-4]
    return f"{parts[0]}/{repository}"


def load_config(config_path: Path) -> tuple[str | None, list[tuple[str, str | None]]]:
    try:
        with config_path.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except OSError as error:
        raise ConfigurationError(
            f"Could not read configuration file '{config_path}': {error}"
        ) from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            f"Invalid JSON in '{config_path}' at line {error.lineno}, "
            f"column {error.colno}: {error.msg}"
        ) from error

    if not isinstance(config, dict):
        raise ConfigurationError("The configuration must be a JSON object.")

    global_token = config.get("github_token")
    if global_token is not None and (
        not isinstance(global_token, str) or not global_token.strip()
    ):
        raise ConfigurationError("'github_token' must be a non-empty string or null.")
    if isinstance(global_token, str):
        global_token = global_token.strip()

    repositories = config.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise ConfigurationError("'repositories' must be a non-empty list.")

    configured_repositories = []
    for index, entry in enumerate(repositories, start=1):
        if not isinstance(entry, dict):
            raise ConfigurationError(f"Repository entry {index} must be an object.")

        url = entry.get("url")
        if not isinstance(url, str):
            raise ConfigurationError(
                f"Repository entry {index} must include a string 'url'."
            )
        try:
            repository = parse_repository_url(url)
        except argparse.ArgumentTypeError as error:
            raise ConfigurationError(
                f"Invalid URL in repository entry {index}: {error}"
            ) from error

        token = entry.get("token")
        if token is not None and (not isinstance(token, str) or not token.strip()):
            raise ConfigurationError(
                f"'token' in repository entry {index} must be a non-empty string or null."
            )
        if isinstance(token, str):
            token = token.strip()
        configured_repositories.append((repository, token))

    return global_token, configured_repositories


def month_windows(today: date, count: int) -> list[tuple[date, date]]:
    year, month = today.year, today.month
    windows = []
    for offset in range(count - 1, -1, -1):
        month_index = year * 12 + month - 1 - offset
        window_year, zero_based_month = divmod(month_index, 12)
        window_month = zero_based_month + 1
        start = date(window_year, window_month, 1)
        if window_month == 12:
            next_month = date(window_year + 1, 1, 1)
        else:
            next_month = date(window_year, window_month + 1, 1)
        end = min(next_month - timedelta(days=1), today)
        windows.append((start, end))
    return windows


class GitHubClient:
    def __init__(self, token: str | None) -> None:
        self.token = token

    def get(self, path: str, params: dict[str, str] | None = None) -> dict:
        url = f"{API_ROOT}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "github-pr-monthly-metrics",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                details = json.loads(error.read()).get("message", str(error))
            except (json.JSONDecodeError, AttributeError):
                details = str(error)
            hint = (
                " Configure 'github_token' in the JSON file to increase the API "
                "rate limit."
                if error.code in (403, 429)
                else ""
            )
            raise GitHubAPIError(
                f"GitHub API request failed ({error.code}): {details}.{hint}"
            ) from error
        except (URLError, TimeoutError) as error:
            raise GitHubAPIError(f"Could not reach the GitHub API: {error}") from error

    def search_pull_requests(
        self, repository: str, field: str, start: date, end: date
    ) -> list[dict]:
        query = (
            f"repo:{repository} is:pr "
            f"{field}:{start.isoformat()}..{end.isoformat()}"
        )
        first_page = self.get(
            "/search/issues", {"q": query, "per_page": "100", "page": "1"}
        )
        total_count = first_page["total_count"]
        if total_count > SEARCH_RESULT_LIMIT:
            raise GitHubAPIError(
                f"The {field} search for {repository} from {start} through {end} "
                f"matched {total_count} PRs. GitHub search only exposes up to "
                f"{SEARCH_RESULT_LIMIT} results per query."
            )

        items = first_page["items"]
        for page in range(2, (total_count + 99) // 100 + 1):
            result = self.get(
                "/search/issues",
                {"q": query, "per_page": "100", "page": str(page)},
            )
            items.extend(result["items"])
        return items

    def first_review_time(self, repository: str, number: int) -> datetime | None:
        reviews = []
        page = 1
        while True:
            result = self.get(
                f"/repos/{repository}/pulls/{number}/reviews",
                {"per_page": "100", "page": str(page)},
            )
            reviews.extend(result)
            if len(result) < 100:
                break
            page += 1

        submitted_times = [
            parse_github_datetime(review["submitted_at"])
            for review in reviews
            if review.get("submitted_at")
        ]
        return min(submitted_times) if submitted_times else None


def parse_github_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_duration(seconds: float) -> str:
    minutes = max(0, round(seconds / 60))
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def collect_metrics(repository: str, client: GitHubClient, months: int) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    metrics = []
    for start, end in month_windows(today, months):
        opened = client.search_pull_requests(repository, "created", start, end)
        closed = client.search_pull_requests(repository, "closed", start, end)
        latencies = []
        no_review = 0

        for pull_request in opened:
            first_review = client.first_review_time(repository, pull_request["number"])
            if first_review is None:
                no_review += 1
            else:
                created = parse_github_datetime(pull_request["created_at"])
                latencies.append((first_review - created).total_seconds())

        metrics.append(
            {
                "month": start.strftime("%Y-%m"),
                "opened": len(opened),
                "closed": len(closed),
                "average": statistics.mean(latencies) if latencies else None,
                "median": statistics.median(latencies) if latencies else None,
                "reviewed": len(latencies),
                "no_review": no_review,
            }
        )
    return metrics


def print_metrics(repository: str, metrics: list[dict]) -> None:
    print(f"Pull request metrics for {repository} (UTC)")
    print(
        f"{'Month':<9} {'Opened':>7} {'Closed':>7} "
        f"{'Avg first review':>17} {'Median':>10} {'Reviewed':>9} {'No review':>10}"
    )
    for metric in metrics:
        average = (
            format_duration(metric["average"])
            if metric["average"] is not None
            else "n/a"
        )
        median = (
            format_duration(metric["median"])
            if metric["median"] is not None
            else "n/a"
        )
        print(
            f"{metric['month']:<9} {metric['opened']:>7} {metric['closed']:>7} "
            f"{average:>17} {median:>10} {metric['reviewed']:>9} "
            f"{metric['no_review']:>10}"
        )
    print(
        "\nFirst-review times are measured from PR creation to its earliest "
        "submitted review and grouped by the PR's opening month."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report monthly pull request metrics for repositories in a JSON "
            "configuration file."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"path to the JSON configuration file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=6,
        help="number of calendar months to report (default: 6)",
    )
    args = parser.parse_args()
    if args.months < 1:
        parser.error("--months must be at least 1")

    try:
        global_token, repositories = load_config(args.config)
        for repository, repository_token in repositories:
            token = repository_token if repository_token is not None else global_token
            metrics = collect_metrics(repository, GitHubClient(token), args.months)
            print_metrics(repository, metrics)
    except (ConfigurationError, GitHubAPIError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
