frappe.ui.form.on("Ecommerce Fee Rule", {
	async onload(frm) {
		await load_scope_options(frm);
	},
	async company(frm) {
		await load_scope_options(frm);
	},
	refresh(frm) {
		if (frm.doc.company) {
			load_scope_options(frm);
		}
	},
});

frappe.ui.form.on("Ecommerce Fee Rule Scope", {
	channel_name(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const stores = get_stores_for_channel(frm, row.channel_name);
		update_row_store_options(frm, cdn, stores);
		if (row.store_name && !stores.includes(row.store_name)) {
			frappe.model.set_value(cdt, cdn, "store_name", "");
		}
	},
});

async function load_scope_options(frm) {
	const options = await frappe.xcall(
		"yimed_ecommerce.ecommerce_dashboard.promotion_import.get_channel_store_options",
		{ company: frm.doc.company },
	);
	frm.__fee_scope_options = options || { channels: [], stores: [] };

	const grid = frm.fields_dict.scopes?.grid;
	if (!grid) return;
	grid.update_docfield_property("channel_name", "options", frm.__fee_scope_options.channels || []);
	grid.update_docfield_property("store_name", "options", get_all_stores(frm));
}

function get_all_stores(frm) {
	return [...new Set((frm.__fee_scope_options?.stores || [])
		.map((row) => row.store)
		.filter(Boolean))].sort();
}

function get_stores_for_channel(frm, channel) {
	return [...new Set((frm.__fee_scope_options?.stores || [])
		.filter((row) => !channel || row.channel === channel)
		.map((row) => row.store)
		.filter(Boolean))].sort();
}

function update_row_store_options(frm, cdn, stores) {
	// 该行的店铺下拉只保留所选渠道下的店铺
	const grid_row = frm.fields_dict.scopes?.grid?.grid_rows_by_docname?.[cdn];
	const control = grid_row?.fields_dict?.store_name;
	if (control?.df) {
		control.df.options = stores;
		control.set_options?.();
	}
}
