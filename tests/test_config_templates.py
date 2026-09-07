import json
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUBSCRIPTION_ID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)
AZURE_OPENAI_HOST_PATTERN = re.compile(
    r'^https://([^.]+)\.openai\.azure\.com/?$', re.IGNORECASE
)
PLACEHOLDER_MARKERS = ("<", "your-", "example", "placeholder")


class AzureConfigTemplateSafetyTests(unittest.TestCase):
    def test_litellm_template_uses_gateway_neutral_resource_group(self):
        config = json.loads((ROOT / "LiteLLM/azure-openai.json").read_text(encoding="utf-8"))
        self.assertIn("resource_group", config)
        self.assertNotIn("apim_resource_group", config)
        self.assertNotIn("apim_name", config)

    def test_tracked_templates_do_not_contain_real_subscription_ids_or_endpoints(self):
        for relative_path in (
            Path("LiteLLM/azure-openai.json"),
        ):
            config = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
            for resource in config.get("azure-openai-list", []):
                subscription_id = resource.get("subscription_id", "")
                endpoint = resource.get("endpoint", "")
                self.assertNotRegex(
                    subscription_id,
                    SUBSCRIPTION_ID_PATTERN,
                    f"{relative_path} must contain a placeholder subscription ID",
                )
                endpoint_match = AZURE_OPENAI_HOST_PATTERN.match(endpoint)
                if endpoint_match:
                    host_name = endpoint_match.group(1).lower()
                    self.assertTrue(
                        any(marker in host_name for marker in PLACEHOLDER_MARKERS),
                        f"{relative_path} must contain a clearly marked placeholder endpoint",
                    )

    def test_local_litellm_config_is_ignored_and_untracked(self):
        relative_path = "LiteLLM/azure-openai.loc.json"
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", relative_path],
            cwd=ROOT,
            check=False,
        )
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", relative_path],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        self.assertNotEqual(tracked.returncode, 0)


if __name__ == "__main__":
    unittest.main()
