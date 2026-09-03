(() => {
	const METHOD = "channel_erp.content_center.";
	const escape = (value) => frappe.utils.escape_html(String(value ?? ""));

	frappe.ui.form.on("Batch", {
		refresh(frm) {
			frm.set_df_property("custom_inspection_report_attachment", "read_only", 1);
			load_report(frm);
		},
	});

	async function load_report(frm) {
		if (frm.is_new()) return;
		const requestId = (frm.__inspection_report_request_id || 0) + 1;
		frm.__inspection_report_request_id = requestId;
		try {
			const response = await frappe.call({
				method: `${METHOD}get_batch_inspection_report`,
				args: {batch_no: frm.docname},
			});
			if (requestId !== frm.__inspection_report_request_id) return;
			render_report(frm, response.message || {});
		} catch (error) {
			// Optional enhancement: Batch remains fully usable when the API is unavailable.
		}
	}

	function render_report(frm, report) {
		const permissions = report.permissions || {};
		const hasReport = Boolean(report.has_report);
		const status = hasReport
			? `<span class="indicator-pill green">${__("已有检测报告")}</span>`
			: `<span class="indicator-pill orange">${__("缺失检测报告")}</span>`;
		const fileMeta = hasReport
			? `<div class="text-muted small" style="margin-top:6px">${escape(report.file_name || __("检测报告"))}${report.file_size ? ` · ${format_size(report.file_size)}` : ""}${report.file_type ? ` · ${escape(report.file_type)}` : ""}</div>`
			: `<div class="text-muted small" style="margin-top:6px">${__("报告只作业务提示，不阻断批次或库存操作。")}</div>`;
		const actions = [
			!hasReport && permissions.can_upload ? button("upload", __("上传报告"), "btn-primary") : "",
			hasReport && permissions.can_view ? button("preview", __("查看")) : "",
			hasReport && permissions.can_download ? button("download", __("下载")) : "",
			hasReport && permissions.can_replace ? button("replace", __("替换")) : "",
			hasReport && permissions.can_delete ? button("delete", __("删除"), "btn-danger") : "",
		].filter(Boolean).join(" ");
		const section = frm.dashboard.add_section(`<div class="channel-batch-inspection-report">
			<div class="cc-batch-inspection-status">${status}${fileMeta}</div>
			${actions ? `<div class="cc-batch-inspection-actions" style="margin-top:10px">${actions}</div>` : ""}
		</div>`, __("检测报告附件"), "custom channel-batch-inspection-section");
		section.find("[data-inspection-action]").on("click", (event) => {
			event.preventDefault();
			const action = $(event.currentTarget).data("inspection-action");
			handle_action(frm, action, report);
		});
	}

	function button(action, label, style = "btn-default") {
		return `<button type="button" class="btn ${style} btn-sm cc-batch-inspection-${action}" data-inspection-action="${action}">${label}</button>`;
	}

	function format_size(bytes) {
		if (frappe.utils.format_file_size) return escape(frappe.utils.format_file_size(bytes));
		return escape(`${bytes} B`);
	}

	function handle_action(frm, action, report) {
		if (frm.__inspection_report_busy) return;
		if (action === "preview" || action === "download") {
			const preview = action === "preview" ? 1 : 0;
			const url = (preview ? report.preview_url : report.download_url) ||
				`/api/method/${METHOD}download_batch_inspection_report?batch_no=${encodeURIComponent(frm.docname)}&preview=${preview}`;
			window.open(url, "_blank", "noopener");
			return;
		}
		if (action === "upload") return open_uploader(frm, false);
		if (action === "replace") {
			return frappe.confirm(
				__("确认替换批次 {0} 的检测报告？原报告引用将被新文件替换。", [frm.docname]),
				() => open_uploader(frm, true),
			);
		}
		if (action === "delete") {
			frappe.confirm(
				__("确认删除批次 {0} 的检测报告？此操作不会删除批次。", [frm.docname]),
				() => delete_report(frm),
			);
		}
	}

	function open_uploader(frm, replacing) {
		if (frm.__inspection_report_busy) return;
		new frappe.ui.FileUploader({
			allow_multiple: false,
			make_attachments_public: false,
			dialog_title: replacing ? __("替换检测报告") : __("上传检测报告"),
			on_success: async (file) => {
				if (frm.__inspection_report_busy) return;
				frm.__inspection_report_busy = true;
				try {
					await frappe.call({
						method: `${METHOD}set_batch_inspection_report`,
						args: {batch_no: frm.docname, file: file.name},
						freeze: true,
						freeze_message: replacing ? __("正在替换检测报告…") : __("正在保存检测报告…"),
					});
					frappe.show_alert({message: replacing ? __("检测报告已替换") : __("检测报告已上传"), indicator: "green"});
					await frm.reload_doc();
				} catch (error) {
					try {
						await frappe.call({method: `${METHOD}discard_uploaded_file`, args: {file: file.name}});
					} catch (cleanupError) { /* best-effort cleanup */ }
					frappe.msgprint(__("检测报告保存失败，批次数据未受影响。"));
				} finally {
					frm.__inspection_report_busy = false;
				}
			},
		});
	}

	async function delete_report(frm) {
		if (frm.__inspection_report_busy) return;
		frm.__inspection_report_busy = true;
		try {
			await frappe.call({
				method: `${METHOD}delete_batch_inspection_report`,
				args: {batch_no: frm.docname},
				freeze: true,
				freeze_message: __("正在删除检测报告…"),
			});
			frappe.show_alert({message: __("检测报告已删除"), indicator: "green"});
			await frm.reload_doc();
		} finally {
			frm.__inspection_report_busy = false;
		}
	}
})();
