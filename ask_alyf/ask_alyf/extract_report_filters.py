"""Extract `filters` from `frappe.query_reports["X"] = {...}` JS files."""

import re
from pathlib import Path
from typing import Any

import frappe
from babel.messages.jslexer import tokenize, unquote_string

EXTEND_RE = re.compile(r"(?:\$\.extend|Object\.assign)\(\s*(?:\{\s*\}\s*,\s*)?([A-Za-z0-9_.]+)")
JS_FILTER_KEYS = {
	"fieldname",
	"label",
	"fieldtype",
	"options",
	"default",
	"reqd",
	"mandatory",
	"depends_on",
	"mandatory_depends_on",
}
# Frappe splits these fieldtypes' options with `"\n"` and keeps "" as a real choice. Link options stay one string.
CHOICE_FIELDTYPES = {"Select", "Autocomplete"}
TODAY_FUNCTIONS = {
	"frappe.datetime.get_today",
	"frappe.datetime.nowdate",
	"frappe.datetime.now_date",
}
_MISSING = object()


def filters_from_doc(filters) -> list[dict[str, Any]]:
	rows = []
	for f in filters or []:
		row = build_filter(
			fieldname=f.fieldname,
			label=f.label,
			fieldtype=f.fieldtype,
			options=f.options,
			default=f.default,
			reqd=f.mandatory,
		)
		if row:
			rows.append(row)
	return rows


def filters_from_js(script: str) -> list[dict[str, Any]]:
	chunks = [extended_script(identifier) for identifier in EXTEND_RE.findall(script)]
	chunks.append(script)
	rows: list[dict[str, Any]] = []
	seen: set[str] = set()
	for chunk in chunks:
		for row in read_filter_objects(chunk or ""):
			name = row["fieldname"]
			if name in seen:
				continue
			seen.add(name)
			rows.append(row)
	return rows


def extended_script(identifier: str) -> str:
	"""JS behind `$.extend` / `Object.assign`.

	`erpnext.accounts.foo` maps to `erpnext/public/js/foo.js` when that file assigns `identifier`.
	Other apps may need a full-path lookup.
	"""
	parts = identifier.split(".")
	if len(parts) < 2 or parts[0] not in frappe.get_installed_apps():
		return ""
	path = Path(frappe.get_app_path(parts[0])) / "public" / "js" / f"{parts[-1]}.js"
	if not path.is_file():
		return ""
	text = path.read_text(encoding="utf-8", errors="ignore")
	return text if f"{identifier} =" in text else ""


def read_filter_objects(script: str) -> list[dict[str, Any]]:
	# dotted=True keeps frappe.datetime.get_today as one name token.
	tokens = [
		token
		for token in tokenize(script, jsx=False, dotted=True)
		if token.type not in ("linecomment", "multilinecomment")
	]
	rows = []
	for index, token in enumerate(tokens):
		if token.value != "{":
			continue
		row = read_filter_object(tokens, index)
		if row:
			rows.append(row)
	return rows


def read_filter_object(tokens, start: int) -> dict[str, Any] | None:
	properties: dict[str, Any] = {}
	index, depth, token_count = start, 0, len(tokens)
	while index < token_count:
		value = tokens[index].value
		if value in "{[":
			depth += 1
		elif value in "}]":
			depth -= 1
			if depth == 0:
				break
		elif (
			depth == 1
			and tokens[index].type == "name"
			and value in JS_FILTER_KEYS
			and index + 1 < token_count
			and tokens[index + 1].value == ":"
		):
			literal, index = read_literal(tokens, index + 2)
			if literal is not None:
				properties[value] = literal
			continue
		index += 1
	return build_filter(
		fieldname=properties.get("fieldname"),
		label=properties.get("label"),
		fieldtype=properties.get("fieldtype"),
		options=properties.get("options"),
		default=properties.get("default"),
		reqd=properties.get("reqd") or properties.get("mandatory"),
		depends_on=properties.get("depends_on"),
		mandatory_depends_on=properties.get("mandatory_depends_on"),
	)


