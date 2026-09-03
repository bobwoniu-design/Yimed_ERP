import hashlib

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, getdate, now_datetime, nowdate


def _enabled_users(users):
	users = sorted(set(filter(None, users)))
	if not users:
		return []
	return frappe.get_all(
		"User", filters={"name": ["in", users], "enabled": 1, "user_type": "System User"}, pluck="name"
	)


def _reviewers():
	users = set()
	for role in ("Content Center Manager", "System Manager"):
		users.update(frappe.get_all("Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent"))
	users.add("Administrator")
	return _enabled_users(users)


def _notify(reminder_type, reference_doctype, reference_name, company, recipient, event_date, title, description):
	raw_key = f"{reminder_type}|{reference_doctype}|{reference_name}|{recipient}|{event_date}"
	key = hashlib.sha256(raw_key.encode()).hexdigest()
	if frappe.db.exists("Channel Content Workflow Reminder Log", {"reminder_key": key}):
		return 0
	notification = frappe.get_doc({
		"doctype": "Notification Log", "title": title, "description": description,
		"document_type": reference_doctype, "document_name": reference_name,
		"for_user": recipient, "from_user": "Administrator", "app": "channel_erp",
	}).insert(ignore_permissions=True)
	try:
		frappe.get_doc({
			"doctype": "Channel Content Workflow Reminder Log", "reminder_key": key,
			"reminder_type": reminder_type, "reference_doctype": reference_doctype,
			"reference_name": reference_name, "company": company, "recipient": recipient,
			"event_date": event_date, "notification_log": notification.name, "sent_at": now_datetime(),
		}).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		notification.delete(ignore_permissions=True)
		return 0
	return 1


def run_workflow_reminders(today=None):
	settings = frappe.get_single("Channel Content Settings")
	approval_enabled = bool(cint(getattr(settings, "enable_approval_workflow", 0)))
	borrowing_enabled = bool(cint(getattr(settings, "enable_borrowing", 0)))
	if not cint(settings.enable_workflow_reminders) or not (approval_enabled or borrowing_enabled):
		return {"pending_review": 0, "borrow_expiry": 0, "disabled": True}
	today = getdate(today or nowdate())
	pending_days = max(cint(settings.pending_review_reminder_days or 3), 1)
	borrow_days = max(cint(settings.borrow_expiry_reminder_days or 3), 1)
	pending_created = 0
	borrow_created = 0
	reviewers = _reviewers() if approval_enabled else []
	pending_assets = frappe.get_all(
		"Channel Content Asset",
		filters={"status": "Active", "publication_status": "Pending", "submitted_at": ["is", "set"]},
		fields=["name", "title", "company", "folder", "submitted_at"], limit=0,
	) if approval_enabled else []
	for asset in pending_assets:
		age = (today - getdate(asset.submitted_at)).days
		if age < pending_days:
			continue
		for recipient in reviewers:
			if recipient != "Administrator" and not frappe.has_permission(
				"Company", "read", doc=asset.company, user=recipient
			):
				continue
			pending_created += _notify(
				"pending-review-overdue", "Channel Content Asset", asset.name, asset.company,
				recipient, getdate(asset.submitted_at),
				_("内容文件审批已等待 {0} 天：{1}").format(age, asset.title),
				_("请进入内容中心完成批准或驳回。"),
			)
	active_grants = frappe.get_all(
		"Channel Content Access Grant", filters={"status": "Active"},
		fields=["name", "asset", "company", "grantee", "valid_until"], limit=0,
	) if borrowing_enabled else []
	for grant in active_grants:
		if get_datetime(grant.valid_until) <= now_datetime():
			continue
		days_remaining = (getdate(grant.valid_until) - today).days
		if days_remaining < 0 or days_remaining > borrow_days:
			continue
		if not frappe.db.exists("Channel Content Asset", {"name": grant.asset, "status": "Active"}):
			continue
		for recipient in _enabled_users([grant.grantee]):
			borrow_created += _notify(
				"borrow-expiring", "Channel Content Access Grant", grant.name, grant.company,
				recipient, getdate(grant.valid_until),
				_("内容借阅将在 {0} 天后到期").format(days_remaining),
				_("借阅文件：{0}。到期后将无法继续访问，请及时处理。" ).format(grant.asset),
			)
	return {"pending_review": pending_created, "borrow_expiry": borrow_created, "disabled": False}
