frappe.query_reports["JD Warehouse Summary"] = {
	filters: [
		{
			fieldname: "import_batch",
			label: __("导入批次"),
			fieldtype: "Link",
			options: "JD Purchase Import Batch",
			reqd: 1,
		},
		{
			fieldname: "destination_city",
			label: __("目的城市"),
			fieldtype: "Data",
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
			const destination_city = report.get_filter_value("destination_city");
			if (destination_city) params.set("destination_city", destination_city);
			window.open(
				`/api/method/yimed_ecommerce.jd.report_pdf.download_warehouse_summary_pdf?${params.toString()}`
			);
		});
	},
};
