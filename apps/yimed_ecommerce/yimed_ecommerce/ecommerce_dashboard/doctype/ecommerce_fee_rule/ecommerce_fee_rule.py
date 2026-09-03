from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate


class EcommerceFeeRule(Document):
	def validate(self):
		from yimed_ecommerce.ecommerce_dashboard.promotion_import import validate_channel_store

		if self.valid_from and self.valid_to and getdate(self.valid_from) > getdate(self.valid_to):
			frappe.throw(_("Valid To cannot be earlier than Valid From."))
		if self.auto_accrual:
			if not self.fee_item:
				frappe.throw(_("Fee Item is required when automatic accrual is enabled."))
			if flt(self.daily_accrual_amount) < 0:
				frappe.throw(_("Daily accrual amount cannot be negative."))
		self._validate_scopes(validate_channel_store)

	def _validate_scopes(self, validate_channel_store):
		seen = set()
		channel_wide = set()
		for row in self.scopes:
			row.channel_name = frappe.utils.cstr(row.channel_name).strip()
			row.store_name = frappe.utils.cstr(row.store_name).strip()
			validate_channel_store(row.channel_name, row.store_name)
			key = (row.channel_name, row.store_name)
			if key in seen:
				frappe.throw(
					_("Scope row {0} duplicates an earlier channel/store selection.").format(row.idx)
				)
			seen.add(key)
			if not row.store_name:
				channel_wide.add(row.channel_name)

		for channel, store in seen:
			if store and channel in channel_wide:
				frappe.throw(
					_("Channel {0} already applies to all its stores; remove its individual store rows.").format(
						frappe.bold(channel)
					)
				)
