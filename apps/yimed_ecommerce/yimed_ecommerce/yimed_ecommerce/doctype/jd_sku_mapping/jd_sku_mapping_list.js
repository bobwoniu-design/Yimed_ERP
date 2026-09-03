frappe.listview_settings["JD SKU Mapping"] = {
	onload(listview) {
		listview.page.add_inner_button(__("导入映射表"), () => {
			const dialog = new frappe.ui.Dialog({
				title: __("导入京东SKU映射表"),
				fields: [
					{
						fieldname: "file_url",
						fieldtype: "Attach",
						label: __("Excel文件"),
						reqd: 1,
						description: __("支持附件原表头：链接主SKU、SKU"),
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
							frappe.msgprint(__("新增 {0}，更新 {1}，失败 {2}", [result.created || 0, result.updated || 0, result.failed || 0]));
							listview.refresh();
						},
					});
				},
			});
			dialog.show();
		});
	},
};
