import frappe

OLD_PROVIDERS = {"OpenAI", "OpenAI Compatible"}


def execute():
	"""Store LangChain `init_chat_model` provider keys instead of display labels."""
	for fieldname in ("llm_provider", "vision_llm_provider"):
		if frappe.db.get_single_value("Ask ALYF Settings", fieldname) in OLD_PROVIDERS:
			frappe.db.set_single_value("Ask ALYF Settings", fieldname, "openai")
