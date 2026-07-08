import argparse
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from scripts import e2e_gateway_client_matrix as matrix


class GatewayClientMatrixTests(unittest.TestCase):
    def test_runtime_provider_parser_emits_codex_app_cases_without_api_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "providers.toml"
            config.write_text(
                textwrap.dedent(
                    """
                    [[providers]]
                    id = "volc"
                    name = "Volc"
                    base_url = "https://ark.example.test/v1"
                    api_key = "secret-token"
                    upstream_format = "responses"
                    enabled = true

                    [[providers.models]]
                    id = "glm-5.2"
                    enabled = true
                    gateway_exported = true

                    [[providers.models]]
                    id = "disabled-model"
                    enabled = false
                    gateway_exported = true
                    """
                ).strip(),
                encoding="utf-8",
            )

            cases = matrix.parse_runtime_providers_config(config, proxy_base_url="http://127.0.0.1:9099/v1")

        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].client, "codex-app")
        self.assertEqual(cases[0].provider_id, "volc")
        self.assertEqual(cases[0].model_id, "glm-5.2")
        self.assertEqual(cases[0].api, "openai-responses")
        self.assertEqual(cases[0].base_url, "http://127.0.0.1:9099/v1/providers/volc")
        self.assertEqual(cases[0].api_key, "dummy-codexhub-e2e")

    def test_load_cases_can_filter_to_codex_app_runtime_provider_cases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "providers.toml"
            config.write_text(
                textwrap.dedent(
                    """
                    [[providers]]
                    id = "minimax-cn"
                    upstream_format = "chat_completions"
                    enabled = true

                    [[providers.models]]
                    id = "MiniMax-M3"
                    enabled = true
                    gateway_exported = true
                    """
                ).strip(),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                runtime_providers=str(config),
                proxy_base_url="http://127.0.0.1:9099/v1",
                opencode_config=str(Path(temp_dir) / "missing-opencode.json"),
                zcode_config=str(Path(temp_dir) / "missing-zcode.json"),
                pi_models=str(Path(temp_dir) / "missing-pi.json"),
                omp_models=str(Path(temp_dir) / "missing-omp.yml"),
                client=["codex-app"],
                provider=None,
                model=None,
            )

            cases = matrix.load_cases(args)

        self.assertEqual([case.client for case in cases], ["codex-app"])
        self.assertEqual(cases[0].api, "openai-completions")

    def test_load_cases_filters_external_configs_to_runtime_provider_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            providers = temp / "providers.toml"
            providers.write_text(
                textwrap.dedent(
                    """
                    [[providers]]
                    id = "volc"
                    upstream_format = "responses"
                    enabled = true

                    [[providers.models]]
                    id = "glm-5.2"
                    enabled = true
                    gateway_exported = true
                    """
                ).strip(),
                encoding="utf-8",
            )
            pi_models = temp / "models.json"
            pi_models.write_text(
                json.dumps(
                    {
                        "providers": {
                            "codexhub-volc": {
                                "api": "openai-responses",
                                "baseUrl": "http://127.0.0.1:9099/v1/providers/volc",
                                "models": [{"id": "glm-5.2"}],
                            },
                            "codexhub-openai": {
                                "api": "openai-responses",
                                "baseUrl": "http://127.0.0.1:9099/v1/providers/openai",
                                "models": [{"id": "gpt-extra"}],
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                runtime_providers=str(providers),
                proxy_base_url="http://127.0.0.1:9099/v1",
                opencode_config=str(temp / "missing-opencode.json"),
                zcode_config=str(temp / "missing-zcode.json"),
                pi_models=str(pi_models),
                omp_models=str(temp / "missing-omp.yml"),
                client=["codex-app", "pi"],
                provider=None,
                model=None,
                include_extra_config_selectors=False,
            )

            cases = matrix.load_cases(args)

        self.assertEqual([matrix.coverage_selector(case) for case in cases], ["volc/glm-5.2", "volc/glm-5.2"])
        self.assertNotIn("openai/gpt-extra", [matrix.coverage_selector(case) for case in cases])

    def test_report_does_not_include_authorization_secret(self):
        result = matrix.CaseResult(
            client="codex-app",
            provider_id="volc",
            model_id="glm-5.2",
            api="responses",
            endpoint="http://127.0.0.1:9099/v1/providers/volc/responses",
            status="passed",
            duration_ms=12,
            output_preview="CODEXHUB_E2E_OK",
        )
        case = matrix.ClientCase(
            client="codex-app",
            provider_id="volc",
            model_id="glm-5.2",
            display_name="glm-5.2",
            api="responses",
            base_url="http://127.0.0.1:9099/v1/providers/volc",
            api_key="secret-token",
            source_path="providers.toml",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = matrix.write_report(Path(temp_dir), [result], [case])
            data = json.loads(report_path.read_text(encoding="utf-8"))
            text = json.dumps(data, ensure_ascii=False)

        self.assertNotIn("secret-token", text)
        self.assertIn("CODEXHUB_E2E_OK", text)

    def test_manual_results_mark_zcode_cases_for_user_assisted_verification(self):
        case = matrix.ClientCase(
            client="zcode",
            provider_id="codexhub-volc",
            model_id="glm-5.2",
            display_name="glm-5.2",
            api="openai-responses",
            base_url="http://127.0.0.1:9099/v1/providers/volc",
            api_key="secret-token",
            source_path="config.json",
        )

        results = matrix.manual_results_for_cases([case], manual_clients={"zcode"})

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "manual_pending")
        self.assertEqual(results[0].endpoint, "zcode-ui")
        self.assertNotIn("secret-token", json.dumps(dataclasses_asdict(results[0]), ensure_ascii=False))

    def test_coverage_uses_runtime_provider_baseline_and_normalizes_codexhub_prefix(self):
        cases = [
            matrix.ClientCase(
                client="codex-app",
                provider_id="volc",
                model_id="glm-5.2",
                display_name="glm-5.2",
                api="openai-responses",
                base_url="http://127.0.0.1:9099/v1/providers/volc",
                api_key="dummy",
                source_path="providers.toml",
            ),
            matrix.ClientCase(
                client="pi",
                provider_id="codexhub-volc",
                model_id="glm-5.2",
                display_name="glm-5.2",
                api="openai-responses",
                base_url="http://127.0.0.1:9099/v1/providers/volc",
                api_key="dummy",
                source_path="models.json",
            ),
            matrix.ClientCase(
                client="pi",
                provider_id="codexhub-openai",
                model_id="gpt-extra",
                display_name="gpt-extra",
                api="openai-responses",
                base_url="http://127.0.0.1:9099/v1/providers/openai",
                api_key="dummy",
                source_path="models.json",
            ),
        ]

        coverage = matrix.summarize_config_coverage(cases)

        self.assertEqual(coverage["selectors"], ["volc/glm-5.2"])
        self.assertEqual(coverage["missing_by_client"]["codex-app"], [])
        self.assertEqual(coverage["missing_by_client"]["pi"], [])


if __name__ == "__main__":
    unittest.main()


def dataclasses_asdict(value):
    return {
        "client": value.client,
        "provider_id": value.provider_id,
        "model_id": value.model_id,
        "api": value.api,
        "endpoint": value.endpoint,
        "status": value.status,
        "duration_ms": value.duration_ms,
        "error": value.error,
    }
