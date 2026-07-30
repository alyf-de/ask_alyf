from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from langchain_core.messages import AIMessage

from ask_alyf.ask_alyf import api, field_contexts
from ask_alyf.ask_alyf.api import _truncate_doc_for_size


class UnitTestFieldContexts(FrappeTestCase):
	def test_get_field_context_mapped_returns_specific_entry(self):
		"""Print Format entry has correct system_prompt, jinja_globals, jinja_filters, render_context_vars."""
		result = field_contexts.get_field_context("Print Format", "html", "Code")

		self.assertIsInstance(result["system_prompt"], str)
		self.assertGreater(len(result["system_prompt"]), 0)

		self.assertIn("frappe", result["jinja_globals"])

		for expected_filter in ["json", "len", "int", "str", "flt"]:
			self.assertIn(expected_filter, result["jinja_filters"])

		render_vars = result["render_context_vars"]
		self.assertIsNotNone(render_vars)
		self.assertIn("doc", render_vars)

	def test_get_field_context_unmapped_returns_generic_fallback(self):
		"""Unmapped field returns a generic fallback whose system_prompt mentions doctype, fieldname, fieldtype."""
		# Prevent frappe.get_meta from being called (Customer may not exist in test DB).
		with patch("ask_alyf.ask_alyf.field_contexts.build_generic_fallback") as mock_build:
			mock_build.return_value = {
				"system_prompt": (
					"You are assisting a user who is editing the `notes` field"
					" on a `Customer` document in Frappe/ERPNext.\n\n"
					"Field label: notes\n"
					"Field type: Small Text\n"
				),
				"jinja_globals": [],
				"jinja_filters": None,
				"render_context_vars": None,
				"safe_exec_env": None,
			}
			result = field_contexts.get_field_context("Customer", "notes", "Small Text")

		self.assertIn("Customer", result["system_prompt"])
		self.assertIn("notes", result["system_prompt"])
		self.assertIn("Small Text", result["system_prompt"])

	def test_get_field_context_unmapped_via_build_generic_fallback_directly(self):
		"""build_generic_fallback itself embeds all three identifiers in the system_prompt."""
		result = field_contexts.build_generic_fallback(
			doctype="Customer",
			fieldname="notes",
			fieldtype="Small Text",
			label="Notes",
		)

		self.assertIn("Customer", result["system_prompt"])
		self.assertIn("notes", result["system_prompt"])
		self.assertIn("Small Text", result["system_prompt"])


class UnitTestFieldAgent(FrappeTestCase):
	def _make_fake_agent(self, output: str = "OK"):
		"""Build a fake stateless agent whose `invoke` returns a native message list."""
		agent = MagicMock()
		agent.invoke.return_value = {"messages": [AIMessage(content=output)]}
		return agent

	def test_run_field_agent_creates_no_conversation(self):
		"""run_field_agent must not create any Ask ALYF Conversation rows."""
		fake_agent = self._make_fake_agent("{{ doc.name }}")

		with (
			patch("ask_alyf.ask_alyf.field_agent.build_stateless_agent", return_value=fake_agent),
			patch("ask_alyf.ask_alyf.field_agent.tools.get_settings") as mock_settings,
		):
			mock_settings.return_value = SimpleNamespace(
				model="gpt-test",
				llm_provider="OpenAI",
				base_url="",
				get_password=lambda _f, raise_exception=False: "test-key",
			)
			# Snapshot count before
			count_before = frappe.db.count("Ask ALYF Conversation")

			from ask_alyf.ask_alyf import field_agent

			field_agent.run_field_agent(
				doctype="Print Format",
				fieldname="html",
				fieldtype="Code",
				current_value="<p>old</p>",
				doc={"name": "TEST-PF"},
				prompt="add grand total in bold",
			)

			count_after = frappe.db.count("Ask ALYF Conversation")

		self.assertEqual(count_before, count_after)

	def test_run_field_agent_no_realtime_published(self):
		"""run_field_agent must never call frappe.publish_realtime."""
		fake_agent = self._make_fake_agent("generated output")

		with (
			patch("ask_alyf.ask_alyf.field_agent.build_stateless_agent", return_value=fake_agent),
			patch("ask_alyf.ask_alyf.field_agent.tools.get_settings") as mock_settings,
			patch.object(frappe, "publish_realtime") as mock_realtime,
		):
			mock_settings.return_value = SimpleNamespace(
				model="gpt-test",
				llm_provider="OpenAI",
				base_url="",
				get_password=lambda _f, raise_exception=False: "test-key",
			)

			from ask_alyf.ask_alyf import field_agent

			field_agent.run_field_agent(
				doctype="Print Format",
				fieldname="html",
				fieldtype="Code",
				current_value="",
				doc={"name": "TEST-PF"},
				prompt="generate header",
			)

		mock_realtime.assert_not_called()

	def test_run_field_agent_uses_stateless_agent_without_checkpointer(self):
		"""run_field_agent must build a stateless agent with no checkpointer or subagents."""
		from ask_alyf.ask_alyf import field_agent

		fake_agent = self._make_fake_agent("ok")
		with (
			patch("ask_alyf.ask_alyf.field_agent.build_stateless_agent", return_value=fake_agent) as build,
			patch("ask_alyf.ask_alyf.field_agent.tools.get_settings") as mock_settings,
		):
			mock_settings.return_value = SimpleNamespace(
				model="gpt-test",
				llm_provider="OpenAI",
				base_url="",
				get_password=lambda _f, raise_exception=False: "test-key",
			)
			field_agent.run_field_agent(
				doctype="Print Format",
				fieldname="html",
				fieldtype="Code",
				current_value="",
				doc={"name": "TEST-PF"},
				prompt="generate header",
			)

		build.assert_called_once()
		# build_stateless_agent(model, tools, system_prompt=...) — no checkpointer/subagents/VFS kwargs.
		kwargs = build.call_args.kwargs
		self.assertEqual(set(kwargs.keys()), {"system_prompt"})


