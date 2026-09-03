app_name = "channel_erp"
app_title = "Channel Erp"
app_publisher = "Yimed"
app_description = "Channel e-commerce OMS: content center, JD consignment stocking, inventory snapshot import, promotion fee import, JackYun integration"
app_email = "admin@yimed.com"
app_license = "mit"

before_migrate = "channel_erp.setup.before_migrate"
after_migrate = "channel_erp.setup.after_migrate"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "channel_erp",
# 		"logo": "/assets/channel_erp/logo.png",
# 		"title": "Channel Erp",
# 		"route": "/channel_erp",
# 		"has_permission": "channel_erp.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/channel_erp/css/channel_erp.css"
# app_include_js = "/assets/channel_erp/js/channel_erp.js"

# include js, css files in header of web template
# web_include_css = "/assets/channel_erp/css/channel_erp.css"
# web_include_js = "/assets/channel_erp/js/channel_erp.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "channel_erp/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
doctype_js = {
	"Batch": "public/js/batch_inspection_report.js",
	"Delivery Note": "public/js/delivery_note_inspection_report.js",
	"Sales Order": "public/js/sales_order_connector.js",
}
doctype_list_js = {"Batch": "public/js/batch_inspection_report_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "channel_erp/public/icons.svg"

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
# 	"methods": "channel_erp.utils.jinja_methods",
# 	"filters": "channel_erp.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "channel_erp.install.before_install"
# after_install = "channel_erp.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "channel_erp.uninstall.before_uninstall"
# after_uninstall = "channel_erp.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "channel_erp.utils.before_app_install"
# after_app_install = "channel_erp.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "channel_erp.utils.before_app_uninstall"
# after_app_uninstall = "channel_erp.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "channel_erp.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "channel_erp.notifications.get_notification_config"

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

doc_events = {
	"Sales Order": {
		"before_submit": "channel_erp.integrations.outbound_events.before_sales_order_submit",
		"on_submit": "channel_erp.integrations.outbound_events.on_sales_order_submit",
		"on_update_after_submit": "channel_erp.integrations.outbound_events.on_sales_order_update_after_submit",
		"on_cancel": "channel_erp.integrations.outbound_events.on_sales_order_cancel",
		"after_delete": "channel_erp.integrations.outbound_events.preserve_sales_order_series_after_delete",
	},
	"Delivery Note": {
		"before_submit": "channel_erp.integrations.jackyun.prepare_standalone_jackyun_return",
	},
	"Purchase Receipt": {
		"before_submit": "channel_erp.integrations.jackyun.prepare_standalone_jackyun_return",
	},
	"File": {
		"on_trash": "channel_erp.file_events.protect_content_asset_file",
		"after_delete": "channel_erp.file_events.handle_attachment_deleted",
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"cron": {
		"* * * * *": [
			"channel_erp.integrations.tasks.process_due_schedules",
			"channel_erp.integrations.connector_operations.process_outbound_queue",
        ],
		"*/15 * * * *": [
            "channel_erp.integrations.tasks.monitor_connector_health",
			"channel_erp.integrations.outbound_events.enqueue_test_order_cleanup",
        ],
        "*/30 * * * *": [
            "channel_erp.integrations.aftersale_link.sync_aftersale_refunds",
            "channel_erp.integrations.aftersale_link.sync_refund_orders",
        ],
		"30 2 * * *": [
			"channel_erp.content_center.run_content_center_maintenance",
		],
		"15 3 * * *": [
			"channel_erp.jackyun_integration.doctype.jackyun_raw_record.jackyun_raw_record.scheduled_cleanup_duplicate_snapshots",
		],
		"0 7 * * *": [
			"channel_erp.integrations.connector_operations.daily_consistency_report",
		],
	}
}

# Testing
# -------

# before_tests = "channel_erp.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "channel_erp.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "channel_erp.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "channel_erp.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

ignore_links_on_delete = ["Connector Outbound Message"]

# Request Events
# ----------------
# before_request = ["channel_erp.utils.before_request"]
# after_request = ["channel_erp.utils.after_request"]

# Job Events
# ----------
# before_job = ["channel_erp.utils.before_job"]
# after_job = ["channel_erp.utils.after_job"]

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
# 	"channel_erp.auth.validate"
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