def read_literal(tokens, index):
	if index >= len(tokens):
		return None, index
	token = tokens[index]
	if token.type in ("string", "template_string"):
		return unquote_string(token.value), index + 1
	if token.value in ("true", "false"):
		return token.value == "true", index + 1
	if (
		token.value == "-"
		and index + 1 < len(tokens)
		and re.fullmatch(r"\d+(?:\.\d+)?", tokens[index + 1].value)
	):
		number_text = tokens[index + 1].value
		return (-float(number_text) if "." in number_text else -int(number_text)), index + 2
	if re.fullmatch(r"\d+(?:\.\d+)?", token.value):
		return (float(token.value) if "." in token.value else int(token.value)), index + 1
	if (
		token.value == "__"
		and index + 3 < len(tokens)
		and tokens[index + 1].value == "("
		and tokens[index + 2].type == "string"
		and tokens[index + 3].value == ")"
	):
		return unquote_string(tokens[index + 2].value), index + 4
	if token.value == "[":
		return read_string_list(tokens, index)
	date_value, next_index = read_date_expression(tokens, index)
	if next_index != index:
		return date_value, next_index
	return None, skip_unknown_value(tokens, index)


def read_string_list(tokens, index):
	"""One submitted value per array entry, in order. `""` is a real option."""
	values = []
	index += 1
	depth = 1
	while index < len(tokens) and depth:
		token = tokens[index]
		if depth == 1 and token.value == "{":
			entry, index = read_option_value(tokens, index)
			if entry is not None:
				values.append(entry)
			continue
		if depth == 1 and token.value not in (",", "]", "}", ")"):
			literal, next_index = read_literal(tokens, index)
			if next_index == index:
				index += 1
				continue
			if literal is not None:
				values.append(literal)
			index = next_index
			continue
		if token.value in "{[(":
			depth += 1
		elif token.value in "}])":
			depth -= 1
			if depth == 0:
				break
		index += 1
	return values, index + 1


def read_option_value(tokens, index):
	"""Submitted `value` of one `{value, label}` option. `index` sits on `{`."""
	entry = _MISSING
	index += 1
	depth = 1
	while index < len(tokens) and depth:
		value = tokens[index].value
		if depth == 1 and value == "value" and token_value(tokens, index + 1) == ":":
			literal, index = read_literal(tokens, index + 2)
			if literal is not None:
				entry = literal
			continue
		if value in "{[(":
			depth += 1
		elif value in "}])":
			depth -= 1
			if depth == 0:
				break
		index += 1
	return (None if entry is _MISSING else entry), index + 1


def token_value(tokens, index):
	if index >= len(tokens):
		return ""
	return tokens[index].value


def read_date_expression(tokens, index):
	"""Resolve the date calls used as report filter defaults to a YYYY-MM-DD string."""
	if index >= len(tokens) or tokens[index].type != "name":
		return None, index
	function_name = tokens[index].value
	if token_value(tokens, index + 1) != "(":
		return None, index

	if function_name in TODAY_FUNCTIONS and token_value(tokens, index + 2) == ")":
		return frappe.utils.today(), index + 3

	if function_name == "frappe.datetime.month_start" and token_value(tokens, index + 2) == ")":
		return frappe.utils.get_first_day(frappe.utils.today(), as_str=True), index + 3
	if function_name == "frappe.datetime.month_end" and token_value(tokens, index + 2) == ")":
		return frappe.utils.get_last_day(frappe.utils.today()).isoformat(), index + 3
	if function_name == "frappe.datetime.year_start" and token_value(tokens, index + 2) == ")":
		return frappe.utils.get_year_start(frappe.utils.today(), as_str=True), index + 3

	if function_name in ("frappe.datetime.add_months", "frappe.datetime.add_days"):
		start_date, next_index = read_date_expression(tokens, index + 2)
		if start_date is None or token_value(tokens, next_index) != ",":
			return None, index
		offset, next_index = read_signed_integer(tokens, next_index + 1)
		if offset is None or token_value(tokens, next_index) != ")":
			return None, index
		shift_date = (
			frappe.utils.add_months
			if function_name == "frappe.datetime.add_months"
			else frappe.utils.add_days
		)
		return frappe.utils.getdate(shift_date(start_date, offset)).isoformat(), next_index + 1

	if function_name == "erpnext.utils.get_fiscal_year":
		return read_fiscal_year(tokens, index)
	return None, index


