frappe.query_reports["Ecommerce Store Profit"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company", reqd: 1, default: frappe.defaults.get_user_default("Company") },
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date", reqd: 1, default: frappe.datetime.add_months(frappe.datetime.get_today(), -1) },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", reqd: 1, default: frappe.datetime.get_today() },
		{ fieldname: "channel_name", label: __("Channel"), fieldtype: "Select", options: [""] },
		{ fieldname: "store_name", label: __("Store"), fieldtype: "Select", options: [""] },
	],
	async onload(report) {
		const options = await frappe.xcall("yimed_ecommerce.ecommerce_dashboard.dashboard.get_filter_options", {
			company: report.get_filter_value("company"),
		});
		const channel = report.get_filter("channel_name");
		channel.df.options = ["", ...options.channels];
		channel.refresh();
		const store = report.get_filter("store_name");
		store.df.options = ["", ...new Set(options.stores.map((row) => row.store))];
		store.refresh();
	},
};
