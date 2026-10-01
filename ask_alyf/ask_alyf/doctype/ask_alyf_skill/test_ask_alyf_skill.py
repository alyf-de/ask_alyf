# Copyright (c) 2026, ALYF GmbH and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class IntegrationTestAskALYFSkill(IntegrationTestCase):
	"""
	Integration tests for AskALYFSkill.
	Use this class for testing interactions between multiple components.
	"""

	def test_erpnext_report_skill_fixture(self):
		from ask_alyf.ask_alyf.install import SKILL_NAME, ensure_erpnext_report_skill

		frappe.delete_doc("Ask ALYF Skill", SKILL_NAME, force=True, ignore_permissions=True)
		ensure_erpnext_report_skill()

		doc = frappe.get_doc("Ask ALYF Skill", SKILL_NAME)
		self.assertEqual(doc.title, "Running ERPNext reports")
		self.assertIn("show_opening_and_closing_balance", doc.description)
		self.assertEqual([row.role for row in doc.roles], ["Ask ALYF User"])

		ensure_erpnext_report_skill()
		self.assertEqual(frappe.db.count("Ask ALYF Skill", {"name": SKILL_NAME}), 1)
