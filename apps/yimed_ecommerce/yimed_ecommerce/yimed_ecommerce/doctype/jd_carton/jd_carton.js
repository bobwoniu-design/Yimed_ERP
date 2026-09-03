frappe.ui.form.on("JD Carton", {
	setup(frm) {
		frm.set_query("batch_no", "components", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return { filters: { item: row.stock_item, disabled: 0 } };
		});
	},
	refresh(frm) {
		frm.fields_dict.components.grid.cannot_add_rows = true;
		frm.fields_dict.components.grid.cannot_delete_rows = true;
		if (!frm.is_new()) {
			frm.add_custom_button(__("打印箱贴"), () => {
				frm.call("mark_printed").then(() => {
					frappe.utils.print(frm.doctype, frm.docname, "JD Carton Label");
				});
			});
		}
	},
});
