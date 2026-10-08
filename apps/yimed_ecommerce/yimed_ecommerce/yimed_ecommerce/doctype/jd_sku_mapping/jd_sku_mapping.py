import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt
from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file

from yimed_ecommerce.jd.product_bundle import is_supported_platform_item


class JDSKUMapping(Document):
	def validate(self):
		if not is_supported_platform_item(self.platform_item):
			frappe.throw(
				_("Platform Item {0} must be a stock Item or an enabled Product Bundle.").format(
					frappe.bold(self.platform_item)
				)
			)

	def on_update(self):
		# 手动新建或修改单条映射后，自动刷新受影响的采购单行（带出 ERP 编码），
		# 与批量导入文件时的行为保持一致。
		if self.jd_sku and not self.flags.in_patch:
			_refresh_purchase_orders([self.jd_sku])
			frappe.db.commit()


@frappe.whitelist()
def import_mapping_file(file_url: str):
	"""Import or update JD mappings from the operational price/mapping workbook."""
	frappe.has_permission("JD SKU Mapping", "write", throw=True)
	rows = read_xlsx_file_from_attached_file(file_url=file_url, read_only=True)
	if not rows:
		frappe.throw(_("The uploaded workbook is empty."))
	headers = [str(value or "").strip() for value in rows[0]]
	jd_column = _find_header(headers, ("京东SKU编码", "京东SKU", "商品编号", "SKU"))
	item_column = _find_header(headers, ("平台商品编码", "ERP编码", "链接主SKU"))
	optional_columns = {
		"brand": _optional_header(headers, ("品牌",)),
		"third_level_category": _optional_header(headers, ("三级类目",)),
		"erp_name": _optional_header(headers, ("ERP名称", "平台商品名称")),
		"labeling_required": _optional_header(headers, ("是否贴码",)),
		"purchase_price": _optional_header(headers, ("采购价",)),
		"net_price": _optional_header(headers, ("到手价",)),
		"cost_price": _optional_header(headers, ("成本价",)),
		"is_medical_device": _optional_header(headers, ("是否器械",)),
		"remarks": _optional_header(headers, ("备注",)),
	}

	created = updated = 0
	errors = []
	imported_skus = set()
	for index, values in enumerate(rows[1:], start=2):
		if not any(value not in (None, "") for value in values):
			continue
		jd_sku = _identifier(values[jd_column] if jd_column < len(values) else None)
		platform_item = _identifier(values[item_column] if item_column < len(values) else None)
		if not jd_sku or not platform_item:
			errors.append(_("Row {0}: JD SKU and Platform Item are required.").format(index))
			continue
		try:
			mapping_values = {
				"platform_item": platform_item,
				"brand": _value(values, optional_columns["brand"]),
				"third_level_category": _value(values, optional_columns["third_level_category"]),
				"erp_name": _value(values, optional_columns["erp_name"]),
				"labeling_required": _yes_no(_value(values, optional_columns["labeling_required"])),
				"purchase_price": flt(_value(values, optional_columns["purchase_price"])),
				"net_price": flt(_value(values, optional_columns["net_price"])),
				"cost_price": flt(_value(values, optional_columns["cost_price"])),
				"is_medical_device": _yes_no(_value(values, optional_columns["is_medical_device"])),
				"remarks": _value(values, optional_columns["remarks"]),
				"disabled": 0,
			}
			if frappe.db.exists("JD SKU Mapping", jd_sku):
				doc = frappe.get_doc("JD SKU Mapping", jd_sku)
				doc.update(mapping_values)
				doc.save()
				updated += 1
			else:
				frappe.get_doc({"doctype": "JD SKU Mapping", "jd_sku": jd_sku, **mapping_values}).insert()
				created += 1
			imported_skus.add(jd_sku)
		except Exception as exc:
			errors.append(_("Row {0}: {1}").format(index, str(exc)))
	refreshed_orders = _refresh_purchase_orders(imported_skus)
	return {
		"created": created,
		"updated": updated,
		"failed": len(errors),
		"errors": errors,
		"purchase_orders_refreshed": refreshed_orders,
	}


def _find_header(headers, aliases):
	for alias in aliases:
		if alias in headers:
			return headers.index(alias)
	frappe.throw(_("Missing column. Accepted headers: {0}").format(", ".join(aliases)))


def _optional_header(headers, aliases):
	for alias in aliases:
		if alias in headers:
			return headers.index(alias)
	return None


def _value(values, column):
	if column is None or column >= len(values):
		return ""
	return _identifier(values[column])


def _yes_no(value):
	value = _identifier(value)
	return value if value in {"是", "否"} else ""


def _refresh_purchase_orders(jd_skus):
	if not jd_skus:
		return 0
	parents = frappe.get_all(
		"JD Purchase Order Item",
		filters={"jd_sku": ["in", sorted(jd_skus)], "parenttype": "JD Purchase Order"},
		pluck="parent",
		distinct=True,
	)
	refreshed = 0
	for name in parents:
		try:
			po = frappe.get_doc("JD Purchase Order", name)
			po.save(ignore_permissions=True)
			po.db_set("status", "待备货" if po.mapping_status == "已映射" else "待映射")
			refreshed += 1
		except Exception:
			frappe.log_error(frappe.get_traceback(), _("Refresh JD Purchase Order mapping failed: {0}").format(name))
	return refreshed


def _identifier(value):
	if value in (None, ""):
		return ""
	if isinstance(value, float) and value.is_integer():
		return str(int(value))
	return str(value).strip()
