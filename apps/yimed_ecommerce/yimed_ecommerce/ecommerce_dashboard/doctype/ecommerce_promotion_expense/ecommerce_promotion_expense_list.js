frappe.listview_settings["Ecommerce Promotion Expense"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Rematch Promotion Products"), async () => {
			const result = await frappe.xcall(
				"yimed_ecommerce.ecommerce_dashboard.promotion_import.rematch_unmatched_promotions",
			);
			await listview.refresh();
			frappe.show_alert({
				message: __("Rematched {0} analysis rows and {1} expense rows.", [result.performance_rows, result.expense_rows]),
				indicator: "green",
			}, 7);
		});
		listview.page.add_inner_button(__("Download Import Template"), () => {
			window.open(
				"/api/method/yimed_ecommerce.ecommerce_dashboard.promotion_import.download_import_template",
				"_blank",
			);
		});

		if (!frappe.model.can_create("Ecommerce Promotion Import Batch")) return;
		listview.page.add_inner_button(__("Import Product Promotion Report"), () => {
			const dialog = new frappe.ui.Dialog({
				title: __("Import Product Promotion Report"),
				fields: [
					{
						fieldname: "sales_channel",
						fieldtype: "Link",
						options: "Jackyun Sales Channel",
						label: __("销售渠道"),
						reqd: 1,
						description: __("报表归属的销售渠道（公司由渠道带出）。拼多多/阿里健康等无店铺列的报表按此渠道入账。"),
						get_query() {
							return { filters: { disabled: 0, deleted: 0, channel_type: "1" } };
						},
					},
					{
						fieldname: "file_url",
						fieldtype: "Attach",
						label: __("Promotion Report File"),
						reqd: 1,
					},
				],
				primary_action_label: __("Start Import"),
				primary_action: async (values) => {
					dialog.disable_primary_action();
					try {
						const result = await frappe.xcall(
							"yimed_ecommerce.ecommerce_dashboard.doctype.ecommerce_promotion_import_batch.ecommerce_promotion_import_batch.create_and_import_batch",
							values,
						);
						dialog.hide();
						await listview.refresh();
						const summary = __("Import batch {0}: {1} succeeded, {2} failed.", [
							result.name,
							result.success_count,
							result.failed_count,
						]);
						if (result.failed_count) {
							frappe.msgprint({
								title: __("Import Completed with Errors"),
								indicator: "orange",
								message: `${frappe.utils.escape_html(summary)}<pre class="mt-3">${frappe.utils.escape_html(result.error_log || "")}</pre>`,
							});
						} else {
							frappe.show_alert({ message: summary, indicator: "green" }, 7);
						}
					} finally {
						dialog.enable_primary_action();
					}
				},
			});
			dialog.show();
		});
	},
};
