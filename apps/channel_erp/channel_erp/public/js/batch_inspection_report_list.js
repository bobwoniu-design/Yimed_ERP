(() => {
	const FIELD = "custom_inspection_report_attachment";
	const current = frappe.listview_settings.Batch || {};
	const originalOnload = current.onload;
	const originalFormatter = current.formatters?.[FIELD];
	const addFields = [...new Set([...(current.add_fields || []), FIELD])];

	frappe.listview_settings.Batch = {
		...current,
		add_fields: addFields,
		formatters: {
			...(current.formatters || {}),
			[FIELD](value, df, doc) {
				if (originalFormatter) return originalFormatter(value, df, doc);
				return value
					? `<span class="indicator-pill green cc-batch-report-indicator" data-report-status="existing">${__("已有")}</span>`
					: `<span class="indicator-pill orange cc-batch-report-indicator" data-report-status="missing">${__("缺失")}</span>`;
			},
		},
		onload(listview) {
			if (originalOnload) originalOnload(listview);
			if (listview.__inspection_report_filters_added) return;
			listview.__inspection_report_filters_added = true;
			const apply = async (mode) => {
				await listview.filter_area.remove(FIELD);
				if (mode === "existing") {
					await listview.filter_area.add("Batch", FIELD, "is", "set");
				} else if (mode === "missing") {
					await listview.filter_area.add("Batch", FIELD, "is", "not set");
				} else {
					listview.refresh();
				}
			};
			listview.page.add_inner_button(__("全部"), () => apply("all"), __("检测报告"));
			listview.page.add_inner_button(__("缺失"), () => apply("missing"), __("检测报告"));
			listview.page.add_inner_button(__("已有"), () => apply("existing"), __("检测报告"));
		},
		button: current.button || {
			show: () => true,
			get_label: () => __("检测报告"),
			get_description: (doc) => __("打开批次 {0} 管理检测报告", [doc.name]),
			action: (doc) => frappe.set_route("Form", "Batch", doc.name),
		},
	};
})();
