import frappe
from frappe import _


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.import_batch:
		frappe.throw(_("Import Batch is required."))
	conditions = ["po.import_batch = %(import_batch)s"]
	if filters.destination_city:
		conditions.append("po.destination_city = %(destination_city)s")
	data = frappe.db.sql(
		f"""
		select po.name as purchase_order, po.destination_city, po.distribution_center,
		       ci.jd_sku, ci.platform_item, ci.stock_item,
		       item.item_name as stock_item_name, ci.batch_no,
		       sum(ci.qty) as stock_qty, ci.uom,
		       count(distinct c.name) as carton_count
		from `tabJD Carton Item` ci
		inner join `tabJD Carton` c on c.name = ci.parent
		inner join `tabJD Purchase Order` po on po.name = c.purchase_order
		left join `tabItem` item on item.name = ci.stock_item
		where {' and '.join(conditions)}
		  and ci.parenttype = 'JD Carton'
		  and ci.parentfield = 'components'
		group by po.name, po.destination_city, po.distribution_center,
		         ci.jd_sku, ci.platform_item, ci.stock_item, item.item_name,
		         ci.batch_no, ci.uom
		order by po.destination_city, po.name, ci.jd_sku, ci.stock_item, ci.batch_no
		""",
		filters,
		as_dict=True,
	)
	message = None
	if not data:
		message = _("本批次尚未生成装箱明细。请先完成装箱并确认箱数，再查看分仓汇总。")
	return _columns(), data, message


def _columns():
	return [
		{"fieldname": "purchase_order", "label": _("京东采购单"), "fieldtype": "Link", "options": "JD Purchase Order", "width": 150},
		{"fieldname": "destination_city", "label": _("目的城市"), "fieldtype": "Data", "width": 90},
		{"fieldname": "distribution_center", "label": _("配送中心"), "fieldtype": "Data", "width": 110},
		{"fieldname": "jd_sku", "label": _("京东SKU"), "fieldtype": "Data", "width": 130},
		{"fieldname": "platform_item", "label": _("平台商品编码"), "fieldtype": "Link", "options": "Item", "width": 140},
		{"fieldname": "stock_item", "label": _("实际备货物料"), "fieldtype": "Link", "options": "Item", "width": 150},
		{"fieldname": "stock_item_name", "label": _("物料名称"), "fieldtype": "Data", "width": 180},
		{"fieldname": "batch_no", "label": _("批号"), "fieldtype": "Link", "options": "Batch", "width": 120},
		{"fieldname": "stock_qty", "label": _("发货数量"), "fieldtype": "Float", "width": 90},
		{"fieldname": "uom", "label": _("单位"), "fieldtype": "Link", "options": "UOM", "width": 70},
		{"fieldname": "carton_count", "label": _("涉及箱数"), "fieldtype": "Int", "width": 80},
	]
