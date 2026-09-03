import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, generate_hash, nowdate

from yimed_ecommerce.jd.packing import (
	assign_batch_to_cartons,
	copy_carton,
	create_equal_cartons,
	create_mixed_cartons,
	delete_unverified_carton,
	update_carton_allocations,
	verify_cartons,
)
from yimed_ecommerce.jd.test_product_bundle import make_item
from yimed_ecommerce.jd.test_transfer import _get_two_warehouses
from yimed_ecommerce.jd.transfer import create_transfer


class TestJDCartonEditing(IntegrationTestCase):
	def test_update_rebuilds_components_and_preserves_matching_batch(self):
		suffix = generate_hash(length=8)
		item = make_item(f"_Test JD Edit Batch Item {suffix}", is_stock_item=1, has_batch_no=1)
		po, skus = _make_po("EDIT-BATCH", [(item, 10)])
		create_equal_cartons(po.name, skus[0], total_qty=4, qty_per_carton=4)
		carton = frappe.db.get_value("JD Carton", {"purchase_order": po.name}, "name")
		batch = frappe.get_doc(
			{
				"doctype": "Batch",
				"batch_id": f"_TEST-JD-EDIT-BATCH-{suffix}",
				"item": item,
				"manufacturing_date": add_days(nowdate(), -10),
				"expiry_date": add_days(nowdate(), 100),
			}
		).insert()
		assign_batch_to_cartons(po.name, item, batch.name)

		result = update_carton_allocations(carton, [{"jd_sku": skus[0], "qty": 5}])
		self.assertEqual(result["allocations"][0]["qty"], 5)
		self.assertEqual(result["components"][0]["qty"], 5)
		self.assertEqual(result["components"][0]["batch_no"], batch.name)
		po.reload()
		self.assertEqual(po.packing_status, "装箱中")
		self.assertEqual(po.status, "装箱中")

	def test_update_validates_replacement_total_and_input(self):
		item = make_item(f"_Test JD Edit Qty {generate_hash(length=8)}", is_stock_item=1)
		po, skus = _make_po("EDIT-QTY", [(item, 10)])
		cartons = create_equal_cartons(po.name, skus[0], total_qty=8, qty_per_carton=4)["cartons"]

		with self.assertRaisesRegex(frappe.ValidationError, "packed in other cartons 4.*replacement 7"):
			update_carton_allocations(cartons[0], [{"jd_sku": skus[0], "qty": 7}])
		with self.assertRaisesRegex(frappe.ValidationError, "positive Quantity"):
			update_carton_allocations(cartons[0], [{"jd_sku": skus[0], "qty": 0}])
		with self.assertRaisesRegex(frappe.ValidationError, "is not in Purchase Order"):
			update_carton_allocations(cartons[0], [{"jd_sku": "NOT-IN-PO", "qty": 1}])

	def test_verified_and_transferred_cartons_cannot_be_changed(self):
		item = make_item(f"_Test JD Edit Locked {generate_hash(length=8)}", is_stock_item=1)
		po, skus = _make_po("VERIFIED", [(item, 2)])
		carton = create_equal_cartons(po.name, skus[0], total_qty=2, qty_per_carton=2)["cartons"][0]
		verify_cartons(po.name)

		with self.assertRaisesRegex(frappe.ValidationError, "is verified and cannot be changed"):
			delete_unverified_carton(carton)

		source, target = _get_two_warehouses(po.company)
		create_transfer(po.name, source, target)
		with self.assertRaisesRegex(frappe.ValidationError, "Draft Stock Entry.*Delete it"):
			copy_carton(carton, 1)

	def test_delete_updates_status_without_renumbering(self):
		item = make_item(f"_Test JD Delete Carton {generate_hash(length=8)}", is_stock_item=1)
		po, skus = _make_po("DELETE", [(item, 2)])
		cartons = create_equal_cartons(po.name, skus[0], total_qty=2, qty_per_carton=1)["cartons"]

		first = delete_unverified_carton(cartons[0])
		self.assertEqual(first["carton_count"], 1)
		self.assertEqual(first["packing_status"], "装箱中")
		self.assertEqual(frappe.db.get_value("JD Carton", cartons[1], "carton_sequence"), 2)
		po.reload()
		self.assertEqual(po.total_cartons, 1)

		second = delete_unverified_carton(cartons[1])
		self.assertEqual(second["carton_count"], 0)
		self.assertEqual(second["packing_status"], "待装箱")
		po.reload()
		self.assertEqual(po.total_cartons, 0)
		self.assertEqual(po.status, "待备货")

	def test_copy_mixed_carton_uses_default_and_explicit_sequences_and_expands_bundle(self):
		suffix = generate_hash(length=8)
		direct = make_item(f"_Test JD Copy Direct {suffix}", is_stock_item=1)
		component = make_item(f"_Test JD Copy Component {suffix}", is_stock_item=1)
		bundle = make_item(f"_Test JD Copy Bundle {suffix}", is_stock_item=0)
		frappe.get_doc(
			{
				"doctype": "Product Bundle",
				"new_item_code": bundle,
				"items": [{"item_code": component, "qty": 2, "uom": "Nos"}],
			}
		).insert()
		po, skus = _make_po("COPY-MIXED", [(direct, 10), (bundle, 10)])
		source = create_mixed_cartons(
			po.name,
			[
				{"jd_sku": skus[0], "qty": 1},
				{"jd_sku": skus[1], "qty": 1},
			],
			repeat_count=1,
			start_sequence=2,
		)["cartons"][0]

		default_copy = copy_carton(source, 2)
		self.assertEqual(default_copy["start_sequence"], 3)
		self.assertEqual(
			frappe.get_all(
				"JD Carton",
				filters={"purchase_order": po.name},
				pluck="carton_sequence",
				order_by="carton_sequence",
			),
			[2, 3, 4],
		)
		self.assertEqual(
			frappe.db.sql(
				"""select sum(ci.qty) from `tabJD Carton Item` ci
				inner join `tabJD Carton` c on c.name = ci.parent
				where c.purchase_order = %s and ci.stock_item = %s""",
				(po.name, component),
			)[0][0],
			6,
		)

		explicit_copy = copy_carton(source, 1, start_sequence=6)
		self.assertEqual(explicit_copy["start_sequence"], 6)
		with self.assertRaisesRegex(frappe.ValidationError, "Carton Sequence 4 already exists"):
			copy_carton(source, 2, start_sequence=4)

	def test_copy_rejects_overpacking_invalid_count_and_guest(self):
		item = make_item(f"_Test JD Copy Boundary {generate_hash(length=8)}", is_stock_item=1)
		po, skus = _make_po("COPY-BOUNDARY", [(item, 3)])
		carton = create_equal_cartons(po.name, skus[0], total_qty=2, qty_per_carton=2)["cartons"][0]

		with self.assertRaisesRegex(frappe.ValidationError, "Packing would exceed ordered quantity"):
			copy_carton(carton, 1)
		with self.assertRaisesRegex(frappe.ValidationError, "positive integer"):
			copy_carton(carton, 0)

		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				update_carton_allocations(carton, [{"jd_sku": skus[0], "qty": 1}])
		finally:
			frappe.set_user("Administrator")


def _make_po(label: str, item_quantities: list[tuple[str, int]]):
	suffix = generate_hash(length=8)
	company = frappe.db.get_value("Company", {}, "name")
	batch = frappe.get_doc(
		{
			"doctype": "JD Purchase Import Batch",
			"company": company,
			"import_file": f"/private/files/test-jd-carton-edit-{suffix}.xlsx",
		}
	).insert()
	items = []
	skus = []
	for index, (item, qty) in enumerate(item_quantities, 1):
		jd_sku = f"_TEST-JD-EDIT-{label}-{index}-{suffix}"
		frappe.get_doc({"doctype": "JD SKU Mapping", "jd_sku": jd_sku, "platform_item": item}).insert()
		skus.append(jd_sku)
		items.append({"jd_sku": jd_sku, "purchase_qty": qty, "purchase_uom": "Nos"})
	po = frappe.get_doc(
		{
			"doctype": "JD Purchase Order",
			"jd_purchase_order_no": f"_TEST-JD-PO-EDIT-{label}-{suffix}",
			"import_batch": batch.name,
			"company": company,
			"items": items,
		}
	).insert()
	return po, skus
