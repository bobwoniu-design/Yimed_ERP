import frappe
from frappe.model.document import Document


class ChannelContentAssetTag(Document):
	pass


def on_doctype_update():
	frappe.db.add_unique("Channel Content Asset Tag", ["asset", "normalized_tag"])
	frappe.db.add_index("Channel Content Asset Tag", ["normalized_tag", "asset"])
