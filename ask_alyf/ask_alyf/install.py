import json
from pathlib import Path

import frappe

SKILL_NAME = "erpnext-report-filters"


def ensure_erpnext_report_skill():
	"""Install the ERPNext report skill once. Skip when ERPNext is absent or the skill was edited."""
	if "erpnext" not in frappe.get_installed_apps():
		return
	if frappe.db.exists("Ask ALYF Skill", SKILL_NAME):
		return

	fixture = Path(__file__).resolve().parents[1] / "fixtures" / "erpnext_report_filters.json"
	doc = frappe.get_doc(json.loads(fixture.read_text(encoding="utf-8")))
	doc.insert(ignore_permissions=True, set_name=SKILL_NAME)
