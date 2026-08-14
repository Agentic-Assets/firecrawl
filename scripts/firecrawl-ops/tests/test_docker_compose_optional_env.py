"""Static guard for intentionally optional self-hosted Compose variables."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "docker-compose.yaml"

OPTIONAL_ENV = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "MODEL_NAME",
    "MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK",
    "MODEL_EMBEDDING_NAME",
    "OLLAMA_BASE_URL",
    "AUTUMN_SECRET_KEY",
    "SLACK_WEBHOOK_URL",
    "BULL_AUTH_KEY",
    "TEST_API_KEY",
    "SUPABASE_ANON_TOKEN",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_TOKEN",
    "SUPABASE_REPLICA_URL",
    "SELF_HOSTED_WEBHOOK_URL",
    "LOGGING_LEVEL",
    "PROXY_SERVER",
    "PROXY_USERNAME",
    "PROXY_PASSWORD",
    "SEARXNG_ENDPOINT",
    "SEARXNG_ENGINES",
    "SEARXNG_CATEGORIES",
    "NUQ_BACKEND",
)


class DockerComposeOptionalEnvTests(unittest.TestCase):
    def test_optional_local_integrations_have_empty_defaults(self) -> None:
        text = COMPOSE.read_text(encoding="utf-8")
        for name in OPTIONAL_ENV:
            self.assertIn(
                f"${{{name}:-}}",
                text,
                f"{name} should stay optional for the core local stack",
            )


if __name__ == "__main__":
    unittest.main()
