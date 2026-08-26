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

	def test_validate_rejects_known_chat_model_without_function_calling(self):
		settings = ask_alyf_settings.AskALYFSettings(
			{
				"doctype": "Ask ALYF Settings",
				"model": "gpt-5-chat",
			}
		)

		with patch.object(ask_alyf_settings, "is_available_model", return_value=False):
			with self.assertRaises(frappe.ValidationError):
				settings.validate()

	def test_validate_rejects_known_chat_model_without_vision_when_shared(self):
		settings = ask_alyf_settings.AskALYFSettings(
			{
				"doctype": "Ask ALYF Settings",
				"vision_model_is_chat_model": 1,
				"model": "gpt-audio",
			}
		)

		with patch.object(
			ask_alyf_settings,
			"is_available_model",
			side_effect=lambda model_id, configuration: (
				configuration == ask_alyf_settings.ModelConfiguration.CHAT
			),
		):
			with self.assertRaises(frappe.ValidationError):
				settings.validate()

	def test_validate_allows_unknown_chat_model_when_shared(self):
		settings = ask_alyf_settings.AskALYFSettings(
			{
				"doctype": "Ask ALYF Settings",
				"vision_model_is_chat_model": 1,
				"model": "custom-local-model",
			}
		)

		with patch.object(ask_alyf_settings, "is_available_model", return_value=True):
			settings.validate()

	def test_validate_rejects_known_vision_model_without_vision_support(self):
		settings = ask_alyf_settings.AskALYFSettings(
			{
				"doctype": "Ask ALYF Settings",
				"vision_model_is_chat_model": 0,
				"model": "gpt-4o",
				"vision_model": "gpt-audio",
			}
		)

		with patch.object(
			ask_alyf_settings,
			"is_available_model",
			side_effect=lambda model_id, configuration: (
				configuration == ask_alyf_settings.ModelConfiguration.CHAT
			),
		):
			with self.assertRaises(frappe.ValidationError):
				settings.validate()

	def test_validate_skips_vision_model_check_when_separate_vision_model_is_not_set(self):
		settings = ask_alyf_settings.AskALYFSettings(
			{
				"doctype": "Ask ALYF Settings",
				"vision_model_is_chat_model": 0,
				"model": "gpt-4o",
			}
		)

		with patch.object(ask_alyf_settings, "is_available_model", return_value=True) as availability_check:
			settings.validate()

		availability_check.assert_called_once_with(
			"gpt-4o",
			ask_alyf_settings.ModelConfiguration.CHAT,
		)

	def test_get_langchain_reasoning_effort_levels_returns_none_for_unknown_model(self):
		with patch.object(ask_alyf_settings, "ChatOpenAI", return_value=SimpleNamespace(profile=None)):
			self.assertIsNone(ask_alyf_settings.get_langchain_reasoning_effort_levels("custom-local-model"))

	def test_get_langchain_reasoning_effort_levels_returns_empty_when_profile_has_no_levels(self):
		with patch.object(
			ask_alyf_settings, "ChatOpenAI", return_value=SimpleNamespace(profile={"name": "GPT-4o"})
		):
			self.assertEqual(ask_alyf_settings.get_langchain_reasoning_effort_levels("gpt-4o"), [])

	def test_warn_if_unsupported_reasoning_effort_skips_unknown_models(self):
		with (
			patch.object(ask_alyf_settings, "get_langchain_reasoning_effort_levels", return_value=None),
			patch.object(ask_alyf_settings.frappe, "msgprint") as msgprint,
		):
			ask_alyf_settings.warn_if_unsupported_reasoning_effort("custom-local-model", "high")

		msgprint.assert_not_called()

	def test_warn_if_unsupported_reasoning_effort_skips_supported_values(self):
		with (
			patch.object(
				ask_alyf_settings,
				"get_langchain_reasoning_effort_levels",
				return_value=["low", "medium", "high"],
			),
			patch.object(ask_alyf_settings.frappe, "msgprint") as msgprint,
		):
			ask_alyf_settings.warn_if_unsupported_reasoning_effort("gpt-5", "high")

		msgprint.assert_not_called()

	def test_warn_if_unsupported_reasoning_effort_warns_when_value_is_not_supported(self):
		with (
			patch.object(
				ask_alyf_settings,
				"get_langchain_reasoning_effort_levels",
				return_value=["low", "medium", "high"],
			),
			patch.object(ask_alyf_settings.frappe, "msgprint") as msgprint,
		):
			ask_alyf_settings.warn_if_unsupported_reasoning_effort("gpt-5", "max")

		msgprint.assert_called_once()
		message, kwargs = msgprint.call_args.args[0], msgprint.call_args.kwargs
		self.assertIn("gpt-5", message)
		self.assertIn("max", message)
		self.assertIn("low, medium, high", message)
		self.assertEqual(kwargs["indicator"], "orange")

	def test_warn_if_unsupported_reasoning_effort_warns_when_model_has_no_levels(self):
		with (
			patch.object(ask_alyf_settings, "get_langchain_reasoning_effort_levels", return_value=[]),
			patch.object(ask_alyf_settings.frappe, "msgprint") as msgprint,
		):
			ask_alyf_settings.warn_if_unsupported_reasoning_effort("gpt-4o", "high")

		msgprint.assert_called_once()
		self.assertIn("gpt-4o", msgprint.call_args.args[0])
		self.assertIn("high", msgprint.call_args.args[0])

	def test_validate_warns_for_unsupported_reasoning_effort(self):
		settings = ask_alyf_settings.AskALYFSettings(
			{
				"doctype": "Ask ALYF Settings",
				"model": "gpt-5",
				"reasoning_effort": "max",
			}
		)

		with (
			patch.object(ask_alyf_settings, "is_available_model", return_value=True),
			patch.object(ask_alyf_settings, "warn_if_unsupported_reasoning_effort") as warn,
		):
			settings.validate()

		warn.assert_called_once_with("gpt-5", "max")


class IntegrationTestAskALYFSettings(IntegrationTestCase):
	"""
	Integration tests for AskALYFSettings.
	Use this class for testing interactions between multiple components.
	"""

	pass
