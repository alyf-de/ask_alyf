from __future__ import annotations

import base64
import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
	import tomllib
except ModuleNotFoundError:
	import tomli as tomllib

import frappe
from frappe import _, client
from frappe.utils import get_bench_path
from frappe.utils.data import cint

CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".md", ".json", ".yml", ".yaml", ".toml"}
READ_ONLY_SQL_RE = re.compile(r"^\s*(with|select|show|explain|describe|desc)\b", re.IGNORECASE)
FORBIDDEN_SQL_RE = re.compile(
	r"\b(insert|update|delete|drop|truncate|alter|create|grant|revoke|replace)\b", re.IGNORECASE
)
ENGLISH_LANGUAGE_CODES = {"en", "en-us", "en-gb"}
OPERATION_KIND_BACKEND = "backend_action"
OPERATION_KIND_FRONTEND = "frontend_action"
UNSAFE_PAYLOAD_KEYS = {"__proto__", "constructor", "prototype"}
FRONTEND_ACTION_TOOLS = {
	"set_route",
	"new_doc",
	"scroll_to_field",
	"frm_set_value",
	"frm_add_child",
	"show_chart",
}
AUTO_FRONTEND_ACTION_TOOLS = {"set_route", "new_doc", "scroll_to_field", "show_chart"}
ALLOWED_FRAPPE_CHART_TYPES = frozenset({"bar", "line", "scatter", "pie", "percentage", "donut", "axis-mixed"})
MAX_FRAPPE_CHARTS_PER_MESSAGE = 8
MAX_FRAPPE_CHART_LABELS = 100
MAX_FRAPPE_CHART_DATASETS = 20
# Frappe Charts subtracts ~130px (margins, padding, title, legend) from this value;
# values below ~200 yield a non-positive plot height and NaN axis geometry in draw.js.
MIN_FRAPPE_CHART_HEIGHT = 240
MAX_FRAPPE_CHART_HEIGHT = 800
VISION_SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
VISION_MIME_TYPES = {
	".png": "image/png",
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".gif": "image/gif",
	".webp": "image/webp",
}
MAX_VISION_PAGES = 10
VISION_DPI = 200
DEFAULT_DOCUMENT_EXTRACTION_PROMPT = (
	"Extract all structured data from this document. "
	"Use clearly labeled fields and include all text, tables, amounts, dates, names, "
	"addresses, references, and line items you can find."
)
JSON_OBJECT_OUTPUT_INSTRUCTION = (
	"Return only a valid JSON object. "
	"Do not wrap the JSON in markdown fences. "
	"Do not add explanatory prose before or after the JSON."
)


@dataclass(frozen=True)
class VisionModelConfig:
	"""Resolved provider settings for a document extraction request."""

	provider_name: str
	api_key: str
	api_base: str | None
	model_id: str


def coerce_int(value: Any, default: int, *, minimum: int | None = None) -> int:
	coerced = cint(value)
	if value not in {0, "0"} and not coerced:
		coerced = default

	if minimum is not None:
		coerced = max(minimum, coerced)

	return coerced


def coerce_field_list(value: str | list[str] | None) -> list[str] | None:
	if value is None:
		return None

	if isinstance(value, list):
		return [str(entry).strip() for entry in value if str(entry).strip()]

	stripped = str(value).strip()
	if not stripped:
		return None

	try:
		parsed = json.loads(stripped)
	except TypeError, json.JSONDecodeError:
		parsed = None

	if isinstance(parsed, list):
		return [str(entry).strip() for entry in parsed if str(entry).strip()]

	return [part.strip() for part in stripped.split(",") if part.strip()]


def get_settings():
	return frappe.get_single("Ask ALYF Settings")


def ensure_code_search_enabled():
	settings = get_settings()
	if not settings.is_code_search_enabled():
		frappe.throw(_("Code search is disabled in Ask ALYF Settings."))


def is_path_within(base_path: Path, target_path: Path) -> bool:
	return base_path == target_path or base_path in target_path.parents


def get_apps_path() -> Path:
	return Path(get_bench_path()).resolve() / "apps"


def get_installed_app_roots() -> dict[str, Path]:
	apps_path = get_apps_path()
	app_roots: dict[str, Path] = {}
	for app_name in frappe.get_installed_apps():
		app_root = apps_path / app_name
		if app_root.exists() and app_root.is_dir() and is_path_within(apps_path, app_root):
			app_roots[app_name] = app_root
	return app_roots


def get_installed_app_root(app_name: str) -> Path:
	ensure_code_search_enabled()
	app_name = (app_name or "").strip()
	if not app_name:
		frappe.throw(_("App name is required."))

	app_root = get_installed_app_roots().get(app_name)
	if not app_root:
		frappe.throw(_("App '{0}' is not installed.").format(app_name))

	return app_root


def get_path_parts(path_value: str) -> list[str]:
	cleaned_path = (path_value or "").strip().strip("/")
	if not cleaned_path or cleaned_path == ".":
		return []

	return [part for part in Path(cleaned_path).parts if part not in {"", "."}]


def normalize_app_relative_path(app_name: str, relative_path: str = "") -> Path:
	parts = get_path_parts(relative_path)
	if parts[:2] == ["apps", app_name]:
		parts = parts[2:]
	elif parts[:1] == [app_name]:
		parts = parts[1:]

	return Path(*parts) if parts else Path()


def resolve_installed_app_path(app_name: str, relative_path: str = "") -> tuple[Path, Path]:
	app_root = get_installed_app_root(app_name)
	relative_target = normalize_app_relative_path(app_name, relative_path)
	target = (app_root / relative_target).resolve()

	if not is_path_within(app_root.resolve(), target):
		frappe.throw(_("Code access is restricted to installed app directories."))

	return app_root, target


def resolve_code_search_roots(relative_path: str = "") -> list[tuple[str, Path, Path]]:
	ensure_code_search_enabled()
	app_roots = get_installed_app_roots()
	if not app_roots:
		frappe.throw(_("No installed app code roots were found."))

	parts = get_path_parts(relative_path)
	if parts[:1] == ["apps"]:
		parts = parts[1:]

	if not parts:
		return [(app_name, app_root, app_root) for app_name, app_root in app_roots.items()]

	app_name = parts[0]
	relative_target = "/".join(parts[1:])
	app_root, target = resolve_installed_app_path(app_name, relative_target)
	return [(app_name, app_root, target)]


def resolve_bench_app_path(path: str) -> tuple[Path, Path]:
	ensure_code_search_enabled()
	parts = get_path_parts(path)
	if parts[:1] == ["apps"]:
		parts = parts[1:]

	if not parts:
		frappe.throw(_("Path must start with an installed app name."))

	app_name = parts[0]
	relative_target = "/".join(parts[1:])
	return resolve_installed_app_path(app_name, relative_target)


