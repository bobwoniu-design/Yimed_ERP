frappe.ui.form.on("Delivery Note", {
	refresh(frm) {
		if (frm.is_new()) return;
		const label = __("批次检测报告");
		frm.remove_custom_button(label, __("导出"));
		frappe.call({
			method: "channel_erp.content_center.get_delivery_note_inspection_report_summary",
			args: {delivery_note: frm.docname},
		}).then((response) => {
			const summary = response.message || {};
			if (summary.permissions?.can_export === false) return;
			frm.add_custom_button(label, () => show_summary(frm, summary), __("导出"));
		}).catch(() => {});
	},
});

function show_summary(frm, summary) {
	const escape = (value) => frappe.utils.escape_html(String(value ?? ""));
	const rows = summary.batches || summary.rows || [];
	const missing = rows.filter((row) => !row.has_report || row.exportable === false);
	const existing = Math.max(0, Number(summary.exportable_count ?? summary.report_count ?? (rows.length - missing.length)));
	const total = Number(summary.batch_count ?? summary.total ?? rows.length);
	const dialog = new frappe.ui.Dialog({
		title: __("批次检测报告"),
		fields: [{fieldname: "summary", fieldtype: "HTML"}],
		primary_action_label: existing ? __("导出检测报告压缩包") : null,
		primary_action() {
			if (dialog.__busy) return;
			dialog.__busy = true;
			dialog.get_primary_btn()?.prop("disabled", true);
			window.open(`/api/method/channel_erp.content_center.export_delivery_note_inspection_reports_zip?delivery_note=${encodeURIComponent(frm.docname)}`, "_blank", "noopener");
			setTimeout(() => { dialog.__busy = false; dialog.get_primary_btn()?.prop("disabled", false); }, 1500);
		},
	});
	const missingRows = missing.map((row) => `<li><a href="/app/batch/${encodeURIComponent(row.batch_no || row.batch || "")}" target="_blank" rel="noopener">${escape(row.batch_no || row.batch || __("未命名批次"))}</a>${row.item_code ? ` · ${escape(row.item_code)}` : ""}</li>`).join("");
	dialog.fields_dict.summary.$wrapper.html(`<div>
		<div class="alert alert-info">${__("将每个批次的原始检测报告分别放入 ZIP 压缩包，不转换或合并文件，并保留上传时的原文件名。压缩包名称为出库单号-检测报告.zip；缺失报告只作提示，不阻断已有报告导出。")}</div>
		<p><strong>${__("已有报告")}：</strong>${escape(existing)} / ${escape(total)}</p>
		${missingRows ? `<details><summary>${__("查看缺失批次")} (${missing.length})</summary><ul>${missingRows}</ul></details>` : `<span class="indicator-pill green">${__("所有批次均有报告")}</span>`}
		${!existing ? `<div class="alert alert-warning mt-3">${__("当前没有可导出的检测报告。出库单不受影响。")}</div>` : ""}
	</div>`);
	dialog.show();
	if (!existing) dialog.get_primary_btn()?.addClass("hide");
}
