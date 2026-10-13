import frappe


def ensure_erpnext_report_skill():
	"""Install the ERPNext report skill once. Skip when ERPNext is absent or the skill was edited."""
	if "erpnext" not in frappe.get_installed_apps():
		return
	if frappe.db.exists("Ask ALYF Skill", REPORT_SKILL_NAME):
		return

	frappe.get_doc(
		{
			"doctype": "Ask ALYF Skill",
			"title": "Running ERPNext reports",
			"description": REPORT_SKILL_DESCRIPTION,
			"roles": [{"role": "Ask ALYF User"}],
		}
	).insert(ignore_permissions=True, set_name=REPORT_SKILL_NAME)


REPORT_SKILL_NAME = "erpnext-report-filters"

REPORT_SKILL_DESCRIPTION = """Read this before `run_report` for an ERPNext report.

## Every report

- Call `get_report_filters` first. Pass only those fieldnames.
- Pass every filter with `reqd` 1. Use its `default` unless the user named a value.
- Dates are `YYYY-MM-DD`. Select and Link values are strings. MultiSelectList values are lists of strings.
- Pass a Check only when its `default` is 1, and then pass 1. Leave out a Check with no default or default 0, unless the user asked for that option.
- Leave out optional Link, Select, and MultiSelectList filters the user did not name.
- For "today", use the date `default` from `get_report_filters`. Do not query the database for the current date.
- If `run_report` returns `error`, tell the user that message and stop. Do not open other documents or the source-code analyzer.

## Reports

Profit and Loss Statement, Balance Sheet, Cash Flow:

- `company`
- `filter_based_on`: `Fiscal Year`, or `Date Range` when the user gave dates
- Fiscal Year: `from_fiscal_year` and `to_fiscal_year`
- Date Range: `period_start_date` and `period_end_date`
- `periodicity`: `Yearly`, or `Monthly` / `Quarterly` / `Half-Yearly` when the user asked
- Profit and Loss Statement and Balance Sheet: `selected_view` `Report`, unless the user asked for Growth or Margin
- Balance Sheet: pass `accumulated_values` as 1 and `include_default_book_entries` as 1
- Profit and Loss Statement: pass `include_default_book_entries` as 1. Leave out `accumulated_values` unless the user asked for accumulated values
- Cash Flow: pass `include_default_book_entries` as 1. Never pass `show_opening_and_closing_balance`. It has no default, and 1 throws when the Fiscal Year has no row for the company

Trial Balance: `company`, `fiscal_year`, `from_date`, `to_date`. Pass `with_period_closing_entry_for_opening`, `with_period_closing_entry_for_current_period`, `include_default_book_entries`, `show_net_values`, and `show_group_accounts` as 1. Leave out `show_zero_values` and `show_unclosed_fy_pl_balances`.

General Ledger: `company`, `from_date`, `to_date`. Add `account`, `party`, or `voucher_no` only when the user named them. Pass `include_default_book_entries` as 1. Pass `include_dimensions` only when its default is 1. Leave out the other checks.

Accounts Receivable, Accounts Payable, Accounts Receivable Summary, Accounts Payable Summary: `company` and `report_date`. Add `party` only when named. Leave out every Check.

Customer Ledger Summary, Supplier Ledger Summary: `company`, `from_date`, `to_date`. Add `party` only when named. Leave out every Check.

Stock Balance, Stock Ledger: `company`, `from_date`, `to_date`. Add `item_code` or `warehouse` only when named. On Stock Balance, pass `include_zero_stock_items` as 1. Leave out the other checks.

Gross Profit: `company`, `from_date`, `to_date`. Pass `include_returned_invoices` as 1.

Sales Analytics: pass the required filters with their defaults (`tree_type`, `doc_type`, `value_quantity`, `from_date`, `to_date`, `company`, `range`, `curves`). Leave out the subsidiary-company check.

Purchase Analytics: pass the required filters with their defaults (`tree_type`, `doc_type`, `value_quantity`, `from_date`, `to_date`, `company`, `range`). Leave out the subsidiary-company check.

Sales Register, Purchase Register: `company`, `from_date`, `to_date`. Add `customer` or `supplier` only when named. Leave out `include_payments`."""
