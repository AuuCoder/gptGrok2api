from __future__ import annotations

import unittest
from unittest.mock import patch

from services import model_catalog_service
from services.protocol import openai_v1_models


class ModelCatalogServiceTest(unittest.TestCase):
    def test_fallback_catalog_exposes_gpt_5_6(self) -> None:
        with (
            patch.object(model_catalog_service.config, "get", return_value={}),
            patch.object(model_catalog_service.account_service, "list_accounts", return_value=[]),
        ):
            catalog = model_catalog_service.get_model_catalog()

        self.assertIn("gpt-5-6", catalog["chat_models"])
        self.assertIn("gpt-5-6", catalog["all_models"])
        self.assertIn("gpt-image-2", catalog["image_edit_models"])
        self.assertTrue(
            {"gpt-5-6-sol", "gpt-5-6-terra", "gpt-5-6-luna"}.issubset(catalog["chat_models"])
        )

    def test_openai_models_output_includes_gpt_5_6_from_catalog(self) -> None:
        catalog = {
            "models": [],
            "chat_models": ["gpt-5-6"],
            "image_models": [],
        }
        with (
            patch.object(openai_v1_models, "get_model_catalog", return_value=catalog),
            patch.object(openai_v1_models, "_append_upstream_models"),
            patch.object(openai_v1_models, "_dynamic_image_models", return_value=[]),
        ):
            payload = openai_v1_models.list_models()

        self.assertEqual([item["id"] for item in payload["data"]], ["gpt-5-6"])

    def test_catalog_only_advertises_verified_grok_cli_oauth_models(self) -> None:
        from services.xai_cli_oauth_service import xai_cli_oauth_service

        with (
            patch.object(model_catalog_service.config, "get", return_value={}),
            patch.object(model_catalog_service.account_service, "list_accounts", return_value=[]),
            patch.object(
                xai_cli_oauth_service,
                "model_items",
                return_value=[
                    {
                        "id": "grok-4.5",
                        "object": "model",
                        "owned_by": "xai",
                        "provider": "grok",
                        "capabilities": ["chat"],
                    }
                ],
            ),
        ):
            catalog = model_catalog_service.get_model_catalog()

        self.assertIn("grok-4.5", catalog["chat_models"])
        self.assertEqual(catalog["models"][-1]["id"], "grok-4.5")


if __name__ == "__main__":
    unittest.main()
