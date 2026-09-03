frappe.ui.form.on("JD Self Operated Settings", {
	setup(frm) {
		for (const fieldname of [
			"default_source_warehouse",
			"default_target_warehouse",
			"default_transit_warehouse",
		]) {
			frm.set_query(fieldname, () => ({
				filters: {
					company: frm.doc.company,
					is_group: 0,
					disabled: 0,
				},
			}));
		}
	},
});
