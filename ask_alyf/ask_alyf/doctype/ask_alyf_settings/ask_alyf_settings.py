import json
from enum import StrEnum
from typing import TYPE_CHECKING

import frappe
from any_llm import AnyLLM
from frappe import _
from frappe.model.document import Document
from langchain_openai import ChatOpenAI
from litellm.utils import get_model_info, supports_function_calling, supports_vision

if TYPE_CHECKING:
	from frappe.core.doctype.has_role.has_role import HasRole
	from frappe.types import DF

	from ask_alyf.ask_alyf.doctype.ask_alyf_excluded_doctype.ask_alyf_excluded_doctype import (
		AskALYFExcludedDocType,
	)


class ModelConfiguration(StrEnum):
	CHAT = "chat"
	VISION = "vision"
	ASK = "ask"


MODEL_CONFIG_FIELDS = {
	ModelConfiguration.CHAT: {
		"provider_field": "llm_provider",
		"base_url_field": "base_url",
		"api_key_field": "api_key",
	},
	ModelConfiguration.VISION: {
		"provider_field": "vision_llm_provider",
		"base_url_field": "vision_base_url",
		"api_key_field": "vision_api_key",
	},
	ModelConfiguration.ASK: {
		"provider_field": "ask_llm_provider",
		"base_url_field": "ask_base_url",
		"api_key_field": "ask_api_key",
	},
}

LITELLM_PROVIDER = "openai"

# `init_chat_model` provider keys whose any-llm name differs (any-llm lists models).
ANY_LLM_PROVIDER_NAMES = {
	"google_genai": "gemini",
	"mistralai": "mistral",
}

NON_TEXT_MODEL_PATTERNS = (
	"audio",
	"dall",
	"embed",
	"image",
	"moderation",
	"omni-moderation",
	"realtime",
	"search",
	"similarity",
	"speech",
	"transcribe",
	"tts",
	"vision-preview",
	"whisper",
)


class AskALYFSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from ask_alyf.ask_alyf.doctype.ask_alyf_excluded_doctype.ask_alyf_excluded_doctype import (
			AskALYFExcludedDocType,
		)

		allow_agent_mode: DF.Check
		allow_code_search: DF.Check
		allow_conversation_deletion: DF.Check
		allow_field_agent: DF.Check
		allow_file_upload: DF.Check
		api_key: DF.Password | None
		ask_api_key: DF.Password | None
		ask_base_url: DF.Data | None
		ask_llm_provider: DF.Autocomplete | None
		ask_model: DF.Autocomplete | None
		ask_model_is_chat_model: DF.Check
		ask_reasoning_effort: DF.Literal["", "low", "medium", "high", "xhigh", "max"]
		base_url: DF.Data | None
		enable_tracing: DF.Check
		enabled: DF.Check
		excluded_doctypes: DF.TableMultiSelect[AskALYFExcludedDocType]
		langsmith_api_key: DF.Password | None
		langsmith_endpoint: DF.Data | None
		langsmith_project: DF.Data | None
		llm_provider: DF.Autocomplete
		model: DF.Autocomplete | None
		reasoning_effort: DF.Literal["", "low", "medium", "high", "xhigh", "max"]
		support_phone_number: DF.Phone | None
		system_prompt: DF.Code | None
		vision_api_key: DF.Password | None
		vision_base_url: DF.Data | None
		vision_llm_provider: DF.Autocomplete | None
		vision_model: DF.Autocomplete | None
		vision_model_is_chat_model: DF.Check
	# end: auto-generated types

	def is_code_search_enabled(self) -> bool:
		return bool(self.allow_code_search)

	def validate(self):
		self.validate_llm_providers()
		validate_model_selection(
			self.model,
			ModelConfiguration.CHAT,
			unsupported_message=_("The selected Chat Model ({0}) does not support function calling."),
		)
		# Only LangChain's OpenAI model data lists the supported reasoning levels.
		if self.llm_provider == "openai":
			warn_if_unsupported_reasoning_effort(self.model, self.get("reasoning_effort"))

		self.validate_ask_model()

		if self.vision_model_is_chat_model:
			validate_model_selection(
				self.model,
				ModelConfiguration.VISION,
				unsupported_message=_(
					"The selected Chat Model ({0}) does not support vision. Choose a vision-capable chat model or configure a separate vision model."
				),
			)
			return

		validate_model_selection(
			self.vision_model,
			ModelConfiguration.VISION,
			unsupported_message=_("The selected Vision Model ({0}) does not support vision."),
		)

	def validate_ask_model(self):
		if self.ask_model_is_chat_model:
			return

		validate_model_selection(
			self.ask_model,
			ModelConfiguration.ASK,
			unsupported_message=_("The selected Ask Mode Model ({0}) does not support function calling."),
		)
		reasoning_effort = (self.get("ask_reasoning_effort") or "").strip()
		if self.ask_llm_provider != "openai" or not self.ask_model or not reasoning_effort:
			return

		levels = get_langchain_reasoning_effort_levels(self.ask_model)
		if levels is not None and reasoning_effort not in levels:
			frappe.msgprint(
				_("The selected Ask Mode Model ({0}) does not support Reasoning Effort ({1}).").format(
					self.ask_model, reasoning_effort
				),
				indicator="orange",
				alert=True,
			)

	def validate_llm_providers(self):
		"""Autocomplete accepts free text, so check the value is a known provider."""
		for fieldname in ("llm_provider", "vision_llm_provider", "ask_llm_provider"):
			provider = self.get(fieldname)
			if not provider:
				continue

			options = json.loads(self.meta.get_field(fieldname).options)
			if provider not in {option["value"] for option in options}:
				frappe.throw(_("Unsupported {0}: {1}").format(_(self.meta.get_label(fieldname)), provider))


