import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import ANY, patch

import github_pr_metrics
import github_pr_metrics_server


class MetricsServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config_directory = TemporaryDirectory()
        config_path = Path(self.config_directory.name) / "config.json"
        with config_path.open("w", encoding="utf-8") as config_file:
            json.dump(
                {
                    "github_token": "global-token",
                    "repositories": [
                        {"url": "https://github.com/acme/one"},
                        {
                            "url": "https://github.com/acme/two",
                            "token": "repository-token",
                        },
                    ],
                },
                config_file,
            )

        application = github_pr_metrics_server.MetricsApplication(
            config_path,
            default_months=2,
            cache_ttl=300,
            cache_size=8,
            cors_origin="http://localhost:5173",
        )
        self.server = github_pr_metrics_server.MetricsHTTPServer(
            ("127.0.0.1", 0), application
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        self.config_directory.cleanup()

    def test_lists_repositories_without_exposing_tokens(self) -> None:
        with urlopen(f"{self.base_url}/api/repositories") as response:
            result = json.load(response)

        self.assertEqual(
            [repository["name"] for repository in result["repositories"]],
            ["acme/one", "acme/two"],
        )
        self.assertEqual(
            result["repositories"][0]["metrics_url"],
            "/api/repositories/acme/one/metrics",
        )
        self.assertNotIn("global-token", json.dumps(result))
        self.assertNotIn("repository-token", json.dumps(result))

    def test_metrics_are_json_and_cached(self) -> None:
        metrics = [{"month": "2026-10", "opened": 3, "closed": 2}]
        with patch.object(
            github_pr_metrics, "collect_metrics", return_value=metrics
        ) as collect_metrics:
            with urlopen(
                f"{self.base_url}/api/repositories/acme/one/metrics"
            ) as response:
                first_result = json.load(response)
            with urlopen(
                f"{self.base_url}/api/repositories/acme/one/metrics"
            ) as response:
                second_result = json.load(response)

        self.assertEqual(first_result["metrics"], metrics)
        self.assertEqual(first_result["months"], 2)
        self.assertFalse(first_result["cached"])
        self.assertTrue(second_result["cached"])
        collect_metrics.assert_called_once_with("acme/one", ANY, 2)
        self.assertEqual(
            self.server.application.repository_tokens["acme/two"],
            "repository-token",
        )

    def test_metrics_use_repository_specific_token(self) -> None:
        token_by_repository = {}

        def collect(repository, client, months):
            self.assertEqual(months, 2)
            token_by_repository[repository] = client.token
            return []

        with patch.object(
            github_pr_metrics, "collect_metrics", side_effect=collect
        ):
            with urlopen(
                f"{self.base_url}/api/repositories/acme/two/metrics"
            ) as response:
                self.assertEqual(response.status, 200)

        self.assertEqual(token_by_repository["acme/two"], "repository-token")

    def test_unknown_repository_and_invalid_months_return_json_errors(self) -> None:
        for path, expected_status in (
            ("/api/repositories/acme/unknown/metrics", 404),
            ("/api/repositories/acme/one/metrics?months=0", 400),
        ):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as error:
                    urlopen(f"{self.base_url}{path}")
                error_response = error.exception
                try:
                    self.assertEqual(error_response.code, expected_status)
                    self.assertIn("error", json.loads(error_response.read()))
                finally:
                    error_response.close()

    def test_preflight_allows_the_configured_origin(self) -> None:
        request = Request(
            f"{self.base_url}/api/repositories",
            headers={"Origin": "http://localhost:5173"},
            method="OPTIONS",
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(
                response.headers["Access-Control-Allow-Origin"],
                "http://localhost:5173",
            )


if __name__ == "__main__":
    unittest.main()
