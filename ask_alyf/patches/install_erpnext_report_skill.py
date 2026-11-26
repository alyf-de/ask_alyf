from ask_alyf.ask_alyf.install import ensure_erpnext_report_skill


def execute():
	"""Give existing sites the ERPNext report skill. New sites get it from `after_install`."""
	ensure_erpnext_report_skill()