def read_fiscal_year(tokens, index):
	"""`get_fiscal_year(today)` is the year name, `[1]` its start date, `[2]` its end date."""
	start_date, next_index = read_date_expression(tokens, index + 2)
	if start_date is None:
		return None, index
	if token_value(tokens, next_index) == ",":
		if token_value(tokens, next_index + 1) != "true" or token_value(tokens, next_index + 2) != ")":
			return None, index
		next_index += 3
	elif token_value(tokens, next_index) == ")":
		next_index += 1
	else:
		return None, index

	# [0] year name, [1] year start, [2] year end — same tuple as the JS helper.
	part = 0
	if token_value(tokens, next_index) == "[" and token_value(tokens, next_index + 2) == "]":
		part_token = token_value(tokens, next_index + 1)
		if not re.fullmatch(r"\d+", part_token):
			return None, index
		part = int(part_token)
		next_index += 3

	from erpnext.accounts.utils import get_fiscal_year

	fiscal_year = get_fiscal_year(start_date, raise_on_missing=False)
	if not fiscal_year:
		return None, next_index
	if part == 0:
		return fiscal_year[0], next_index
	if part in (1, 2):
		return frappe.utils.getdate(fiscal_year[part]).isoformat(), next_index
	return None, next_index


def read_signed_integer(tokens, index):
	sign = -1 if token_value(tokens, index) == "-" else 1
	number_index = index + 1 if sign < 0 else index
	number = token_value(tokens, number_index)
	if re.fullmatch(r"\d+", number):
		return sign * int(number), number_index + 1
	return None, index


def skip_unknown_value(tokens, index):
	"""Index of the comma or bracket that ends a value we do not parse."""
	depth = 0
	while index < len(tokens):
		value = tokens[index].value
		if value in "{[(":
			depth += 1
		elif value in "}])":
			if depth == 0:
				return index
			depth -= 1
		elif value == "," and depth == 0:
			return index
		index += 1
	return index


def build_filter(
	fieldname=None,
	label=None,
	fieldtype=None,
	options=None,
	default=None,
	reqd=None,
	depends_on=None,
	mandatory_depends_on=None,
) -> dict[str, Any] | None:
	if not fieldname:
		return None
	row = {
		"fieldname": fieldname,
		"label": label or fieldname,
		"fieldtype": fieldtype or "Data",
		"reqd": 1 if reqd else 0,
	}
	if fieldtype in CHOICE_FIELDTYPES and isinstance(options, str):
		options = options.split("\n")
	if options not in (None, ""):
		row["options"] = options
	if isinstance(default, list) and len(default) == 1 and fieldtype == "Select":
		default = default[0]
	if default is not None and (default != "" or fieldtype in CHOICE_FIELDTYPES):
		row["default"] = default
	elif fieldtype == "Link" and options == "Company":
		row["default"] = frappe.defaults.get_user_default("Company")
	elif fieldtype == "Link" and options == "Fiscal Year":
		from erpnext.accounts.utils import get_fiscal_year

		if fiscal_year := get_fiscal_year(frappe.utils.today(), raise_on_missing=False):
			row["default"] = fiscal_year[0]
	if depends_on:
		row["depends_on"] = depends_on
	if mandatory_depends_on:
		row["mandatory_depends_on"] = mandatory_depends_on
	return row