@frappe.whitelist()
def get_available_models(configuration: str = ModelConfiguration.CHAT) -> list[dict[str, str]]:
	configuration = parse_model_configuration(configuration)
	settings = frappe.get_single("Ask ALYF Settings")
	settings.check_permission("write")

	model_fields = get_model_config_fields(configuration)
	llm_provider = (getattr(settings, model_fields["provider_field"], "") or "").strip()
	base_url = (getattr(settings, model_fields["base_url_field"], "") or "").strip() or None
	api_key = normalize_api_key(settings.get_password(model_fields["api_key_field"], raise_exception=False))

	if not llm_provider:
		return []

	if not api_key:
		frappe.msgprint(
			_("Please configure an API key first and save the settings, then we can fetch available models."),
			alert=True,
		)
		return []

	client = AnyLLM.create(
		provider=ANY_LLM_PROVIDER_NAMES.get(llm_provider, llm_provider),
		api_key=api_key,
		api_base=base_url,
	)
	response = client.list_models()
	models = sorted(
		[model for model in response if is_available_model(model.id, configuration)],
		key=lambda model: model.id.lower(),
	)

	return [{"id": model.id} for model in models]


def parse_model_configuration(
	configuration: str | ModelConfiguration | None = None,
) -> ModelConfiguration:
	if isinstance(configuration, ModelConfiguration):
		return configuration

	try:
		return ModelConfiguration((configuration or ModelConfiguration.CHAT).strip().lower())
	except ValueError:
		frappe.throw(_("Unsupported model configuration: {0}").format(configuration))


def get_model_config_fields(configuration: str | ModelConfiguration) -> dict[str, str]:
	return MODEL_CONFIG_FIELDS[parse_model_configuration(configuration)]


def normalize_api_key(api_key: str | None) -> str:
	api_key = (api_key or "").strip()
	if not api_key:
		return ""

	# Password fields may send a masked placeholder when the document is already saved.
	if set(api_key) == {"*"}:
		return ""

	return api_key


def validate_model_selection(
	model_id: str | None,
	configuration: ModelConfiguration,
	*,
	unsupported_message: str,
) -> None:
	model_id = (model_id or "").strip()
	if not model_id:
		return

	if is_available_model(model_id, configuration):
		return

	frappe.throw(unsupported_message.format(model_id))


def get_langchain_reasoning_effort_levels(model_id: str) -> list[str] | None:
	"""Return LangChain's reasoning effort levels for a model, or None if unknown."""
	model_id = (model_id or "").strip()
	if not model_id:
		return None

	profile = ChatOpenAI(model=model_id, api_key="unused").profile
	if profile is None:
		return None

	return list(profile.get("reasoning_effort_levels") or ())


def warn_if_unsupported_reasoning_effort(model_id: str | None, reasoning_effort: str | None) -> None:
	model_id = (model_id or "").strip()
	reasoning_effort = (reasoning_effort or "").strip()
	if not model_id or not reasoning_effort:
		return

	levels = get_langchain_reasoning_effort_levels(model_id)
	if levels is None or reasoning_effort in levels:
		return

	if levels:
		message = _(
			"The selected Chat Model ({0}) does not support Reasoning Effort ({1}). Supported values: {2}."
		).format(model_id, reasoning_effort, ", ".join(levels))
	else:
		message = _("The selected Chat Model ({0}) does not support Reasoning Effort ({1}).").format(
			model_id, reasoning_effort
		)

	frappe.msgprint(message, indicator="orange", alert=True)


def is_text_generation_model(model_id: str) -> bool:
	model_id = (model_id or "").strip().lower()
	if not model_id:
		return False

	return not any(pattern in model_id for pattern in NON_TEXT_MODEL_PATTERNS)


def is_litellm_mapped_model(model_id: str) -> bool:
	try:
		get_model_info(model_id, custom_llm_provider=LITELLM_PROVIDER)
	except Exception:
		return False

	return True


def has_required_capability(model_id: str, configuration: ModelConfiguration) -> bool:
	match configuration:
		case ModelConfiguration.CHAT | ModelConfiguration.ASK:
			supported = supports_function_calling(model_id, custom_llm_provider=LITELLM_PROVIDER)
		case ModelConfiguration.VISION:
			supported = supports_vision(model_id, custom_llm_provider=LITELLM_PROVIDER)

	if supported:
		return True

	# LiteLLM returns False for unmapped custom models; keep those selectable.
	return not is_litellm_mapped_model(model_id)


def is_available_model(model_id: str, configuration: str | ModelConfiguration) -> bool:
	if not is_text_generation_model(model_id):
		return False

	return has_required_capability(model_id, parse_model_configuration(configuration))