def is_hidden_path(app_root: Path, path: Path) -> bool:
	return any(
		part.startswith(".")
		for part in Path(to_app_relative_path(app_root, path)).parts
		if part not in {"", "."}
	)


def to_app_relative_path(app_root: Path, path: Path) -> str:
	return path.resolve().relative_to(app_root.resolve()).as_posix() or "."


def to_bench_relative_path(path: Path) -> str:
	bench_path = Path(get_bench_path()).resolve()
	try:
		return path.relative_to(bench_path).as_posix()
	except ValueError:
		resolved_path = path.resolve()
		for app_root in get_installed_app_roots().values():
			real_app_root = app_root.resolve()
			if not is_path_within(real_app_root, resolved_path):
				continue
			return (app_root / resolved_path.relative_to(real_app_root)).relative_to(bench_path).as_posix()

		raise


def build_code_path_entry(app_root: Path, path: Path) -> dict[str, Any]:
	entry = {
		"name": path.name or app_root.name,
		"path": to_bench_relative_path(path),
		"app_relative_path": to_app_relative_path(app_root, path),
		"type": "directory" if path.is_dir() else "file",
	}
	if path.is_file():
		entry["size"] = path.stat().st_size
	return entry


def ensure_app_target_exists(app_root: Path, target: Path):
	if target.exists():
		return

	relative_path = to_app_relative_path(app_root, target)
	frappe.throw(_("Path '{0}' was not found in app '{1}'.").format(relative_path, app_root.name))


def iter_scoped_entries(
	app_root: Path,
	target: Path,
	*,
	recursive: bool,
	include_hidden: bool,
) -> list[Path]:
	ensure_app_target_exists(app_root, target)
	if target.is_file():
		return [target]

	results: list[Path] = []
	real_app_root = app_root.resolve()
	seen_paths = {target}
	pending_paths = [target]

	while pending_paths:
		current_path = pending_paths.pop()
		try:
			children = sorted(current_path.iterdir(), key=lambda child: child.name.lower())
		except Exception:
			continue

		for child in children:
			resolved_child = child.resolve()
			if resolved_child in seen_paths or not is_path_within(real_app_root, resolved_child):
				continue
			if not include_hidden and is_hidden_path(app_root, child):
				continue

			seen_paths.add(resolved_child)
			results.append(resolved_child)
			if recursive and resolved_child.is_dir():
				pending_paths.append(resolved_child)

	return results


def iter_scoped_files(app_root: Path, target: Path, include_hidden: bool = False) -> list[Path]:
	if target.is_file():
		ensure_app_target_exists(app_root, target)
		return [target]

	return [
		entry
		for entry in iter_scoped_entries(app_root, target, recursive=True, include_hidden=include_hidden)
		if entry.is_file()
	]


def get_excluded_doctypes() -> set[str]:
	settings = get_settings()

	parsed_doctypes: set[str] = set()
	for row in settings.excluded_doctypes or []:
		doctype = (row.excluded_doctype or "").strip()

		if doctype:
			parsed_doctypes.add(doctype)

	return parsed_doctypes


def ensure_editable_doctype(doctype: str):
	if doctype in get_excluded_doctypes():
		frappe.throw(_("DocType '{0}' is excluded from Agent mode.").format(_(doctype)))


def get_list(
	doctype: str,
	fields: str | list[str] | None = None,
	filters: dict[str, Any] | list | None = None,
	order_by: str | None = None,
	limit: int = 20,
	group_by: str | None = None,
) -> list[dict[str, Any]]:
	limit = coerce_int(limit, 20, minimum=1)
	fields = coerce_field_list(fields)
	return client.get_list(
		doctype=doctype,
		fields=fields,
		filters=filters,
		order_by=order_by,
		limit_page_length=limit,
		group_by=group_by,
	)


def get_count(doctype: str, filters: dict[str, Any] | list | None = None) -> int:
	return client.get_count(doctype=doctype, filters=filters)


def get_document(
	doctype: str, name: str | None = None, filters: dict[str, Any] | None = None
) -> dict[str, Any]:
	return client.get(doctype=doctype, name=name, filters=filters)


def get_value(
	doctype: str,
	fieldname: str | list[str],
	filters: dict[str, Any] | list | str | None = None,
) -> Any:
	return client.get_value(doctype=doctype, fieldname=fieldname, filters=filters)


def get_single_value(doctype: str, field: str) -> Any:
	return client.get_single_value(doctype=doctype, field=field)


def get_meta(doctype: str) -> dict[str, Any]:
	meta = frappe.get_meta(doctype)
	fields = []
	for df in meta.fields:
		fields.append(
			{
				"fieldname": df.fieldname,
				"label": df.label,
				"fieldtype": df.fieldtype,
				"options": df.options,
				"reqd": df.reqd,
				"read_only": df.read_only,
			}
		)

	return {
		"name": meta.name,
		"module": meta.module,
		"issingle": meta.issingle,
		"istable": meta.istable,
		"is_submittable": meta.is_submittable,
		"title_field": meta.title_field,
		"fields": fields,
		"permissions": [perm.as_dict() for perm in meta.permissions],
	}


def has_permission(doctype: str, docname: str, perm_type: str = "read") -> dict[str, bool]:
	return client.has_permission(doctype=doctype, docname=docname, perm_type=perm_type)


def get_doc_permissions(doctype: str, docname: str) -> dict[str, Any]:
	return client.get_doc_permissions(doctype=doctype, docname=docname)


def list_accessible_doctypes(permission_type: str = "read") -> list[str]:
	doctypes = frappe.get_all("DocType", filters={"istable": 0}, pluck="name")
	return [
		doctype
		for doctype in doctypes
		if frappe.has_permission(doctype, ptype=permission_type, user=frappe.session.user)
	]


def list_accessible_reports() -> list[dict[str, Any]]:
	reports = frappe.get_all(
		"Report",
		fields=["name", "ref_doctype", "report_type", "module"],
		filters={"disabled": 0},
		order_by="modified desc",
	)
	allowed = []
	for report in reports:
		if report.ref_doctype and frappe.has_permission(report.ref_doctype, ptype="report"):
			allowed.append(report)
	return allowed


def get_file_id(
	reference_doctype: str,
	reference_name: str,
	reference_field: str = "",
	file_url: str = "",
	file_name: str = "",
) -> str:
	"""Resolve a unique File ID from attachment reference filters."""
	filters = _build_file_reference_filters(
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		reference_field=reference_field,
		file_url=file_url,
		file_name=file_name,
	)
	matches = get_list("File", fields=["name"], filters=filters, order_by="modified desc", limit=2)
	if not matches:
		frappe.throw(_("No matching file was found."))

	if len(matches) > 1:
		frappe.throw(
			_("Multiple matching files were found. Provide reference_field, file_url, or file_name.")
		)

	return matches[0]["name"]


