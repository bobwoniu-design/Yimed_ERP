import frappe
from frappe import _
from frappe.model.document import Document


class ChannelContentQualityMetric(Document):
	def before_save(self):
		if not self.is_new() and not self.flags.get("allow_metric_update"):
			frappe.throw(_("质量指标不可直接修改，请执行重建"))

	def on_trash(self):
		if not self.flags.get("allow_metric_delete"):
			frappe.throw(_("质量指标不可手工删除"))
