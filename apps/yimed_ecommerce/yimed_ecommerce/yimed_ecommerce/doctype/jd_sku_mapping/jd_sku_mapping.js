frappe.ui.form.on("JD SKU Mapping", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("导入映射表"), open_mapping_import);
	},
});

function open_mapping_import() {
	const dialog = new frappe.ui.Dialog({
		title: __("导入京东SKU映射表"),
		fields: [
			{
				fieldname: "file_url",
				fieldtype: "Attach",
				label: __("Excel文件"),
				reqd: 1,
					description: __("支持价格表全部字段；必填：ERP编码、SKU。导入后会自动刷新现有京东采购单映射。"),
			},
		],
		primary_action_label: __("开始导入"),
		primary_action(values) {
			frappe.call({
				method: "yimed_ecommerce.yimed_ecommerce.doctype.jd_sku_mapping.jd_sku_mapping.import_mapping_file",
				args: { file_url: values.file_url },
				freeze: true,
				callback: (response) => {
					dialog.hide();
					const result = response.message || {};
					frappe.msgprint(__("新增 {0}，更新 {1}，失败 {2}，刷新采购单 {3}", [result.created || 0, result.updated || 0, result.failed || 0, result.purchase_orders_refreshed || 0]));
				},
			});
		},
	});
	dialog.show();
}
