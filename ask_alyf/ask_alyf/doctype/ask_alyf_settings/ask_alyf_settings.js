// Copyright (c) 2026, ALYF GmbH and contributors
// For license information, please see license.txt

const CHAT_MODEL_CONFIGURATION = "chat";
const VISION_MODEL_CONFIGURATION = "vision";
const VISION_PROVIDER_FIELDS = [
	"vision_llm_provider",
	"vision_base_url",
	"vision_api_key",
	"vision_model",
];

frappe.ui.form.on("Ask ALYF Settings", {
	async refresh(frm) {
		await refresh_model_options(frm);
	},

	async llm_provider(frm) {
		await refresh_chat_model_options(frm);
	},

	async base_url(frm) {
		await refresh_chat_model_options(frm);
	},

	async api_key(frm) {
		await refresh_chat_model_options(frm);
	},

	async vision_model_is_chat_model(frm) {
		toggle_vision_provider_fields(frm);
		await refresh_vision_model_options(frm);
	},

	async vision_llm_provider(frm) {
		await refresh_vision_model_options(frm);
	},

	async vision_base_url(frm) {
		await refresh_vision_model_options(frm);
	},

	async vision_api_key(frm) {
		await refresh_vision_model_options(frm);
	},
});

async function refresh_model_options(frm) {
	toggle_vision_provider_fields(frm);
	await refresh_chat_model_options(frm);
	await refresh_vision_model_options(frm);
}

async function refresh_chat_model_options(frm) {
	clear_model_options(frm, "model");
	await load_model_options(frm, {
		fieldname: "model",
		configuration: CHAT_MODEL_CONFIGURATION,
	});
}

async function refresh_vision_model_options(frm) {
	clear_model_options(frm, "vision_model");

	if (uses_chat_model_for_vision(frm)) {
		return;
	}

	await load_model_options(frm, {
		fieldname: "vision_model",
		configuration: VISION_MODEL_CONFIGURATION,
	});
}

async function load_model_options(frm, { fieldname, configuration }) {
	const model_field = frm.fields_dict[fieldname];
	if (!model_field) {
		return;
	}

	if (!is_model_configuration_ready(frm, configuration)) {
		model_field.set_data([]);
		return;
	}

	try {
		const response = await frappe.call({
			method: "ask_alyf.ask_alyf.doctype.ask_alyf_settings.ask_alyf_settings.get_available_models",
			args: { configuration },
		});

		const models = response.message || [];
		const options = models.map((model) => ({
			label: model.id,
			value: model.id,
		}));
		model_field.set_data(options);
	} catch {
		model_field.set_data([]);
	}
}

function clear_model_options(frm, fieldname) {
	frm.fields_dict[fieldname]?.set_data([]);
}

function toggle_vision_provider_fields(frm) {
	frm.toggle_display(VISION_PROVIDER_FIELDS, !uses_chat_model_for_vision(frm));
}

function uses_chat_model_for_vision(frm) {
	return Boolean(frm.doc.vision_model_is_chat_model);
}

function is_model_configuration_ready(frm, configuration) {
	const field_prefix = configuration === VISION_MODEL_CONFIGURATION ? "vision_" : "";
	const llm_provider = (frm.doc[`${field_prefix}llm_provider`] || "").trim();
	const api_key = (frm.doc[`${field_prefix}api_key`] || "").trim();

	return Boolean(llm_provider && api_key);
}