def translate_ui_labels(labels: list[str] | str, language: str | None = None) -> dict[str, Any]:
	if isinstance(labels, str):
		labels = [labels]
	elif not isinstance(labels, list):
		frappe.throw(_("Labels must be provided as a list of strings."))

	cleaned_labels: list[str] = []
	for label in labels:
		if not isinstance(label, str):
			frappe.throw(_("Each label must be a string."))
		label = label.strip()
		if label:
			cleaned_labels.append(label)

	if not cleaned_labels:
		frappe.throw(_("Provide at least one label to translate."))

	resolved_language = ((language or getattr(frappe.local, "lang", "") or "en").strip() or "en").replace(
		"_", "-"
	)
	translations = {label: _(label, lang=resolved_language) for label in dict.fromkeys(cleaned_labels)}

	return {
		"language": resolved_language,
		"is_english": resolved_language.lower() in ENGLISH_LANGUAGE_CODES,
		"translations": translations,
	}


def search_code(query: str, relative_path: str = "", limit: int = 20) -> list[dict[str, Any]]:
	ensure_code_search_enabled()
	limit = coerce_int(limit, 20, minimum=1)
	query = (query or "").strip()
	if not query:
		frappe.throw(_("Query is required."))

	query_lower = query.lower()
	results = []

	for _app_name, app_root, search_root in resolve_code_search_roots(relative_path):
		for file_path in sorted(iter_scoped_files(app_root, search_root), key=to_bench_relative_path):
			if file_path.suffix.lower() not in CODE_EXTENSIONS:
				continue

			try:
				lines = file_path.read_text(encoding="utf-8").splitlines()
			except Exception:
				continue

			for index, line in enumerate(lines, start=1):
				if query_lower in line.lower():
					results.append(
						{
							"path": to_bench_relative_path(file_path),
							"line": index,
							"snippet": line.strip(),
						}
					)
					if len(results) >= limit:
						return results

	return results


def read_code_file(path: str, start_line: int = 1, end_line: int = 200) -> dict[str, Any]:
	ensure_code_search_enabled()
	start_line = coerce_int(start_line, 1, minimum=1)
	end_line = coerce_int(end_line, 200, minimum=1)
	app_root, target = resolve_bench_app_path(path)
	ensure_app_target_exists(app_root, target)
	if not target.is_file():
		frappe.throw(_("Path '{0}' must point to a file inside an installed app.").format(path))

	lines = target.read_text(encoding="utf-8").splitlines()
	start = max(1, start_line)
	stop = min(len(lines), end_line)
	content = []
	for line_number in range(start, stop + 1):
		content.append(f"{line_number}: {lines[line_number - 1]}")

	return {
		"path": to_bench_relative_path(target),
		"content": "\n".join(content),
		"start_line": start,
		"end_line": stop,
	}


def ls(
	app_name: str,
	relative_path: str = "",
	recursive: bool = False,
	include_hidden: bool = False,
	limit: int = 200,
) -> dict[str, Any]:
	"""List files or directories inside an installed app, similar to Debian ls."""
	ensure_code_search_enabled()
	limit = coerce_int(limit, 200, minimum=1)
	app_root, target = resolve_installed_app_path(app_name, relative_path)
	entries = sorted(
		iter_scoped_entries(app_root, target, recursive=bool(recursive), include_hidden=bool(include_hidden)),
		key=to_bench_relative_path,
	)

	return {
		"app_name": app_name,
		"path": to_app_relative_path(app_root, target),
		"recursive": bool(recursive),
		"entries": [build_code_path_entry(app_root, entry) for entry in entries[:limit]],
	}


def find(
	app_name: str,
	name_pattern: str = "*",
	relative_path: str = "",
	entry_type: str = "any",
	include_hidden: bool = False,
	limit: int = 200,
) -> dict[str, Any]:
	"""Find files or directories inside an installed app, similar to Debian find."""
	ensure_code_search_enabled()
	limit = coerce_int(limit, 200, minimum=1)
	entry_type = (entry_type or "any").strip().lower()
	if entry_type not in {"any", "file", "directory"}:
		frappe.throw(_("Entry type must be one of any, file, or directory."))

	app_root, target = resolve_installed_app_path(app_name, relative_path)
	candidates = sorted(
		iter_scoped_entries(app_root, target, recursive=True, include_hidden=bool(include_hidden)),
		key=to_bench_relative_path,
	)
	if target.is_file():
		candidates = [target]

	matches = []
	for candidate in candidates:
		if entry_type == "file" and not candidate.is_file():
			continue
		if entry_type == "directory" and not candidate.is_dir():
			continue
		if not fnmatch.fnmatch(candidate.name, name_pattern):
			continue

		matches.append(build_code_path_entry(app_root, candidate))
		if len(matches) >= limit:
			break

	return {
		"app_name": app_name,
		"path": to_app_relative_path(app_root, target),
		"name_pattern": name_pattern,
		"entry_type": entry_type,
		"matches": matches,
	}


def grep(
	app_name: str,
	query: str,
	relative_path: str = "",
	file_pattern: str = "*",
	case_sensitive: bool = False,
	include_hidden: bool = False,
	limit: int = 50,
) -> dict[str, Any]:
	"""Search file contents inside an installed app, similar to Debian grep."""
	ensure_code_search_enabled()
	limit = coerce_int(limit, 50, minimum=1)
	query = (query or "").strip()
	if not query:
		frappe.throw(_("Query is required."))

	app_root, target = resolve_installed_app_path(app_name, relative_path)
	needle = query if case_sensitive else query.lower()
	matches = []

	for file_path in sorted(
		iter_scoped_files(app_root, target, include_hidden=bool(include_hidden)), key=to_bench_relative_path
	):
		if file_pattern and not fnmatch.fnmatch(file_path.name, file_pattern):
			continue

		try:
			lines = file_path.read_text(encoding="utf-8").splitlines()
		except Exception:
			continue

		for line_number, line in enumerate(lines, start=1):
			haystack = line if case_sensitive else line.lower()
			if needle not in haystack:
				continue

			matches.append(
				{
					"path": to_bench_relative_path(file_path),
					"line": line_number,
					"snippet": line.strip(),
				}
			)
			if len(matches) >= limit:
				return {
					"app_name": app_name,
					"path": to_app_relative_path(app_root, target),
					"query": query,
					"matches": matches,
				}

	return {
		"app_name": app_name,
		"path": to_app_relative_path(app_root, target),
		"query": query,
		"matches": matches,
	}


def read_file_record(file_id: str) -> dict[str, Any]:
	"""Read textual content from an accessible File record."""
	file_doc = _get_accessible_file_doc(file_id=file_id)
	content = file_doc.get_content()
	if isinstance(content, bytes):
		content = content.decode("utf-8", errors="replace")
	return {"file_id": file_doc.name, "file_name": file_doc.file_name, "content": content}


