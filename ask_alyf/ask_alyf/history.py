from typing import Any
from urllib.parse import quote

import frappe
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage


def _build_document_extraction_history_entry(
	extraction: dict[str, Any],
	*,
	extraction_prompt: str = "",
) -> dict[str, Any]:
	"""Normalize extraction metadata stored on assistant messages."""
	return {
		"file_id": extraction.get("file_id") or extraction.get("name"),
		"file_name": extraction.get("file_name"),
		"pages_processed": extraction.get("pages_processed"),
		"total_pages": extraction.get("total_pages"),
		"truncated": bool(extraction.get("truncated")),
		"warning": extraction.get("warning"),
		"extraction_prompt": extraction_prompt,
		"extracted_data": extraction.get("extracted_data"),
	}


def _build_attachment_metadata_lines(files: Any) -> list[str]:
	"""Render attachment metadata into compact prompt lines."""
	if not isinstance(files, list):
		return []

	lines: list[str] = []
	for file_entry in files:
		if not isinstance(file_entry, dict):
			continue

		parts = []
		file_id = (file_entry.get("name") or file_entry.get("file_id") or "").strip()
		file_name = (file_entry.get("file_name") or "").strip()
		if file_id:
			parts.append(f"id={file_id}")
		if file_name:
			parts.append(f"name={file_name}")
		if parts:
			lines.append(f"Attachment metadata: {', '.join(parts)}")

	return lines


def _build_document_extraction_lines(document_extractions: Any) -> list[str]:
	"""Render persisted document extraction metadata and JSON into prompt lines."""
	if not isinstance(document_extractions, list):
		return []

	lines: list[str] = []
	for extraction in document_extractions:
		if not isinstance(extraction, dict):
			continue

		summary = _build_document_extraction_summary(extraction)
		if summary:
			lines.append(summary)

		extraction_prompt = (extraction.get("extraction_prompt") or "").strip()
		if extraction_prompt:
			lines.append(f"Extraction request: {extraction_prompt}")

		warning = (extraction.get("warning") or "").strip()
		if warning:
			lines.append(f"Extraction warning: {warning}")

		extracted_data_text = _stringify_extracted_data(extraction.get("extracted_data"))
		if extracted_data_text:
			lines.append("Extracted document data (JSON):")
			lines.append(extracted_data_text)

	return lines


def _build_document_extraction_summary(extraction: dict[str, Any]) -> str:
	"""Summarize a stored extraction record for prompt reuse."""
	parts = []
	file_id = (extraction.get("file_id") or extraction.get("name") or "").strip()
	file_name = (extraction.get("file_name") or "").strip()
	pages_processed = extraction.get("pages_processed")
	total_pages = extraction.get("total_pages")
	if file_id:
		parts.append(f"id={file_id}")
	if file_name:
		parts.append(f"name={file_name}")
	if isinstance(pages_processed, int):
		parts.append(f"pages_processed={pages_processed}")
	if isinstance(total_pages, int):
		parts.append(f"total_pages={total_pages}")
	return f"Stored document extraction: {', '.join(parts)}" if parts else ""


def _build_file_markdown_link(file_doc) -> str:
	"""Build a Markdown link for a File document label."""
	file_label = _escape_markdown_link_label(
		(file_doc.file_name or file_doc.name or "").strip() or file_doc.name
	)
	file_url = _quote_markdown_link_destination(file_doc.file_url)
	if not file_url:
		return file_label
	return f"[{file_label}]({file_url})"


def _escape_markdown_link_label(label: str) -> str:
	"""Escape characters that would break a Markdown link label."""
	return label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _quote_markdown_link_destination(url: str | None) -> str:
	"""Quote a URL so it is safe inside Markdown link syntax."""
	clean_url = (url or "").strip()
	if not clean_url:
		return ""
	return quote(clean_url, safe="/#:?&=%")


def _stringify_extracted_data(extracted_data: Any) -> str:
	"""Render stored extraction data as JSON text for the prompt."""
	if isinstance(extracted_data, str):
		return extracted_data.strip()
	if extracted_data is None:
		return ""
	return frappe.as_json(extracted_data, indent=2)


def history_item_to_native_message(item: dict[str, Any]) -> AnyMessage | None:
	"""Convert one stored conversation history item into a native LangChain message.

	Attachment and extraction metadata are appended to the message content so the
	model sees the same context the old ``build_prompt`` flattening provided.
	"""
	role = (item.get("role") or "user").lower()
	content = item.get("content") or ""
	metadata = item.get("metadata")
	if not isinstance(metadata, dict):
		metadata = None

	parts = [content] if content else []
	if metadata:
		parts.extend(_build_attachment_metadata_lines(metadata.get("files")))
		parts.extend(_build_document_extraction_lines(metadata.get("document_extractions")))

	text = "\n".join(part for part in parts if part).strip()
	if not text:
		return None

	if role == "assistant":
		return AIMessage(content=text)
	if role == "system":
		return SystemMessage(content=text)
	return HumanMessage(content=text)
