import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import generate_hash

from yimed_ecommerce.jd.packing import (
	create_equal_cartons,
	generate_whole_carton_suggestions,
	get_packing_workbench,
)
from yimed_ecommerce.jd.test_product_bundle import make_item


class TestJDPackingWorkbench(IntegrationTestCase):
	def test_workbench_aggregates_duplicate_sku_and_partial_packing(self):
		po, jd_sku, item = _make_po("WORKBENCH", quantities=[4, 6], box_factor=4)
		create_equal_cartons(po.name, jd_sku, total_qty=3, qty_per_carton=3)

		data = get_packing_workbench(po.name)
		self.assertEqual(len(data["items"]), 1)
		row = data["items"][0]
		self.assertEqual(row["platform_item"], item)
		self.assertEqual(row["purchase_qty"], 10)
		self.assertEqual(row["packed_qty"], 3)
		self.assertEqual(row["remaining_qty"], 7)
		self.assertEqual(row["whole_carton_uom"], "Box")
		self.assertEqual(row["whole_carton_qty"], 4)
		self.assertEqual(row["suggested_full_cartons"], 1)
		self.assertEqual(row["suggested_remainder_qty"], 3)
		self.assertEqual(data["summary"]["sku_count"], 1)
		self.assertEqual(data["summary"]["carton_count"], 1)
		self.assertEqual(data["summary"]["remaining_qty"], 7)
		self.assertEqual(data["next_carton_sequence"], 2)
		self.assertEqual(data["cartons"][0]["allocations"][0]["jd_sku"], jd_sku)
		self.assertEqual(data["cartons"][0]["components"][0]["stock_item"], item)
		self.assertIn("expiry_date", data["cartons"][0]["components"][0])

	def test_whole_carton_generation_keeps_remainder_and_is_idempotent(self):
		po, jd_sku, _ = _make_po("REMAINDER", quantities=[10], box_factor=4)

		result = generate_whole_carton_suggestions(po.name)
		self.assertEqual(result["carton_count"], 2)
		self.assertEqual(result["items"][0]["qty_per_carton"], 4)
		self.assertEqual(result["items"][0]["remainder_qty"], 2)
		quantities = frappe.get_all(
			"JD Carton Allocation",
			filters={"parent": ["in", result["cartons"]], "jd_sku": jd_sku},
			pluck="qty",
		)
		self.assertEqual(quantities, [4, 4])

		second = generate_whole_carton_suggestions(po.name)
		self.assertEqual(second["carton_count"], 0)
		self.assertEqual(second["items"][0]["status"], "仅有零头")
		self.assertEqual(second["items"][0]["remainder_qty"], 2)
		self.assertEqual(frappe.db.count("JD Carton", {"purchase_order": po.name}), 2)

	def test_whole_carton_generation_handles_exact_division_and_purchase_uom_conversion(self):
		po, jd_sku, _ = _make_po(
			"PURCHASE-UOM",
			quantities=[10],
			box_factor=10,
			purchase_uom="Pack",
			purchase_uom_factor=2,
		)

		result = generate_whole_carton_suggestions(po.name)
		self.assertEqual(result["carton_count"], 2)
		self.assertEqual(result["items"][0]["qty_per_carton"], 5)
		self.assertEqual(result["items"][0]["remainder_qty"], 0)
		self.assertEqual(
			frappe.db.sql(
				"""select sum(a.qty) from `tabJD Carton Allocation` a
				inner join `tabJD Carton` c on c.name = a.parent
				where c.purchase_order = %s and a.jd_sku = %s""",
				(po.name, jd_sku),
			)[0][0],
			10,
		)

	def test_generation_skips_item_without_box_uom(self):
		po, _, _ = _make_po("NO-BOX", quantities=[9], box_factor=None)

		result = generate_whole_carton_suggestions(po.name)
		self.assertEqual(result["carton_count"], 0)
		self.assertEqual(result["items"][0]["status"], "未配置整箱单位")
		self.assertEqual(result["items"][0]["remainder_qty"], 9)
		self.assertFalse(frappe.db.exists("JD Carton", {"purchase_order": po.name}))

	def test_generated_product_bundle_cartons_keep_component_expansion(self):
		suffix = generate_hash(length=8)
		component = make_item(f"_Test JD WB Component {suffix}", is_stock_item=1)
		parent = make_item(f"_Test JD WB Bundle {suffix}", is_stock_item=0)
		_add_uom_conversion(parent, "Box", 2)
		frappe.get_doc(
			{
				"doctype": "Product Bundle",
				"new_item_code": parent,
				"items": [{"item_code": component, "qty": 3, "uom": "Nos"}],
			}
		).insert()
		po, jd_sku, _ = _make_po("BUNDLE", quantities=[4], platform_item=parent)

		result = generate_whole_carton_suggestions(po.name)
		self.assertEqual(result["carton_count"], 2)
		component_qty = frappe.db.sql(
			"""select sum(ci.qty) from `tabJD Carton Item` ci
			inner join `tabJD Carton` c on c.name = ci.parent
			where c.purchase_order = %s and ci.jd_sku = %s and ci.stock_item = %s""",
			(po.name, jd_sku, component),
		)[0][0]
		self.assertEqual(component_qty, 12)

	def test_workbench_and_generation_require_permissions(self):
		po, _, _ = _make_po("PERMISSION", quantities=[4], box_factor=2)
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				get_packing_workbench(po.name)
			with self.assertRaises(frappe.PermissionError):
				generate_whole_carton_suggestions(po.name)
		finally:
			frappe.set_user("Administrator")


def _make_po(
	label: str,
	quantities: list[int],
	box_factor: int | None = None,
	purchase_uom: str = "Nos",
	purchase_uom_factor: int | None = None,
	platform_item: str | None = None,
):
	suffix = generate_hash(length=8)
	company = frappe.db.get_value("Company", {}, "name")
	item = platform_item or make_item(f"_Test JD WB Item {label} {suffix}", is_stock_item=1)
	if purchase_uom_factor:
		_add_uom_conversion(item, purchase_uom, purchase_uom_factor)
	if box_factor:
		_add_uom_conversion(item, "Box", box_factor)
	jd_sku = f"_TEST-JD-WB-{label}-{suffix}"
	frappe.get_doc({"doctype": "JD SKU Mapping", "jd_sku": jd_sku, "platform_item": item}).insert()
	batch = frappe.get_doc(
		{
			"doctype": "JD Purchase Import Batch",
			"company": company,
			"import_file": f"/private/files/test-jd-workbench-{suffix}.xlsx",
		}
	).insert()
	po = frappe.get_doc(
		{
			"doctype": "JD Purchase Order",
			"jd_purchase_order_no": f"_TEST-JD-PO-WB-{label}-{suffix}",
			"import_batch": batch.name,
			"company": company,
			"items": [
				{
					"jd_sku": jd_sku,
					"jd_item_name": f"JD Item {label}",
					"purchase_qty": qty,
					"purchase_uom": purchase_uom,
				}
				for qty in quantities
			],
		}
	).insert()
	return po, jd_sku, item


def _add_uom_conversion(item_code: str, uom: str, conversion_factor: int):
	if not frappe.db.exists("UOM", uom):
		frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert()
	item = frappe.get_doc("Item", item_code)
	for row in item.uoms:
		if row.uom == uom:
			row.conversion_factor = conversion_factor
			item.save()
			return
	item.append("uoms", {"uom": uom, "conversion_factor": conversion_factor})
	item.save()