async def extract_document_data(file_id: str, extraction_prompt: str = "") -> dict[str, Any]:
	"""Extract structured JSON data from an accessible PDF or image file."""
	file_doc = _get_accessible_file_doc(file_id=file_id)
	images, total_pages = _file_to_base64_images(file_doc.get_full_path())
	if not images:
		frappe.throw(_("Could not extract visual content from the file."))

	model_config = _get_document_extraction_model_config(get_settings())
	content_parts = _build_document_extraction_content_parts(
		_build_document_extraction_prompt(extraction_prompt),
		images,
	)
	response = await _run_document_extraction_completion(model_config, content_parts)
	extracted_data = _parse_json_object_text(_get_completion_message_content(response))
	return _build_document_extraction_result(
		file_doc=file_doc,
		extracted_data=extracted_data,
		total_pages=total_pages,
		pages_processed=len(images),
	)


def _get_accessible_file_doc(
	*,
	file_id: str,
):
	"""Load a File document by ID and enforce read permission."""
	clean_file_id = (file_id or "").strip()
	if not clean_file_id:
		frappe.throw(_("File ID is required."))
	if not frappe.db.exists("File", clean_file_id):
		frappe.throw(_("File '{0}' was not found.").format(clean_file_id))

	file_doc = frappe.get_doc("File", clean_file_id)
	file_doc.check_permission("read")
	return file_doc


def _build_file_reference_filters(
	*,
	reference_doctype: str,
	reference_name: str,
	reference_field: str = "",
	file_url: str = "",
	file_name: str = "",
) -> dict[str, str]:
	"""Build a File lookup filter from the provided attachment reference."""
	clean_reference_doctype = (reference_doctype or "").strip()
	if not clean_reference_doctype:
		frappe.throw(_("Reference doctype is required."))

	clean_reference_name = (reference_name or "").strip()
	if not clean_reference_name:
		frappe.throw(_("Reference name is required."))

	filters = {
		"attached_to_doctype": clean_reference_doctype,
		"attached_to_name": clean_reference_name,
	}

	clean_reference_field = (reference_field or "").strip()
	if clean_reference_field:
		filters["attached_to_field"] = clean_reference_field

	clean_file_url = (file_url or "").strip()
	if clean_file_url:
		filters["file_url"] = clean_file_url

	clean_file_name = (file_name or "").strip()
	if clean_file_name:
		filters["file_name"] = clean_file_name

	return filters


def _get_document_extraction_model_config(settings) -> VisionModelConfig:
	"""Resolve the vision-capable model configuration from settings."""
	from ask_alyf.ask_alyf.doctype.ask_alyf_settings.ask_alyf_settings import get_any_llm_provider

	if settings.vision_model_is_chat_model:
		provider_label = settings.llm_provider
		api_base = (settings.base_url or "").strip() or None
		api_key = (settings.get_password("api_key", raise_exception=False) or "").strip()
		model_setting = (settings.model or "").strip()
	else:
		provider_label = settings.vision_llm_provider
		api_base = (settings.vision_base_url or "").strip() or None
		api_key = (settings.get_password("vision_api_key", raise_exception=False) or "").strip()
		model_setting = (settings.vision_model or "").strip()

	if not api_key:
		frappe.throw(_("Configure a vision model API key in Ask ALYF Settings."))
	if not model_setting:
		frappe.throw(_("Configure a vision model in Ask ALYF Settings."))

	return VisionModelConfig(
		provider_name=get_any_llm_provider(provider_label),
		api_key=api_key,
		api_base=api_base,
		model_id=_normalize_any_llm_model_id(model_setting),
	)


def _normalize_any_llm_model_id(model_setting: str) -> str:
	"""Strip an optional provider prefix from an any-llm model setting."""
	from any_llm import AnyLLM

	try:
		return AnyLLM.split_model_provider(model_setting)[1]
	except ValueError:
		return model_setting


def _build_document_extraction_prompt(extraction_prompt: str) -> str:
	"""Combine the user prompt with strict JSON output instructions."""
	prompt_text = (extraction_prompt or "").strip() or DEFAULT_DOCUMENT_EXTRACTION_PROMPT
	return f"{prompt_text}\n\n{JSON_OBJECT_OUTPUT_INSTRUCTION}"


def _build_document_extraction_content_parts(
	prompt_text: str,
	images: list[dict[str, str]],
) -> list[dict[str, Any]]:
	"""Build the multimodal message payload for the vision model."""
	content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
	for image in images:
		content_parts.append(_build_vision_image_part(image))
	return content_parts


def _build_vision_image_part(image: dict[str, str]) -> dict[str, Any]:
	"""Build one OpenAI-style image content block."""
	return {
		"type": "image_url",
		"image_url": {
			"url": f"data:{image['mime_type']};base64,{image['data']}",
			"detail": "high",
		},
	}


async def _run_document_extraction_completion(
	model_config: VisionModelConfig,
	content_parts: list[dict[str, Any]],
):
	"""Execute the vision completion request that returns JSON."""
	from any_llm import AnyLLM

	client = AnyLLM.create(
		provider=model_config.provider_name,
		api_key=model_config.api_key,
		api_base=model_config.api_base,
	)
	return await client.acompletion(
		model=model_config.model_id,
		messages=[{"role": "user", "content": content_parts}],
		temperature=0.1,
		response_format={"type": "json_object"},
	)


def _get_completion_message_content(response: Any) -> Any:
	"""Read the first completion message content or raise a user-facing error."""
	choices = getattr(response, "choices", None)
	if not isinstance(choices, list) or not choices:
		frappe.throw(_("Document extraction returned no choices."))

	message = getattr(choices[0], "message", None)
	content = getattr(message, "content", None)
	if content in (None, ""):
		frappe.throw(_("Document extraction returned no content."))

	return content


def _build_document_extraction_result(
	*,
	file_doc,
	extracted_data: dict[str, Any],
	total_pages: int,
	pages_processed: int,
) -> dict[str, Any]:
	"""Build the persisted extraction payload returned to the agent."""
	result: dict[str, Any] = {
		"file_id": file_doc.name,
		"file_name": file_doc.file_name,
		"pages_processed": pages_processed,
		"total_pages": total_pages,
		"extracted_data": extracted_data,
	}

	warning = _get_document_extraction_warning(total_pages=total_pages, pages_processed=pages_processed)
	if warning:
		result["truncated"] = True
		result["warning"] = warning

	return result


def _get_document_extraction_warning(*, total_pages: int, pages_processed: int) -> str | None:
	"""Describe when a document had more pages than the extraction cap allows."""
	if total_pages <= pages_processed:
		return None

	return (
		f"The document has {total_pages} pages but only the first {pages_processed} were processed. "
		"Data from later pages is not included."
	)


