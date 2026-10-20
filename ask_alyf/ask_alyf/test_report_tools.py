import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

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

	@unittest.skipUnless("erpnext" in frappe.get_installed_apps(), "erpnext not installed")
	def test_erpnext_balance_sheet_report(self):
		"""Test ERPNext Balance Sheet Filters from balance_sheet.js.

		- import filters from $.extend({}, erpnext.financial_statements);
		- If Company, Fiscal Year and Date filters are existing and defaulted to the correct values
		- If all other filters are present and correct
		- If Report runs (but empty)
		"""
		from erpnext.accounts.utils import get_fiscal_year

		# needed for creating a company
		if not frappe.db.exists("Warehouse Type", "Transit"):
			frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(ignore_permissions=True)

		# create test company
		company = "Ask ALYF Report Co"
		if not frappe.db.exists("Company", company):
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": company,
					"abbr": "AARC",
					"default_currency": "EUR",
					"country": "Germany",
					"create_chart_of_accounts_based_on": "Standard Template",
				}
			).insert(ignore_permissions=True)

		global_defaults = frappe.get_doc("Global Defaults")
		global_defaults.default_company = company
		global_defaults.save(ignore_permissions=True)
		frappe.defaults.set_user_default("company", company)

		today = frappe.utils.today()
		if not get_fiscal_year(today, raise_on_missing=False):
			year = frappe.utils.getdate(today).year
			frappe.get_doc(
				{
					"doctype": "Fiscal Year",
					"year": str(year),
					"year_start_date": f"{year}-01-01",
					"year_end_date": f"{year}-12-31",
				}
			).insert(ignore_permissions=True)

		fiscal_year = get_fiscal_year(today, raise_on_missing=False)[0]
		balance_sheet_meta = tools.get_report_filters("Balance Sheet")
		balance_sheet_fields = {row["fieldname"]: row for row in balance_sheet_meta["filters"]}

		self.assertEqual(balance_sheet_meta["report_type"], "Script Report")
		for fieldname in (
			"company",
			"finance_book",
			"filter_based_on",
			"period_start_date",
			"period_end_date",
			"from_fiscal_year",
			"to_fiscal_year",
			"periodicity",
			"presentation_currency",
			"cost_center",
			"project",
			"group_by_dimension",
			"report_template",
			"show_account_details",
			"accumulated_values",
			"include_default_book_entries",
			"show_zero_values",
		):
			self.assertIn(fieldname, balance_sheet_fields)

		# check if defaults for ERPNext values are correctly set.
		self.assertEqual(balance_sheet_fields["company"]["default"], company)
		self.assertEqual(balance_sheet_fields["filter_based_on"]["default"], "Fiscal Year")
		self.assertEqual(balance_sheet_fields["from_fiscal_year"]["default"], fiscal_year)
		self.assertEqual(balance_sheet_fields["periodicity"]["default"], "Yearly")
		self.assertEqual(balance_sheet_fields["include_default_book_entries"]["default"], 1)

		balance_sheet = tools.run_report("Balance Sheet")
		year_end = frappe.utils.getdate(frappe.db.get_value("Fiscal Year", fiscal_year, "year_end_date"))
		period_key = year_end.strftime("%b_%Y").lower()

		self.assertEqual(balance_sheet["result"], [])
		self.assertEqual(
			[column["fieldname"] for column in balance_sheet["columns"]],
			["account", "acc_name", "acc_number", "currency", period_key],
		)
		self.assertEqual(balance_sheet["columns"][-1]["label"], fiscal_year)
		self.assertEqual(balance_sheet["chart"]["data"], {"labels": [fiscal_year], "datasets": []})
		self.assertEqual(balance_sheet["chart"]["currency"], "EUR")
		self.assertEqual(
			[(row["label"], row["value"]) for row in balance_sheet["report_summary"]],
			[
				("Total Asset", 0.0),
				("Total Liability", 0.0),
				("Total Equity", 0.0),
				("Provisional Profit / Loss (Credit)", 0.0),
			],
		)