class UnitTestFieldAgentEndpoint(FrappeTestCase):
	def test_field_agent_run_endpoint_perm_gate_role(self):
		"""field_agent_run raises when user lacks Ask ALYF User role."""
		with (
			patch.object(api.frappe, "get_roles", return_value=["Desk User"]),
			patch.dict(api.frappe.session, {"user": "test@example.com"}),
		):
			with self.assertRaises(frappe.exceptions.ValidationError):
				api.field_agent_run(
					doctype="Print Format",
					fieldname="html",
					fieldtype="Code",
					current_value="",
					doc="{}",
					prompt="generate",
				)


class UnitTestFieldContextsAccuracy(FrappeTestCase):
	def test_field_contexts_accuracy(self):
		"""Every jinja_global must exist in get_jenv().globals; every jinja_filter in get_jenv().filters;
		every safe_exec_env name as a top-level key in get_safe_globals()."""
		import frappe.utils.jinja as _jinja
		import frappe.utils.safe_exec as _safe_exec

		jenv = _jinja.get_jenv()
		jenv_globals = set(jenv.globals.keys())
		jenv_filters = set(jenv.filters.keys())
		safe_globals = set(_safe_exec.get_safe_globals().keys())

		def _check_entry(entry: dict, context_label: str) -> None:
			# jinja_globals: must resolve in jenv.globals
			for name in entry.get("jinja_globals") or []:
				self.assertIn(
					name,
					jenv_globals,
					f"{context_label} jinja_globals entry {name!r} not found in get_jenv().globals",
				)

			# jinja_filters: must resolve in jenv.filters
			for name in entry.get("jinja_filters") or []:
				self.assertIn(
					name,
					jenv_filters,
					f"{context_label} jinja_filters entry {name!r} not found in get_jenv().filters",
				)

			# safe_exec_env: must be a top-level key in get_safe_globals()
			for name in entry.get("safe_exec_env") or []:
				self.assertIn(
					name,
					safe_globals,
					f"{context_label} safe_exec_env entry {name!r} not found in get_safe_globals()",
				)

			# render_context_vars: documentation-only — assert non-empty strings only
			for name in entry.get("render_context_vars") or []:
				self.assertIsInstance(name, str)
				self.assertGreater(
					len(name),
					0,
					f"{context_label} render_context_vars contains empty string",
				)

		for (doctype, fieldname), entry in field_contexts.FIELD_CONTEXTS.items():
			_check_entry(entry, f"({doctype!r}, {fieldname!r})")

		for (doctype, fieldname), variant_entry in field_contexts.FIELD_CONTEXT_VARIANTS.items():
			self.assertIn(
				variant_entry["default_variant"],
				variant_entry["variants"],
				f"({doctype!r}, {fieldname!r}) default_variant {variant_entry['default_variant']!r}"
				" not in variants map",
			)
			for variant_name, variant in variant_entry["variants"].items():
				_check_entry(variant, f"({doctype!r}, {fieldname!r}, variant={variant_name!r})")


