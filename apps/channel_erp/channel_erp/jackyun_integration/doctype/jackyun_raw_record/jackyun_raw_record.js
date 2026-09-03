frappe.ui.form.on("Jackyun Raw Record", {
	refresh(frm) {
		if (frm.doc.processing_status !== "Failed" || !frm.doc.retryable) return;
		frm.add_custom_button(__("重新处理"), () => {
			frappe.call({
				method: "channel_erp.jackyun_integration.doctype.jackyun_raw_record.jackyun_raw_record.retry_processing",
				args: { name: frm.doc.name },
				freeze: true,
				freeze_message: __("正在重新处理原始数据…"),
			}).then((response) => {
				const result = response.message || {};
				frappe.show_alert({
					indicator: result.ok ? "green" : "red",
					message: result.ok ? __("重新处理成功") : __("重新处理失败：{0}", [result.error || __("未知错误")]),
				});
				frm.reload_doc();
			});
		});
	},
});
