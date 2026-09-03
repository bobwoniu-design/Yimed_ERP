import frappe
from frappe.model.rename_doc import get_link_fields, update_link_field_values


def execute():
	"""把既有 Ecommerce Fee Item 的中文名称改为顺序编号，配合 autoname 从 field:item_name 切换为 autoincrement。

	不能直接用 frappe.rename_doc：它对 autoincrement 名称会做 cint 强转，无法处理中文旧名。
	"""
	if not frappe.db.table_exists("Ecommerce Fee Item"):
		return

	names = [row["name"] for row in frappe.get_all("Ecommerce Fee Item", fields=["name"], order_by="name")]
	non_numeric = [name for name in names if not str(name).isdigit()]
	if not non_numeric:
		return

	used = {int(name) for name in names if str(name).isdigit()}
	next_num = 1
	while next_num in used:
		next_num += 1

	link_fields = get_link_fields("Ecommerce Fee Item")

	for old in non_numeric:
		new = str(next_num)
		frappe.db.sql("update `tabEcommerce Fee Item` set name = %s where name = %s", (new, old))
		update_link_field_values(link_fields, old, new, "Ecommerce Fee Item")
		next_num += 1
