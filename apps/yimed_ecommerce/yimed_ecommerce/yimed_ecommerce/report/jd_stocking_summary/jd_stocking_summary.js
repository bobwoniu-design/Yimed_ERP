frappe.query_reports["JD Stocking Summary"] = {
	filters: [
		{
			fieldname: "import_batch",
			label: __("导入批次"),
			fieldtype: "Link",
			options: "JD Purchase Import Batch",
			reqd: 1,
		},
		{
			fieldname: "purchase_order",
			label: __("京东采购单"),
			fieldtype: "Link",
			options: "JD Purchase Order",
			get_query: () => ({ filters: { import_batch: frappe.query_report.get_filter_value("import_batch") } }),
		},
	],
	onload(report) {
		report.page.add_inner_button(__("下载横向PDF"), () => {
			const import_batch = report.get_filter_value("import_batch");
			if (!import_batch) {
				frappe.msgprint(__("请先选择导入批次。"));
				return;
			}
			const params = new URLSearchParams({ import_batch });
			const purchase_order = report.get_filter_value("purchase_order");
			if (purchase_order) params.set("purchase_order", purchase_order);
			window.open(
				`/api/method/yimed_ecommerce.jd.report_pdf.download_stocking_summary_pdf?${params.toString()}`
			);
		});
	},
};
