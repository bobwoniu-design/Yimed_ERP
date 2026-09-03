frappe.ui.form.on("Ecommerce Promotion Import Batch", {
	async sales_channel(frm) {
		if (!frm.doc.sales_channel) return;
		const company = await frappe.db.get_value("Jackyun Sales Channel", frm.doc.sales_channel, "company_name");
		if (company && company.message) frm.set_value("company", company.message.company_name);
	},
	refresh(frm) {
		if (!frm.is_new() && frm.doc.import_file) {
			frm.add_custom_button(__("Import Product Promotion Report"), async () => {
				await frappe.call({
					method: "yimed_ecommerce.ecommerce_dashboard.doctype.ecommerce_promotion_import_batch.ecommerce_promotion_import_batch.import_expenses",
					args: { name: frm.doc.name },
					freeze: true,
					freeze_message: __("Importing Tmall product promotion report..."),
				});
				await frm.reload_doc();
			}, __("Import Tools"));
		}
		frm.add_custom_button(__("Download Import Template"), () => {
			open_url_post(
				"/api/method/yimed_ecommerce.ecommerce_dashboard.promotion_import.download_import_template",
				{},
			);
		}, __("Import Tools"));
	},
});
