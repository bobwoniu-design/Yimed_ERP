from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt

from yimed_ecommerce.jd.product_bundle import expand_platform_item, is_product_bundle


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.import_batch:
		frappe.throw(_("Import Batch is required."))

	rows = _get_order_rows(filters)
	aggregated = defaultdict(float)
	logical_quantities = defaultdict(float)
	meta = {}
	for row in rows:
		if flt(row.purchase_qty) <= 0:
			continue
		logical_key = (row.jd_sku, row.platform_item)
		logical_quantities[logical_key] += flt(row.purchase_qty)
		if not row.platform_item:
			key = (row.jd_sku, None, None, None)
			aggregated[key] = 0
			meta[key] = (row.jd_item_name, row.purchase_uom, False, _("未映射"))
			continue

		components = expand_platform_item(row.platform_item, row.purchase_qty)
		bundle = is_product_bundle(row.platform_item)
		for component in components:
			key = (row.jd_sku, row.platform_item, component.stock_item, component.stock_uom)
			aggregated[key] += flt(component.qty)
			meta[key] = (row.jd_item_name, row.purchase_uom, bundle, _("已映射"))

	data = []
	for key, component_qty in sorted(
		aggregated.items(), key=lambda entry: tuple(value or "" for value in entry[0])
	):
		jd_sku, platform_item, stock_item, stock_uom = key
		jd_name, purchase_uom, bundle, mapping_status = meta[key]
		data.append(
			{
				"jd_sku": jd_sku,
				"jd_item_name": jd_name,
				"platform_item": platform_item,
				"mapping_status": mapping_status,
				"purchase_qty": logical_quantities[(jd_sku, platform_item)],
				"purchase_uom": purchase_uom,
				"is_bundle": (_("Yes") if bundle else _("No")) if platform_item else "",
				"stock_item": stock_item,
				"stock_item_name": (
					frappe.get_cached_value("Item", stock_item, "item_name") if stock_item else None
				),
				"stock_qty": component_qty,
				"stock_uom": stock_uom,
			}
		)
	return _columns(), data


def _get_order_rows(filters):
	conditions = ["po.import_batch = %(import_batch)s"]
	if filters.purchase_order:
		conditions.append("po.name = %(purchase_order)s")
	return frappe.db.sql(
		f"""
		select poi.jd_sku, poi.jd_item_name, poi.platform_item,
		       poi.purchase_qty, poi.purchase_uom
		from `tabJD Purchase Order Item` poi
		inner join `tabJD Purchase Order` po on po.name = poi.parent
		where {' and '.join(conditions)}
		  and poi.parenttype = 'JD Purchase Order'
		order by poi.jd_sku, poi.idx
		""",
		filters,
		as_dict=True,
	)


def _columns():
	return [
		{"fieldname": "jd_sku", "label": _("京东SKU"), "fieldtype": "Data", "width": 130},
		{"fieldname": "jd_item_name", "label": _("京东商品名称"), "fieldtype": "Data", "width": 220},
		{"fieldname": "platform_item", "label": _("平台商品编码"), "fieldtype": "Link", "options": "Item", "width": 150},
		{"fieldname": "mapping_status", "label": _("映射状态"), "fieldtype": "Data", "width": 75},
		{"fieldname": "purchase_qty", "label": _("采购数量"), "fieldtype": "Float", "width": 90},
		{"fieldname": "purchase_uom", "label": _("采购单位"), "fieldtype": "Link", "options": "UOM", "width": 80},
		{"fieldname": "is_bundle", "label": _("组合装"), "fieldtype": "Data", "width": 70},
		{"fieldname": "stock_item", "label": _("备货物料"), "fieldtype": "Link", "options": "Item", "width": 150},
		{"fieldname": "stock_item_name", "label": _("备货物料名称"), "fieldtype": "Data", "width": 200},
		{"fieldname": "stock_qty", "label": _("备货数量"), "fieldtype": "Float", "width": 90},
		{"fieldname": "stock_uom", "label": _("库存单位"), "fieldtype": "Link", "options": "UOM", "width": 80},
	]