def _parse_json_object_text(raw_content: Any) -> dict[str, Any]:
	"""Parse a JSON object from raw model output."""
	if isinstance(raw_content, dict):
		return raw_content

	if not isinstance(raw_content, str):
		frappe.throw(_("Document extraction did not return text."))

	text = raw_content.strip()
	if not text:
		frappe.throw(_("Document extraction returned an empty response."))

	candidates = [text]
	fenced_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
	if fenced_match:
		candidates.insert(0, fenced_match.group(1).strip())

	start = text.find("{")
	end = text.rfind("}")
	if start != -1 and end != -1 and start < end:
		candidates.append(text[start : end + 1])

	for candidate in candidates:
		try:
			parsed = json.loads(candidate)
		except Exception:
			continue
		if isinstance(parsed, dict):
			return parsed

	frappe.throw(_("Document extraction did not return valid JSON."))


def _file_to_base64_images(file_path: str) -> tuple[list[dict[str, str]], int]:
	"""Render a supported document into base64-encoded image payloads."""
	path = Path(file_path)
	suffix = path.suffix.lower()

	if suffix == ".pdf":
		return _pdf_to_base64_images(file_path)
	if suffix in VISION_SUPPORTED_EXTENSIONS:
		return [_read_image_file_as_base64(path)], 1

	frappe.throw(_("Unsupported file type '{0}'. Supported types: PDF, PNG, JPG, GIF, WebP.").format(suffix))


def _read_image_file_as_base64(path: Path) -> dict[str, str]:
	"""Load an image file as a base64 payload for multimodal input."""
	mime_type = VISION_MIME_TYPES.get(path.suffix.lower(), "image/png")
	return {
		"data": base64.b64encode(path.read_bytes()).decode(),
		"mime_type": mime_type,
	}


def _pdf_to_base64_images(file_path: str) -> tuple[list[dict[str, str]], int]:
	"""Render the first N PDF pages into PNG payloads for the vision model."""
	import pymupdf

	doc = pymupdf.open(file_path)
	total_pages = len(doc)
	images: list[dict[str, str]] = []

	for page_num in range(min(total_pages, MAX_VISION_PAGES)):
		pix = doc[page_num].get_pixmap(dpi=VISION_DPI)
		images.append({"data": base64.b64encode(pix.tobytes("png")).decode(), "mime_type": "image/png"})

	doc.close()
	return images, total_pages


def get_print(
	doctype: str,
	name: str,
	conversation_name: str,
	print_format: str = "",
	letterhead: str = "",
	language: str = "",
) -> dict[str, Any]:
	"""Generate a PDF print of a document and attach it to the conversation."""
	from frappe.translate import print_language
	from frappe.utils.file_manager import save_file
	from frappe.utils.print_utils import get_print as frappe_get_print

	clean_doctype = (doctype or "").strip()
	clean_name = (name or "").strip()
	clean_conversation = (conversation_name or "").strip()

	if not clean_doctype:
		frappe.throw(_("DocType is required."))
	if not clean_name:
		frappe.throw(_("Document name is required."))
	if not clean_conversation:
		frappe.throw(_("Conversation name is required."))
	if not frappe.db.exists(clean_doctype, clean_name):
		frappe.throw(_("{0} '{1}' was not found.").format(_(clean_doctype), clean_name))

	doc = frappe.get_doc(clean_doctype, clean_name)
	doc.check_permission("print")

	resolved_print_format = (print_format or "").strip() or doc.meta.default_print_format or "Standard"
	resolved_letterhead = (letterhead or "").strip()
	if not resolved_letterhead:
		resolved_letterhead = frappe.db.get_value("Letter Head", {"is_default": 1}, "name") or ""

	resolved_language = (language or "").strip() or getattr(doc, "language", "") or ""

	with print_language(resolved_language):
		pdf_content = frappe_get_print(
			doctype=clean_doctype,
			name=clean_name,
			print_format=resolved_print_format,
			as_pdf=True,
			no_letterhead=0 if resolved_letterhead else 1,
			letterhead=resolved_letterhead,
		)

	file_name = f"{clean_name}.pdf".replace(" ", "-").replace("/", "-")
	file_doc = save_file(
		file_name,
		pdf_content,
		"Ask ALYF Conversation",
		clean_conversation,
		is_private=1,
	)

	return {
		"name": file_doc.name,
		"file_name": file_doc.file_name,
		"file_url": file_doc.file_url,
	}


def run_read_only_sql(query: str) -> list[dict[str, Any]]:
	roles = set(frappe.get_roles())
	if frappe.session.user != "Administrator" and "System Manager" not in roles:
		frappe.throw(_("Only Administrator and System Manager can run SQL queries."))

	if ";" in query.strip().rstrip(";"):
		frappe.throw(_("Only a single read-only SQL statement is allowed."))

	if not READ_ONLY_SQL_RE.match(query) or FORBIDDEN_SQL_RE.search(query):
		frappe.throw(_("Only read-only SQL statements are allowed."))

	return frappe.db.sql(query, as_dict=True)


def get_app_version(app_name: str) -> str:
	app_name = (app_name or "").strip()
	if not app_name:
		frappe.throw(_("App name is required."))

	if app_name not in frappe.get_installed_apps():
		frappe.throw(_("App '{0}' is not installed.").format(app_name))

	version = ""
	try:
		module_version = frappe.get_attr(f"{app_name}.__version__")
		if isinstance(module_version, str):
			version = module_version.strip()
	except Exception:
		version = ""

	if not version:
		project_data = get_app_pyproject_data(app_name)
		project_version = project_data.get("project", {}).get("version")
		if isinstance(project_version, str):
			version = project_version.strip()

	if not version:
		frappe.throw(_("No version was found for app '{0}'.").format(app_name))

	return version


