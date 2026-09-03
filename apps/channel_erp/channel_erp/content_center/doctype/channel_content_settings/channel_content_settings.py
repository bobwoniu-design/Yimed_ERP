import frappe
from frappe import _
from frappe.model.document import Document


class ChannelContentSettings(Document):
	def validate(self):
		if (self.recycle_retention_days or 0) < 1:
			frappe.throw(_("回收站保留天数必须大于 0"))
		if (self.max_file_size_mb or 0) < 1:
			frappe.throw(_("单文件大小限制必须大于 0"))
		if not 0.5 <= float(self.insight_min_confidence or 0.8) <= 1:
			frappe.throw(_("洞察建议最低置信度必须在 0.5 到 1 之间"))
		if not 1 <= int(self.insight_keyword_limit or 12) <= 50:
			frappe.throw(_("关键词数量上限必须在 1 到 50 之间"))
		if not 1 <= int(self.insight_summary_sentences or 3) <= 10:
			frappe.throw(_("摘要句数上限必须在 1 到 10 之间"))
		if not 1 <= int(self.insight_suggestion_limit or 10) <= 50:
			frappe.throw(_("建议数量上限必须在 1 到 50 之间"))
		if not 1 <= int(self.search_event_retention_days or 90) <= 3650:
			frappe.throw(_("搜索事件保留天数必须在 1 到 3650 之间"))
		if not 1 <= int(self.content_stale_days or 180) <= 3650:
			frappe.throw(_("长期未使用天数必须在 1 到 3650 之间"))
		if not 1 <= int(self.pending_review_reminder_days or 3) <= 365:
			frappe.throw(_("待审批提醒天数必须在 1 到 365 之间"))
		if not 1 <= int(self.borrow_expiry_reminder_days or 3) <= 365:
			frappe.throw(_("借阅到期提醒天数必须在 1 到 365 之间"))
		self.expiry_reminder_days = _normalize_reminder_days(self.expiry_reminder_days)


def _normalize_reminder_days(value):
	parts = [part.strip() for part in (value or "").replace("，", ",").split(",") if part.strip()]
	try:
		days = sorted({int(part) for part in parts}, reverse=True)
	except ValueError:
		frappe.throw(_("提前提醒天数必须是以逗号分隔的整数"))
	if any(day < 0 for day in days):
		frappe.throw(_("提前提醒天数不能小于 0"))
	if any(day > 3650 for day in days):
		frappe.throw(_("提前提醒天数不能超过 3650 天"))
	return ",".join(str(day) for day in days)
