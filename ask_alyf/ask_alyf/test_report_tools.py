from unittest.mock import patch
from frappe.tests import IntegrationTestCase

import frappe

from ask_alyf.ask_alyf import tools



class IntegrationTestReports(IntegrationTestCase):
	def test_report_builder_rejects_unknown_filter(self):
		"""Report Builder must reject unknown filters, even when the user can read the report rows."""
		report = frappe.get_doc(
			{
				"doctype": "Report",
				"report_name": "Test Report Builder Unknown Filter",
				"ref_doctype": "User",
				"report_type": "Report Builder",
				"is_standard": "No",
				"json": '{"filters":[],"columns":[["name","User"]]}',
			}
		).insert()
		meta = tools.get_report_filters(report.name)

		with self.assertRaisesRegex(frappe.ValidationError, "Invalid filter: unknown"):
			tools._execute_report(report.name, {"unknown": "x"}, meta, are_default_filters=False)

	def test_report_builder_rejects_link_the_user_cannot_read(self):
		"""The user can read this ToDo, and its Link points at Administrator.
		get_list would return that row. Report Builder must still reject the filter,
		because this user can neither read nor select that User.
		"""
		frappe.get_doc({"doctype": "Role", "role_name": "Report Link Demo", "desk_access": 0}).insert()
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "report-link-demo@example.com",
				"first_name": "Report Link Demo",
				"send_welcome_email": 0,
				"roles": [{"role": "Report Link Demo"}],
			}
		).insert()
		frappe.permissions.add_permission("Report", "Report Link Demo", ptype="read")
		frappe.clear_cache()

		report = frappe.get_doc(
			{
				"doctype": "Report",
				"report_name": "Test Report Builder Link Filter",
				"ref_doctype": "ToDo",
				"report_type": "Report Builder",
				"is_standard": "No",
				"json": '{"filters":[],"columns":[["name","ToDo"],["allocated_to","ToDo"]]}',
				"filters": [
					{
						"label": "Allocated To",
						"fieldname": "allocated_to",
						"fieldtype": "Link",
						"options": "User",
					}
				],
			}
		).insert()
		todo = frappe.get_doc(
			{"doctype": "ToDo", "description": "link filter demo", "allocated_to": "Administrator"}
		).insert()
		frappe.db.set_value("ToDo", todo.name, "owner", user.name)

		meta = tools.get_report_filters(report.name)
		frappe.set_user(user.name)
		try:
			visible = frappe.get_list("ToDo", filters={"name": todo.name}, fields=["name", "allocated_to"])
			self.assertEqual([row.allocated_to for row in visible], ["Administrator"])
			with self.assertRaisesRegex(
				frappe.ValidationError,
				"You do not have permission to access User: Administrator",
			):
				tools._execute_report(
					report.name,
					{"allocated_to": "Administrator"},
					meta,
					are_default_filters=False,
				)
		finally:
			frappe.set_user("Administrator")

	def test_script_filter_replaces_extended_filter(self):
		base = """
		let filters = [
			{ fieldname: "company", label: "From Extend", fieldtype: "Data" },
			{ fieldname: "period", label: "Period", fieldtype: "Data", default: "Monthly" },
		];
		"""
		script = """
		frappe.query_reports["Demo"] = $.extend({}, erpnext.demo);
		frappe.query_reports["Demo"]["filters"].push(
			{ fieldname: "company", label: "From Script", fieldtype: "Data", reqd: 1 },
			{ fieldname: "extra", label: "Extra", fieldtype: "Check" }
		);
		"""
		with patch(
			"ask_alyf.ask_alyf.extract_report_filters.extended_script",
			return_value=base,
		):
			from ask_alyf.ask_alyf.extract_report_filters import filters_from_js

			rows = filters_from_js(script)

		self.assertEqual(
			[(row["fieldname"], row["label"], row["reqd"]) for row in rows],
			[
				("company", "From Script", 1),
				("period", "Period", 0),
				("extra", "Extra", 0),
			],
		)

	def test_read_filter_objects_accepts_the_filter_forms(self):
		from ask_alyf.ask_alyf.extract_report_filters import read_filter_objects

		rows = read_filter_objects("""
		frappe.query_reports["Demo"] = {
			filters: [
				{ fieldname: "bare", label: __("Bare"), fieldtype: "Data", reqd: 1 },
				{ "fieldname": "quoted", "label": "Quoted", "fieldtype": "Check", "mandatory": true },
				{ fieldname: "select_text", label: "Select text", fieldtype: "Select", options: "Open\\nClosed", default: "Open" },
				{ fieldname: "select_list", label: "Select list", fieldtype: "Select", options: ["", "A", "B"], default: ["A"] },
				{ fieldname: "select_objects", label: "Select objects", fieldtype: "Select", options: [{value: "M", label: __("Monthly")}, {"value": "Q", label: "Quoted value"}] },
				{ fieldname: "number", label: "Number", fieldtype: "Int", default: -3 },
				{ fieldname: "depends", label: "Depends", fieldtype: "Date", depends_on: "eval:doc.bare", mandatory_depends_on: "eval:doc.bare" },
			],
		};
		let filters = [
			{ fieldname: "assigned", label: "Assigned", fieldtype: "Data" },
		];
		frappe.query_reports["Demo"]["filters"].push(
			{ fieldname: "pushed", label: "Pushed", fieldtype: "Check", default: 0 }
		);
		query_reports['Demo']['filters'].push({ fieldname: "single_quoted_push", label: "Single", fieldtype: "Data" });
		""")

		self.assertEqual(
			rows,
			[
				{"fieldname": "bare", "label": "Bare", "fieldtype": "Data", "reqd": 1},
				{"fieldname": "quoted", "label": "Quoted", "fieldtype": "Check", "reqd": 1},
				{
					"fieldname": "select_text",
					"label": "Select text",
					"fieldtype": "Select",
					"reqd": 0,
					"options": ["Open", "Closed"],
					"default": "Open",
				},
				{
					"fieldname": "select_list",
					"label": "Select list",
					"fieldtype": "Select",
					"reqd": 0,
					"options": ["", "A", "B"],
					"default": "A",
				},
				{
					"fieldname": "select_objects",
					"label": "Select objects",
					"fieldtype": "Select",
					"reqd": 0,
					"options": ["M", "Q"],
				},
				{"fieldname": "number", "label": "Number", "fieldtype": "Int", "reqd": 0, "default": -3},
				{
					"fieldname": "depends",
					"label": "Depends",
					"fieldtype": "Date",
					"reqd": 0,
					"depends_on": "eval:doc.bare",
					"mandatory_depends_on": "eval:doc.bare",
				},
				{"fieldname": "assigned", "label": "Assigned", "fieldtype": "Data", "reqd": 0},
				{"fieldname": "pushed", "label": "Pushed", "fieldtype": "Check", "reqd": 0, "default": 0},
				{"fieldname": "single_quoted_push", "label": "Single", "fieldtype": "Data", "reqd": 0},
			],
		)