def read_github_releases(app_name: str, limit: int = 5) -> list[dict[str, Any]]:
	limit = coerce_int(limit, 5, minimum=1)
	urls = get_project_urls(app_name)
	repository_url = urls.get("repository")
	if not repository_url:
		frappe.throw(_("No repository URL was found for app '{0}'.").format(app_name))

	parsed = urlparse(repository_url)
	parts = [part for part in parsed.path.split("/") if part]
	if parsed.netloc not in {"github.com", "www.github.com"} or len(parts) < 2:
		frappe.throw(_("Only GitHub repositories are supported for release lookup."))

	owner, repo = parts[0], parts[1].removesuffix(".git")
	api_url = f"https://api.github.com/repos/{owner}/{repo}/releases?per_page={limit}"
	request = Request(api_url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ask_alyf"})
	with urlopen(request, timeout=10) as response:
		payload = json.loads(response.read().decode("utf-8"))

	return [
		{
			"name": item.get("name") or item.get("tag_name"),
			"tag_name": item.get("tag_name"),
			"published_at": item.get("published_at"),
			"url": item.get("html_url"),
			"body": item.get("body", "")[:4000],
		}
		for item in payload
	]


def read_documentation_page(app_name: str, relative_path: str = "") -> dict[str, Any]:
	urls = get_project_urls(app_name)
	base_url = urls.get("documentation")
	if not base_url:
		frappe.throw(_("No documentation URL was found for app '{0}'.").format(app_name))

	if relative_path:
		target_url = base_url.rstrip("/") + "/" + relative_path.lstrip("/")
	else:
		target_url = base_url

	request = Request(target_url, headers={"User-Agent": "ask_alyf"})
	with urlopen(request, timeout=10) as response:
		content = response.read().decode("utf-8", errors="replace")

	return {"url": target_url, "content": content[:20000]}


def get_app_pyproject_data(app_name: str) -> dict[str, Any]:
	pyproject_path = Path(get_bench_path()) / "apps" / app_name / "pyproject.toml"
	if not pyproject_path.exists():
		frappe.throw(_("No pyproject.toml was found for app '{0}'.").format(app_name))

	return tomllib.loads(pyproject_path.read_text(encoding="utf-8"))


def get_project_urls(app_name: str) -> dict[str, str]:
	data = get_app_pyproject_data(app_name)
	urls = data.get("project", {}).get("urls", {})
	return {key.lower(): value for key, value in urls.items()}


def get_frontend_action_requires_confirmation(tool: str) -> bool:
	return tool not in AUTO_FRONTEND_ACTION_TOOLS


def validate_pending_operation_payload(
	kind: str,
	tool: str,
	payload: dict[str, Any],
) -> str | None:
	if kind == OPERATION_KIND_BACKEND:
		return validate_pending_action_payload(tool, payload)

	if kind == OPERATION_KIND_FRONTEND:
		return validate_frontend_action_payload(tool, payload)

	return _("Unsupported operation kind '{0}'.").format(kind)


def execute_pending_operation(operation: dict[str, Any]) -> dict[str, Any]:
	if not isinstance(operation, dict):
		frappe.throw(_("Invalid pending operation payload."))

	kind = (operation.get("kind") or "").strip()
	tool = (operation.get("tool") or "").strip()
	payload = coerce_object_payload(operation.get("payload"), "payload")
	validation_error = validate_pending_operation_payload(kind, tool, payload)
	if validation_error:
		frappe.throw(validation_error)

	if kind == OPERATION_KIND_BACKEND:
		return execute_action({"action": tool, **payload})

	if kind == OPERATION_KIND_FRONTEND:
		frappe.throw(_("Frontend actions must be executed in the browser."))

	frappe.throw(_("Unsupported operation kind '{0}'.").format(kind))


def execute_action(action: dict[str, Any]) -> dict[str, Any]:
	if not isinstance(action, dict):
		frappe.throw(_("Invalid action payload."))

	action_type = action.get("action")

	if action_type == "insert":
		doctype = action["doctype"]
		ensure_editable_doctype(doctype)
		values = coerce_object_payload(action.get("values"), "values")
		validation_error = validate_pending_action_payload("insert", {"doctype": doctype, "values": values})
		if validation_error:
			frappe.throw(validation_error)
		doc = {"doctype": doctype, **values}
		return client.insert(doc=doc)

	if action_type == "batch_insert":
		doctype = action["doctype"]
		ensure_editable_doctype(doctype)
		records = action.get("records")
		validation_error = validate_pending_action_payload(
			"batch_insert", {"doctype": doctype, "records": records}
		)
		if validation_error:
			frappe.throw(validation_error)

		created_names: list[str] = []
		failures: list[dict[str, Any]] = []
		for row_number, values in enumerate(records, start=1):
			try:
				created_doc = client.insert(doc={"doctype": doctype, **values})
			except Exception as error:
				failures.append(
					{
						"row": row_number,
						"error": str(error).strip() or _("Unknown error."),
					}
				)
				continue

			created_name = ""
			if isinstance(created_doc, dict):
				created_name = str(created_doc.get("name") or "").strip()
			else:
				created_name = str(getattr(created_doc, "name", "") or "").strip()
			if created_name:
				created_names.append(created_name)

		return build_batch_insert_result(
			doctype=doctype,
			total_records=len(records),
			created_names=created_names,
			failures=failures,
		)

	if action_type == "save":
		doctype = action["doctype"]
		ensure_editable_doctype(doctype)
		values = coerce_object_payload(action.get("values"), "values")
		validation_error = validate_pending_action_payload("save", {"doctype": doctype, "values": values})
		if validation_error:
			frappe.throw(validation_error)
		doc = client.get(doctype=doctype, name=action["name"])
		doc.update(values)
		return client.save(doc=doc)

	if action_type == "set_value":
		doctype = action["doctype"]
		ensure_editable_doctype(doctype)
		return client.set_value(
			doctype=doctype,
			name=action["name"],
			fieldname=action["fieldname"],
			value=action.get("value"),
		)

	if action_type == "submit":
		doctype = action["doctype"]
		ensure_editable_doctype(doctype)
		doc = client.get(doctype=doctype, name=action["name"])
		return client.submit(doc=doc)

	if action_type == "cancel":
		doctype = action["doctype"]
		ensure_editable_doctype(doctype)
		return client.cancel(doctype=doctype, name=action["name"])

	if action_type == "amend":
		doctype = action["doctype"]
		ensure_editable_doctype(doctype)
		doc = frappe.get_doc(doctype, action["name"])
		new_doc = frappe.copy_doc(doc)
		new_doc.amended_from = doc.name
		new_doc.insert()
		return new_doc.as_dict()

	if action_type == "delete":
		doctype = action["doctype"]
		ensure_editable_doctype(doctype)
		client.delete(doctype=doctype, name=action["name"])
		return {"message": _("Deleted {0} {1}").format(_(doctype), action["name"])}

	if action_type == "rename_doc":
		doctype = action["doctype"]
		ensure_editable_doctype(doctype)
		new_name = client.rename_doc(
			doctype=doctype,
			old_name=action["name"],
			new_name=action["new_name"],
			merge=bool(action.get("merge")),
		)
		return {"new_name": new_name}

	if action_type == "attach_file":
		doctype = action["doctype"]
		ensure_editable_doctype(doctype)
		file_doc = _get_accessible_file_doc(file_id=action["file_id"])
		attached = frappe.get_doc(
			{
				"doctype": "File",
				"file_url": file_doc.file_url,
				"file_name": file_doc.file_name,
				"is_private": file_doc.is_private,
				"attached_to_doctype": doctype,
				"attached_to_name": action["name"],
			}
		)
		attached.insert()
		return attached.as_dict()

	if action_type == "run_method":
		method_path = action["method"]
		method = frappe.get_attr(method_path)
		frappe.is_whitelisted(method)
		return frappe.call(method, **coerce_object_payload(action.get("args"), "args"))

	frappe.throw(_("Unsupported action '{0}'.").format(action_type))


def coerce_object_payload(value: Any, fieldname: str) -> dict[str, Any]:
	if value in (None, ""):
		return {}

	if isinstance(value, dict):
		return value

	if isinstance(value, str):
		try:
			parsed = json.loads(value)
		except Exception:
			parsed = None
		if isinstance(parsed, dict):
			return parsed

	frappe.throw(_("Action field '{0}' must be an object.").format(fieldname))


def build_batch_insert_result(
	*,
	doctype: str,
	total_records: int,
	created_names: list[str],
	failures: list[dict[str, Any]],
) -> dict[str, Any]:
	created_count = len(created_names)
	failed_count = len(failures)
	message = _("Created {0} of {1} {2} records.").format(created_count, total_records, _(doctype))
	failure_summary = summarize_batch_insert_failures(failures)
	if failure_summary:
		message += " " + failure_summary

	return {
		"message": message,
		"total": total_records,
		"created_count": created_count,
		"failed_count": failed_count,
		"created_names": created_names,
		"failed": failures,
	}


def summarize_batch_insert_failures(failures: list[dict[str, Any]], limit: int = 10) -> str:
	if not failures:
		return ""

	items: list[str] = []
	for failure in failures[:limit]:
		row = failure.get("row")
		error = (failure.get("error") or "").strip()
		if isinstance(row, int) and row > 0 and error:
			items.append(_("row {0}: {1}").format(row, error))
		elif isinstance(row, int) and row > 0:
			items.append(_("row {0}").format(row))
		elif error:
			items.append(error)

	if not items:
		return _("Some rows failed.")

	summary = _("Failed rows: {0}").format("; ".join(items))
	remaining = len(failures) - len(items)
	if remaining > 0:
		summary += " " + _("And {0} more.").format(remaining)
	return summary


def validate_pending_action_payload(action_type: str, payload: dict[str, Any]) -> str | None:
	if action_type in {"insert", "save"}:
		doctype = payload.get("doctype")
		if not isinstance(doctype, str) or not doctype.strip():
			return _("Action field 'doctype' is required.")

		values = payload.get("values")
		if not isinstance(values, dict):
			return _("Action field 'values' must be an object.")

		return validate_table_field_shapes(doctype, values)

	if action_type == "batch_insert":
		doctype = payload.get("doctype")
		if not isinstance(doctype, str) or not doctype.strip():
			return _("Action field 'doctype' is required.")

		records = payload.get("records")
		if not isinstance(records, list) or not records:
			return _("Action field 'records' must be a non-empty list.")

		for row_number, values in enumerate(records, start=1):
			if not isinstance(values, dict):
				return _("Action field 'records' row #{0} must be an object.").format(row_number)
			table_shape_error = validate_table_field_shapes(doctype, values)
			if table_shape_error:
				return table_shape_error

		return None

	if action_type == "attach_file":
		file_id = payload.get("file_id")
		if not isinstance(file_id, str) or not file_id.strip():
			return _("Action field 'file_id' is required.")

	if action_type == "run_method":
		args = payload.get("args")
		if args is not None and not isinstance(args, dict):
			return _("Action field 'args' must be an object.")

	return None


def has_unsafe_payload_keys(value: Any) -> bool:
	if isinstance(value, dict):
		for key, nested_value in value.items():
			if key in UNSAFE_PAYLOAD_KEYS:
				return True
			if has_unsafe_payload_keys(nested_value):
				return True
		return False

	if isinstance(value, list):
		for item in value:
			if has_unsafe_payload_keys(item):
				return True
		return False

	return False


def _coerce_chart_scalar(value: Any) -> float | str | None:
	if isinstance(value, bool):
		return None
	if isinstance(value, (int, float)):
		if isinstance(value, float) and not (value == value):  # NaN
			return None
		return float(value)
	if isinstance(value, str):
		stripped = value.strip()
		return stripped[:500] if stripped else None
	return None


def _sanitize_axis_chart_data(data: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
	labels_raw = data.get("labels")
	if not isinstance(labels_raw, list) or not labels_raw:
		return None, _("Chart data requires a non-empty labels array.")

	if len(labels_raw) > MAX_FRAPPE_CHART_LABELS:
		return None, _("Too many chart labels.")

	labels: list[str] = []
	for label in labels_raw:
		coerced = _coerce_chart_scalar(label)
		if coerced is None and label not in (0, 0.0):
			return None, _("Chart labels must be numbers or strings.")
		if coerced is None:
			labels.append("0")
		elif isinstance(coerced, float):
			labels.append(str(int(coerced)) if coerced == int(coerced) else str(coerced))
		else:
			labels.append(str(coerced))

	datasets_raw = data.get("datasets")
	if not isinstance(datasets_raw, list) or not datasets_raw:
		return None, _("Chart data requires a non-empty datasets array.")

	if len(datasets_raw) > MAX_FRAPPE_CHART_DATASETS:
		return None, _("Too many chart datasets.")

	datasets: list[dict[str, Any]] = []
	for row in datasets_raw:
		if not isinstance(row, dict):
			return None, _("Each dataset must be an object.")
		values_raw = row.get("values")
		if not isinstance(values_raw, list):
			return None, _("Each dataset needs a values array.")
		if len(values_raw) != len(labels):
			return None, _("Dataset values must match labels length.")

		values: list[float] = []
		for v in values_raw:
			if isinstance(v, bool):
				return None, _("Dataset values must be numeric.")
			if isinstance(v, (int, float)):
				if isinstance(v, float) and not (v == v):
					return None, _("Dataset values must be numeric.")
				values.append(float(v))
			else:
				return None, _("Dataset values must be numeric.")

		entry: dict[str, Any] = {"values": values}
		name = row.get("name")
		if isinstance(name, str) and name.strip():
			entry["name"] = name.strip()[:120]
		dtype = row.get("type")
		if isinstance(dtype, str) and dtype.strip() in {"bar", "line"}:
			entry["type"] = dtype.strip()
		datasets.append(entry)

	return {"labels": labels, "datasets": datasets}, None


def validate_frappe_chart_options(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
	if not isinstance(raw, dict):
		return None, _("Each chart must be an object.")

	ctype = raw.get("type")
	if not isinstance(ctype, str) or ctype not in ALLOWED_FRAPPE_CHART_TYPES:
		return None, _("Unsupported or missing chart type.")

	sanitized: dict[str, Any] = {"type": ctype}

	title = raw.get("title")
	if isinstance(title, str) and title.strip():
		sanitized["title"] = title.strip()[:240]

	height = raw.get("height")
	if height is not None:
		height_int = int(cint(height))
		if height_int == 0:
			pass
		else:
			height_int = max(MIN_FRAPPE_CHART_HEIGHT, height_int)
			if height_int > MAX_FRAPPE_CHART_HEIGHT:
				return None, _("Chart height is out of range.")
			sanitized["height"] = height_int

	colors = raw.get("colors")
	if isinstance(colors, list) and colors:
		safe_colors: list[str] = []
		for c in colors[:24]:
			if isinstance(c, str) and c.strip():
				safe_colors.append(c.strip()[:64])
		if safe_colors:
			sanitized["colors"] = safe_colors

	data = raw.get("data")
	if not isinstance(data, dict):
		return None, _("Chart data must be an object.")

	safe_data, err = _sanitize_axis_chart_data(data)
	if err or not safe_data:
		return None, err or _("Invalid chart data.")

	sanitized["data"] = safe_data

	bar_options = raw.get("barOptions")
	if isinstance(bar_options, dict):
		bo: dict[str, Any] = {}
		if "spaceRatio" in bar_options and isinstance(bar_options["spaceRatio"], (int, float)):
			ratio = float(bar_options["spaceRatio"])
			if 0 <= ratio <= 5:
				bo["spaceRatio"] = ratio
		if bo:
			sanitized["barOptions"] = bo

	line_options = raw.get("lineOptions")
	if isinstance(line_options, dict):
		lo: dict[str, Any] = {}
		if "dotSize" in line_options and isinstance(line_options["dotSize"], (int, float)):
			ds = float(line_options["dotSize"])
			if 0 <= ds <= 40:
				lo["dotSize"] = ds
		if lo:
			sanitized["lineOptions"] = lo

	axis_options = raw.get("axisOptions")
	if isinstance(axis_options, dict):
		ao: dict[str, Any] = {}
		for key in ("xAxisMode", "yAxisMode"):
			val = axis_options.get(key)
			if val in {"tick", "span"}:
				ao[key] = val
		if ao:
			sanitized["axisOptions"] = ao

	for flag in ("valuesOverPoints", "truncateLegends", "isNavigable"):
		if raw.get(flag) is True:
			sanitized[flag] = True

	max_slices = raw.get("maxSlices")
	if isinstance(max_slices, (int, float)):
		ms = int(max_slices)
		if 1 <= ms <= 50:
			sanitized["maxSlices"] = ms

	return sanitized, None


def validate_frappe_charts_payload(frappe_charts: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
	if not isinstance(frappe_charts, list) or not frappe_charts:
		return None, _("Provide at least one chart in frappe_charts.")

	if len(frappe_charts) > MAX_FRAPPE_CHARTS_PER_MESSAGE:
		return None, _("Too many charts in one request.")

	out: list[dict[str, Any]] = []
	for chart in frappe_charts:
		sanitized, err = validate_frappe_chart_options(chart)
		if err or not sanitized:
			return None, err or _("Invalid chart specification.")
		out.append(sanitized)

	return out, None


def validate_frontend_action_payload(tool: str, payload: dict[str, Any]) -> str | None:
	tool = (tool or "").strip()
	if tool not in FRONTEND_ACTION_TOOLS:
		return _("Unsupported frontend action '{0}'.").format(tool)

	if not isinstance(payload, dict):
		return _("Frontend action payload must be an object.")

	if has_unsafe_payload_keys(payload):
		return _("Frontend action payload contains an unsafe key.")

	if tool == "set_route":
		route = payload.get("route")
		if not isinstance(route, list) or not route:
			return _("Frontend action 'set_route' requires a non-empty route list.")
		for index, route_part in enumerate(route, start=1):
			if not isinstance(route_part, str) or not route_part.strip():
				return _("Route part #{0} must be a non-empty string.").format(index)
		return None

	if tool == "new_doc":
		doctype = payload.get("doctype")
		if not isinstance(doctype, str) or not doctype.strip():
			return _("Frontend action 'new_doc' requires a DocType.")
		route_options = payload.get("route_options")
		if route_options is not None and not isinstance(route_options, dict):
			return _("Frontend action 'new_doc' field 'route_options' must be an object.")
		return None

	if tool == "scroll_to_field":
		fieldname = payload.get("fieldname")
		if not isinstance(fieldname, str) or not fieldname.strip():
			return _("Frontend action 'scroll_to_field' requires a fieldname.")
		return None

	if tool == "frm_set_value":
		fieldname = payload.get("fieldname")
		if not isinstance(fieldname, str) or not fieldname.strip():
			return _("Frontend action 'frm_set_value' requires a fieldname.")
		doctype = payload.get("doctype")
		docname = payload.get("docname")
		if doctype is not None and (not isinstance(doctype, str) or not doctype.strip()):
			return _("Frontend action 'frm_set_value' field 'doctype' must be a string.")
		if docname is not None and (not isinstance(docname, str) or not docname.strip()):
			return _("Frontend action 'frm_set_value' field 'docname' must be a string.")
		return None

	if tool == "frm_add_child":
		fieldname = payload.get("fieldname")
		if not isinstance(fieldname, str) or not fieldname.strip():
			return _("Frontend action 'frm_add_child' requires a fieldname.")
		values = payload.get("values")
		if values is not None and not isinstance(values, dict):
			return _("Frontend action 'frm_add_child' field 'values' must be an object.")
		doctype = payload.get("doctype")
		docname = payload.get("docname")
		if doctype is not None and (not isinstance(doctype, str) or not doctype.strip()):
			return _("Frontend action 'frm_add_child' field 'doctype' must be a string.")
		if docname is not None and (not isinstance(docname, str) or not docname.strip()):
			return _("Frontend action 'frm_add_child' field 'docname' must be a string.")
		return None

	if tool == "show_chart":
		_validation = validate_frappe_charts_payload(payload.get("frappe_charts"))
		return _validation[1]

	return _("Unsupported frontend action '{0}'.").format(tool)


def validate_table_field_shapes(doctype: str, values: dict[str, Any]) -> str | None:
	meta = frappe.get_meta(doctype)
	table_fields = {
		df.fieldname: df.options
		for df in meta.fields
		if df.fieldtype == "Table" and df.fieldname and df.options
	}
	if not table_fields:
		return None

	for fieldname, child_doctype in table_fields.items():
		if fieldname not in values:
			continue

		field_value = values.get(fieldname)
		if field_value in (None, ""):
			continue

		if not isinstance(field_value, list):
			return _("Field '{0}' in {1} is a child table ({2}) and must be a list of row objects.").format(
				fieldname,
				_(doctype),
				_(child_doctype),
			)

		for index, row in enumerate(field_value, start=1):
			if not isinstance(row, dict):
				return _("Field '{0}' row #{1} in {2} must be an object.").format(
					fieldname,
					index,
					_(doctype),
				)

	return None
