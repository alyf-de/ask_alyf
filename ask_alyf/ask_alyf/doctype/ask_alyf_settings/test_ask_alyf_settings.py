# Copyright (c) 2026, ALYF GmbH and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from ask_alyf.ask_alyf.doctype.ask_alyf_settings import ask_alyf_settings

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class FakeSettings(SimpleNamespace):
	def __init__(self, *, passwords: dict[str, str] | None = None, **kwargs):
		super().__init__(**kwargs)
		self.passwords = passwords or {}
		self.checked_permissions = []

	def check_permission(self, permission_type: str):
		self.checked_permissions.append(permission_type)

	def get_password(self, fieldname: str, raise_exception: bool = False):
		return self.passwords.get(fieldname)


class UnitTestAskALYFSettings(UnitTestCase):
	def test_get_available_models_uses_chat_configuration_by_default(self):
		settings = FakeSettings(
			llm_provider="OpenAI",
			base_url=None,
			passwords={"api_key": "chat-key"},
		)
		client = SimpleNamespace(
			list_models=lambda: [
				SimpleNamespace(id="gpt-4o"),
				SimpleNamespace(id="whisper-1"),
				SimpleNamespace(id="gpt-4.1"),
			]
		)

		with (
			patch.object(ask_alyf_settings.frappe, "get_single", return_value=settings),
			patch.object(ask_alyf_settings.AnyLLM, "create", return_value=client) as create_client,
		):
			models = ask_alyf_settings.get_available_models()

		self.assertEqual(settings.checked_permissions, ["write"])
		create_client.assert_called_once_with(provider="openai", api_key="chat-key", api_base=None)
		self.assertEqual(models, [{"id": "gpt-4.1"}, {"id": "gpt-4o"}])

	def test_get_available_models_uses_vision_configuration_when_requested(self):
		settings = FakeSettings(
			vision_llm_provider="OpenAI Compatible",
			vision_base_url="https://example.test/v1",
			passwords={"vision_api_key": "vision-key"},
		)
		client = SimpleNamespace(
			list_models=lambda: [
				SimpleNamespace(id="gpt-4.1-mini"),
				SimpleNamespace(id="omni-moderation-latest"),
			]
		)

		with (
			patch.object(ask_alyf_settings.frappe, "get_single", return_value=settings),
			patch.object(ask_alyf_settings.AnyLLM, "create", return_value=client) as create_client,
		):
			models = ask_alyf_settings.get_available_models(configuration="vision")

		self.assertEqual(settings.checked_permissions, ["write"])
		create_client.assert_called_once_with(
			provider="openai",
			api_key="vision-key",
			api_base="https://example.test/v1",
		)
		self.assertEqual(models, [{"id": "gpt-4.1-mini"}])

	def test_get_available_models_returns_empty_when_configuration_is_blank(self):
		settings = FakeSettings(
			vision_llm_provider="",
			vision_base_url="",
			passwords={"vision_api_key": "vision-key"},
		)

		with (
			patch.object(ask_alyf_settings.frappe, "get_single", return_value=settings),
			patch.object(ask_alyf_settings.AnyLLM, "create") as create_client,
		):
			models = ask_alyf_settings.get_available_models(configuration="vision")

		self.assertEqual(settings.checked_permissions, ["write"])
		create_client.assert_not_called()
		self.assertEqual(models, [])

	def test_get_model_config_fields_rejects_unknown_configuration(self):
		with self.assertRaises(frappe.ValidationError):
			ask_alyf_settings.get_model_config_fields("audio")

	def test_is_available_model_filters_known_models_without_required_capability(self):
		with patch.object(ask_alyf_settings, "supports_function_calling", return_value=False):
			with patch.object(ask_alyf_settings, "is_litellm_mapped_model", return_value=True):
				self.assertFalse(
					ask_alyf_settings.is_available_model(
						"gpt-5-chat", ask_alyf_settings.ModelConfiguration.CHAT
					)
				)

	def test_is_available_model_keeps_unknown_models_for_chat_configuration(self):
		with patch.object(ask_alyf_settings, "supports_function_calling", return_value=False):
			with patch.object(ask_alyf_settings, "is_litellm_mapped_model", return_value=False):
				self.assertTrue(
					ask_alyf_settings.is_available_model(
						"custom-local-model", ask_alyf_settings.ModelConfiguration.CHAT
					)
				)

	def test_is_available_model_requires_vision_capability_for_vision_configuration(self):
		with patch.object(ask_alyf_settings, "supports_vision", return_value=False):
			with patch.object(ask_alyf_settings, "is_litellm_mapped_model", return_value=True):
				self.assertFalse(
					ask_alyf_settings.is_available_model(
						"gpt-4.1-mini", ask_alyf_settings.ModelConfiguration.VISION
					)
				)

	def test_is_available_model_keeps_unknown_models_for_vision_configuration(self):
		with patch.object(ask_alyf_settings, "supports_vision", return_value=False):
			with patch.object(ask_alyf_settings, "is_litellm_mapped_model", return_value=False):
				self.assertTrue(
					ask_alyf_settings.is_available_model(
						"custom-vision-model", ask_alyf_settings.ModelConfiguration.VISION
					)
				)

	def test_parse_model_configuration_accepts_string_values(self):
		self.assertEqual(
			ask_alyf_settings.parse_model_configuration("vision"),
			ask_alyf_settings.ModelConfiguration.VISION,
		)


class IntegrationTestAskALYFSettings(IntegrationTestCase):
	"""
	Integration tests for AskALYFSettings.
	Use this class for testing interactions between multiple components.
	"""

	pass
