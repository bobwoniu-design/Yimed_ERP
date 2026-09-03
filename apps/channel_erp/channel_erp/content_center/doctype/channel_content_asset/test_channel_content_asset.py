import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.file_manager import save_file

from channel_erp.content_center import (
	create_folder,
	get_asset_detail,
	get_workspace,
	permanently_delete_assets,
	register_uploaded_file,
)
from channel_erp.content_center.simple_api import _root_folder
from channel_erp.setup import OBSOLETE_CONTENT_CENTER_DOCTYPES


class TestChannelContentAsset(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_company_dimension_is_removed(self):
		for doctype in ("Channel Content Folder", "Channel Content Asset"):
			self.assertFalse(frappe.get_meta(doctype).has_field("company"))
			self.assertFalse(frappe.db.has_column(doctype, "company"))
		for doctype in OBSOLETE_CONTENT_CENTER_DOCTYPES:
			self.assertFalse(frappe.db.exists("DocType", doctype))

	def test_global_root_and_folder_acl_workspace(self):
		root = _root_folder()
		workspace = get_workspace()
		self.assertEqual(workspace["root_folder"], root)
		self.assertNotIn("company", workspace)
		self.assertNotIn("available_companies", workspace)
		self.assertTrue(workspace["permissions"]["can_manage"])

	def test_private_file_lifecycle_without_company(self):
		root = _root_folder()
		folder = create_folder(f"测试目录-{frappe.generate_hash(length=8)}", parent=root)["name"]
		file_doc = save_file("content-center-test.txt", b"company-neutral", None, None, is_private=1)
		result = register_uploaded_file(file_doc.name, folder, title="测试文件", tags="测试,文档")
		asset = result["asset"]
		detail = get_asset_detail(asset)
		self.assertEqual(detail["asset"]["folder"], folder)
		self.assertNotIn("company", detail["asset"])
		self.assertEqual(detail["asset"]["tags"], "测试,文档")
		permanently_delete_assets([asset])
		self.assertFalse(frappe.db.exists("File", file_doc.name))
		frappe.delete_doc("Channel Content Folder", folder, ignore_permissions=True)

	def test_batch_report_is_independent_from_content_center(self):
		field = frappe.db.get_value(
			"Custom Field", "Batch-custom_inspection_report_attachment",
			["label", "fieldtype"], as_dict=True,
		)
		self.assertEqual(field.label, "检测报告附件")
		self.assertEqual(field.fieldtype, "Attach")
