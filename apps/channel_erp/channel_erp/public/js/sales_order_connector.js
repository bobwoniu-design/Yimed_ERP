frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		if (!frm.doc.__islocal) {
			frm.add_custom_button(__("查看推送队列"), () => {
				frappe.set_route("List", "Connector Outbound Message", {
					reference_doctype: "Sales Order",
					reference_name: frm.doc.name,
				});
			}, __("接口"));
		}
	},
});
