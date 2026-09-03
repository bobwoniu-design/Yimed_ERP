import re

import frappe
from frappe import _
from frappe.utils import cint, getdate, nowdate


def run_expiry_reminders(today=None):
	"""Create idempotent Desk notifications for active content files approaching expiry."""
	settings = frappe.get_single("Channel Content Settings")
	if not cint(settings.enable_expiry_reminders):
		return {"checked": 0, "notifications": 0, "disabled": True}

	today = getdate(today or nowdate())
	reminder_days = _parse_reminder_days(settings.expiry_reminder_days)
	assets = frappe.get_all(
		"Channel Content Asset",
		filters={"status": "Active", "expires_on": ["is", "set"]},
		fields=["name", "title", "expires_on", "responsible_user"],
	)
	admin_recipients = _admin_recipients(settings)
	created = 0

	for asset in assets:
		days_remaining = (getdate(asset.expires_on) - today).days
		stage = _reminder_stage(days_remaining, reminder_days, cint(settings.remind_after_expiry))
		if not stage:
			continue
		recipients = set(admin_recipients)
		if cint(settings.notify_responsible_user) and asset.responsible_user:
			recipients.add(asset.responsible_user)
		for recipient in _enabled_users(recipients):
			created += _create_notification(asset, recipient, today, stage, days_remaining)

	return {"checked": len(assets), "notifications": created, "disabled": False}


def _parse_reminder_days(value):
	days = set()
	for part in (value or "").replace("，", ",").split(","):
		part = part.strip()
		if part:
			days.add(cint(part))
	return days


def _reminder_stage(days_remaining, reminder_days, remind_after_expiry):
	if days_remaining in reminder_days:
		return "due_today" if days_remaining == 0 else f"before_{days_remaining}_days"
	if days_remaining < 0 and remind_after_expiry:
		return "overdue"
	return None


def _admin_recipients(settings):
	recipients = set(filter(None, re.split(r"[,，\n;；]+", settings.expiry_admin_users or "")))
	if settings.expiry_admin_role:
		recipients.update(
			frappe.get_all(
				"Has Role",
				filters={"role": settings.expiry_admin_role, "parenttype": "User"},
				pluck="parent",
			)
		)
	return recipients


def _enabled_users(recipients):
	if not recipients:
		return []
	return frappe.get_all(
		"User",
		filters={"name": ["in", sorted(recipients)], "enabled": 1, "user_type": "System User"},
		pluck="name",
	)


def _create_notification(asset, recipient, reminder_date, stage, days_remaining):
	key = {
		"asset": asset.name,
		"recipient": recipient,
		"reminder_date": reminder_date,
		"stage": stage,
	}
	if frappe.db.exists("Channel Content Expiry Reminder Log", key):
		return 0

	if days_remaining > 0:
		title = _("内容文件将在 {0} 天后到期：{1}").format(days_remaining, asset.title)
	elif days_remaining == 0:
		title = _("内容文件今天到期：{0}").format(asset.title)
	else:
		title = _("内容文件已逾期 {0} 天：{1}").format(abs(days_remaining), asset.title)

	notification = frappe.get_doc(
		{
			"doctype": "Notification Log",
			"title": title,
			"description": _("到期日期：{0}。请检查文件有效性，并更新到期日期或处理该文件。").format(
				asset.expires_on
			),
			"document_type": "Channel Content Asset",
			"document_name": asset.name,
			"for_user": recipient,
			"from_user": "Administrator",
			"app": "channel_erp",
		}
	).insert(ignore_permissions=True)
	try:
		frappe.get_doc(
			{
				"doctype": "Channel Content Expiry Reminder Log",
				**key,
				"notification_log": notification.name,
			}
		).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		notification.delete(ignore_permissions=True)
		return 0
	return 1
