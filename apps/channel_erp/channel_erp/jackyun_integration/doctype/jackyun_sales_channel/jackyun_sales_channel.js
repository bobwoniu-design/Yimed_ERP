const paired_fields = [
	["online_platform_code", "online_platform_name"],
	["platform_shop_id", "platform_shop_name"],
	["company_code", "company_name"],
	["department_id", "department_name"],
	["warehouse_code", "warehouse_name"],
	["category_id", "category_name"],
];

function option_label(primary, secondary) {
	return secondary ? `${primary} - ${secondary}` : primary;
}

function set_autocomplete_data(frm, fieldname, options) {
	const control = frm.fields_dict[fieldname];
	if (control && control.set_data) {
		control.set_data(options);
	}
}

function synchronize_pair(frm, changed_field) {
	for (const [code_field, name_field] of paired_fields) {
		if (![code_field, name_field].includes(changed_field)) continue;

		const pairs = frm.jackyun_controlled_options?.[`${code_field}:${name_field}`] || [];
		const source_key = changed_field === code_field ? "code" : "name";
		const target_field = changed_field === code_field ? name_field : code_field;
		const target_key = changed_field === code_field ? "name" : "code";
		const matches = pairs.filter((row) => row[source_key] === frm.doc[changed_field]);
		const selected = matches.length === 1 ? matches[0] : null;

		if (selected && frm.doc[target_field] !== selected[target_key]) {
			frm.set_value(target_field, selected[target_key]);
		}
		break;
	}
}

frappe.ui.form.on("Jackyun Sales Channel", {
	async setup(frm) {
		const response = await frappe.call({
			method:
				"channel_erp.jackyun_integration.doctype.jackyun_sales_channel.jackyun_sales_channel.get_controlled_field_options",
		});
		frm.jackyun_controlled_options = response.message || {};

		for (const [code_field, name_field] of paired_fields) {
			const pairs = frm.jackyun_controlled_options[`${code_field}:${name_field}`] || [];
			set_autocomplete_data(
				frm,
				code_field,
				pairs.map((row) => ({
					value: row.code,
					label: option_label(row.code, row.name),
				}))
			);
			set_autocomplete_data(
				frm,
				name_field,
				pairs.map((row) => ({
					value: row.name,
					label: option_label(row.name, row.code),
				}))
			);
		}

		for (const fieldname of ["responsible_user_name"]) {
			set_autocomplete_data(frm, fieldname, frm.jackyun_controlled_options[fieldname] || []);
		}
	},
});

for (const fields of paired_fields) {
	for (const fieldname of fields) {
		frappe.ui.form.on("Jackyun Sales Channel", fieldname, (frm) => {
			synchronize_pair(frm, fieldname);
		});
	}
}
