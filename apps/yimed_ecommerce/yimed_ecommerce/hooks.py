app_name = "yimed_ecommerce"
app_title = "Yimed Ecommerce"
app_publisher = "Yimed"
app_description = "Yimed channel e-commerce operations for JD procurement, packing, handover and stock transfer"
app_email = "admin@yimed.com"
app_license = "mit"

required_apps = ["erpnext"]

after_install = "yimed_ecommerce.setup.after_install"
after_migrate = "yimed_ecommerce.setup.after_migrate"

scheduler_events = {
	"daily": ["yimed_ecommerce.ecommerce_dashboard.fee_management.generate_daily_accruals"],
}

doc_events = {
	"Stock Entry": {
		"on_submit": "yimed_ecommerce.jd.stock_entry_events.on_submit",
		"on_cancel": "yimed_ecommerce.jd.stock_entry_events.on_cancel",
		"on_trash": "yimed_ecommerce.jd.stock_entry_events.on_trash",
	}
}

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "yimed_ecommerce",
# 		"logo": "/assets/yimed_ecommerce/logo.png",
# 		"title": "Yimed Ecommerce",
# 		"route": "/yimed_ecommerce",
# 		"has_permission": "yimed_ecommerce.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/yimed_ecommerce/css/yimed_ecommerce.css"
app_include_js = "/assets/yimed_ecommerce/js/jd_sidebar_routes.js"

# include js, css files in header of web template
# web_include_css = "/assets/yimed_ecommerce/css/yimed_ecommerce.css"
# web_include_js = "/assets/yimed_ecommerce/js/yimed_ecommerce.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "yimed_ecommerce/public/scss/website"

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
# app_include_icons = "yimed_ecommerce/public/icons.svg"

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
# 	"methods": "yimed_ecommerce.utils.jinja_methods",
# 	"filters": "yimed_ecommerce.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "yimed_ecommerce.install.before_install"
# after_install = "yimed_ecommerce.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "yimed_ecommerce.uninstall.before_uninstall"
# after_uninstall = "yimed_ecommerce.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "yimed_ecommerce.utils.before_app_install"
# after_app_install = "yimed_ecommerce.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "yimed_ecommerce.utils.before_app_uninstall"
# after_app_uninstall = "yimed_ecommerce.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "yimed_ecommerce.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "yimed_ecommerce.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

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
# 		"yimed_ecommerce.tasks.all"
# 	],
# 	"daily": [
# 		"yimed_ecommerce.tasks.daily"
# 	],
# 	"hourly": [
# 		"yimed_ecommerce.tasks.hourly"
# 	],
# 	"weekly": [
# 		"yimed_ecommerce.tasks.weekly"
# 	],
# 	"monthly": [
# 		"yimed_ecommerce.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "yimed_ecommerce.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "yimed_ecommerce.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "yimed_ecommerce.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "yimed_ecommerce.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["yimed_ecommerce.utils.before_request"]
# after_request = ["yimed_ecommerce.utils.after_request"]

# Job Events
# ----------
# before_job = ["yimed_ecommerce.utils.before_job"]
# after_job = ["yimed_ecommerce.utils.after_job"]

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
# 	"yimed_ecommerce.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