class UnitTestFieldContextVariants(FrappeTestCase):
	def test_print_format_html_jinja_doctype_variant(self):
		"""Jinja + DocType resolves to the doc/letter_head Jinja prompt."""
		ctx = field_contexts.get_field_context(
			"Print Format",
			"html",
			"Code",
			doc={"print_format_type": "Jinja", "print_format_for": "DocType"},
		)
		self.assertIn("print_format_for = DocType", ctx["system_prompt"])
		self.assertIn("frappe", ctx["jinja_globals"])
		self.assertIn("doc", ctx["render_context_vars"])
		self.assertIn("letter_head", ctx["render_context_vars"])

	def test_print_format_html_jinja_report_variant(self):
		"""Jinja + Report resolves to the report-context Jinja prompt."""
		ctx = field_contexts.get_field_context(
			"Print Format",
			"html",
			"Code",
			doc={"print_format_type": "Jinja", "print_format_for": "Report"},
		)
		self.assertIn("print_format_for = Report", ctx["system_prompt"])
		self.assertIn("data", ctx["render_context_vars"])
		self.assertIn("columns", ctx["render_context_vars"])
		self.assertIn("filters", ctx["render_context_vars"])

	def test_print_format_html_js_report_variant(self):
		"""JS + Report resolves to the client-side microtemplate prompt."""
		ctx = field_contexts.get_field_context(
			"Print Format",
			"html",
			"Code",
			doc={"print_format_type": "JS", "print_format_for": "Report"},
		)
		self.assertIn("microtemplate", ctx["system_prompt"])
		self.assertIn("frappe.render_template", ctx["system_prompt"])
		self.assertEqual(ctx["jinja_globals"], [])
		self.assertIsNone(ctx["jinja_filters"])
		self.assertIn("data", ctx["render_context_vars"])

	def test_print_format_html_default_when_doc_missing(self):
		"""No doc passed → defaults to (Jinja, DocType) variant."""
		ctx = field_contexts.get_field_context("Print Format", "html", "Code")
		self.assertIn("print_format_for = DocType", ctx["system_prompt"])

	def test_print_format_html_js_doctype_falls_back_to_default(self):
		"""(JS, DocType) is uncommon at custom_format=1 — fall back to (Jinja, DocType)."""
		ctx = field_contexts.get_field_context(
			"Print Format",
			"html",
			"Code",
			doc={"print_format_type": "JS", "print_format_for": "DocType"},
		)
		self.assertIn("print_format_for = DocType", ctx["system_prompt"])
		self.assertIn("frappe", ctx["jinja_globals"])

	def test_system_console_python_variant(self):
		"""System Console with type=Python resolves to the Python safe_exec prompt."""
		ctx = field_contexts.get_field_context("System Console", "console", "Code", doc={"type": "Python"})
		self.assertIn("safe_exec", ctx["system_prompt"])
		self.assertEqual(ctx["safe_exec_env"], ["frappe", "json", "dict"])

	def test_system_console_sql_variant(self):
		"""System Console with type=SQL resolves to the read-only SQL prompt."""
		ctx = field_contexts.get_field_context("System Console", "console", "Code", doc={"type": "SQL"})
		self.assertIn("SQL", ctx["system_prompt"])
		self.assertIn("read_sql", ctx["system_prompt"])
		self.assertIsNone(ctx["safe_exec_env"])

	def test_unknown_discriminator_falls_back_to_default(self):
		"""An unrecognized discriminator value falls back to the default variant."""
		ctx = field_contexts.get_field_context("System Console", "console", "Code", doc={"type": "Bogus"})
		self.assertIn("safe_exec", ctx["system_prompt"])  # Python default


class UnitTestDocPayloadTruncation(FrappeTestCase):
	def _make_large_doc(self, n_rows: int = 50) -> dict:
		"""Build a doc dict whose 'items' child table has n_rows rows, each ~500 bytes."""
		row_template = {
			"item_code": "ITEM-001",
			"item_name": "A" * 200,
			"qty": 1,
			"rate": 100.0,
			"amount": 100.0,
		}
		return {
			"name": "SO-0001",
			"customer": "Test Customer",
			"items": [dict(row_template) for _ in range(n_rows)],
		}

	def test_doc_payload_truncation_caps_at_20_rows(self):
		"""_truncate_doc_for_size caps child tables at 20 rows when size exceeds threshold."""
		doc = self._make_large_doc(50)

		# Force threshold to 0 so truncation always triggers (doc is ~25 KB already, but be explicit)
		result = _truncate_doc_for_size(doc, table_fieldnames={"items"}, threshold_bytes=0, max_rows=20)

		self.assertIs(result, doc, "_truncate_doc_for_size should return the same dict")
		self.assertEqual(len(result["items"]), 21)  # 20 data rows + 1 synthetic _truncated row

	def test_doc_payload_truncation_synthetic_key_present(self):
		"""The truncated sentinel row contains the _truncated key."""
		doc = self._make_large_doc(50)
		_truncate_doc_for_size(doc, table_fieldnames={"items"}, threshold_bytes=0, max_rows=20)

		last_row = doc["items"][-1]
		self.assertIn("_truncated", last_row)
		self.assertIn("20", last_row["_truncated"])
		self.assertIn("50", last_row["_truncated"])

	def test_doc_payload_truncation_no_op_when_under_threshold(self):
		"""_truncate_doc_for_size leaves small docs untouched."""
		doc = {"name": "SMALL", "items": [{"item_code": "X"} for _ in range(5)]}
		result = _truncate_doc_for_size(
			doc, table_fieldnames={"items"}, threshold_bytes=16 * 1024, max_rows=20
		)
		self.assertEqual(len(result["items"]), 5)

	def test_doc_payload_truncation_detects_child_table_by_list_of_dicts(self):
		"""_truncate_doc_for_size treats any list-of-dicts as a child table even without table_fieldnames hint."""
		doc = self._make_large_doc(50)
		# Pass empty table_fieldnames — should still truncate via heuristic
		_truncate_doc_for_size(doc, table_fieldnames=set(), threshold_bytes=0, max_rows=20)

		self.assertEqual(len(doc["items"]), 21)
		self.assertIn("_truncated", doc["items"][-1])
