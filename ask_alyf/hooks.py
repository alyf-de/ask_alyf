app_name = "ask_alyf"
app_title = "Ask ALYF"
app_publisher = "ALYF GmbH"
app_description = "Helps you understand ERPNext"
app_email = "hallo@alyf.de"
app_license = "gpl-3.0"
app_include_js = "ask_alyf.bundle.js"
app_include_css = "ask_alyf.bundle.css"
extend_bootinfo = "ask_alyf.ask_alyf.boot.boot_session"

after_install = ["ask_alyf.ask_alyf.install.ensure_erpnext_report_skill"]
after_migrate = ["ask_alyf.ask_alyf.install.ensure_erpnext_report_skill"]

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# Includes in <head>
# ------------------

# include js, css files in header of web template
# web_include_css = "/assets/ask_alyf/css/ask_alyf.css"
# web_include_js = "/assets/ask_alyf/js/ask_alyf.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "ask_alyf/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "ask_alyf/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "ask_alyf.utils.jinja_methods",
# 	"filters": "ask_alyf.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "ask_alyf.install.before_install"

# Uninstallation
# ------------

# before_uninstall = "ask_alyf.uninstall.before_uninstall"
# after_uninstall = "ask_alyf.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "ask_alyf.utils.before_app_install"
# after_app_install = "ask_alyf.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "ask_alyf.utils.before_app_uninstall"
# after_app_uninstall = "ask_alyf.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "ask_alyf.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

permission_query_conditions = {}

has_permission = {}

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"ask_alyf.tasks.all"
# 	],
# 	"daily": [
# 		"ask_alyf.tasks.daily"
# 	],
# 	"hourly": [
# 		"ask_alyf.tasks.hourly"
# 	],
# 	"weekly": [
# 		"ask_alyf.tasks.weekly"
# 	],
# 	"monthly": [
# 		"ask_alyf.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "ask_alyf.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "ask_alyf.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "ask_alyf.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "ask_alyf.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["ask_alyf.utils.before_request"]
# after_request = ["ask_alyf.utils.after_request"]

# Job Events
# ----------
# before_job = ["ask_alyf.utils.before_job"]
# after_job = ["ask_alyf.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"ask_alyf.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True

# Require all whitelisted methods to have type annotations
require_type_annotated_api_methods = True

default_log_clearing_doctypes = {
	"Ask ALYF Conversation": 90  # days to retain logs
}

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
ignore_translatable_strings_from = ["frappe"]
