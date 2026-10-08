frappe.ui.form.on("JD Purchase Import Batch", {
	async onload(frm) {
		if (frm.is_new() && !frm.doc.batch_code) {
			const batch_code = await frappe.xcall(
				"yimed_ecommerce.yimed_ecommerce.doctype.jd_purchase_import_batch.jd_purchase_import_batch.get_default_batch_code"
			);
			await frm.set_value("batch_code", batch_code);
		}
	},

	refresh(frm) {
		frm.set_df_property("batch_code", "read_only", !frm.is_new());
		frm.add_custom_button(__("下载导入模板"), () => {
			open_url_post(frappe.request.url, {
				cmd: "yimed_ecommerce.jd.import_workbooks.download_import_template",
			});
		}, __("导入工具"));
		if (!frm.is_new() && frm.doc.failed_count) {
			frm.add_custom_button(__("下载失败明细"), () => {
				open_url_post(frappe.request.url, {
					cmd: "yimed_ecommerce.jd.import_workbooks.download_failed_details",
					batch_name: frm.doc.name,
				});
			}, __("导入工具"));
		}
		if (!frm.is_new() && frm.doc.import_file && ["待导入", "部分失败", "导入失败"].includes(frm.doc.status)) {
			frm.add_custom_button(__("采购单导入"), () => {
				frappe.call({
					method: "yimed_ecommerce.jd.import_purchase_orders.run_import_by_name",
					args: { batch_name: frm.doc.name },
					freeze: true,
					freeze_message: __("正在导入采购单..."),
				}).then((r) => {
					if (r.exc) return;
					const result = r.message || {};
					const failed = result.failed_count || 0;
					if (failed) {
						frappe.msgprint({
							title: __("导入失败"),
							message: __("全部 {0} 条采购单均未导入。原因：{1}", [
								failed,
								result.error || __("未知"),
							]),
							indicator: "red",
						});
						frm.reload_doc();
					} else {
						frappe.show_alert({
							message: __("导入成功：共 {0} 条采购单，正在进入工作台...", [result.success_count || 0]),
							indicator: "green",
						});
						// 导入成功后直接进入该批次的采购备货工作台，省去手动回列表查找
						setTimeout(() => {
							window.location.assign(
								`/app/jd-purchase-workbench?import_batch=${encodeURIComponent(frm.doc.name)}`
							);
						}, 1000);
					}
				});
			}).addClass("btn-primary");
		}
		if (!frm.is_new() && ["导入成功", "部分失败"].includes(frm.doc.status)) {
			frm.add_custom_button(__("商品备货汇总"), () => {
				frappe.set_route("query-report", "JD Stocking Summary", { import_batch: frm.doc.name });
			}, __("汇总与打印"));
			frm.add_custom_button(__("分仓汇总"), () => {
				frappe.set_route("query-report", "JD Warehouse Summary", { import_batch: frm.doc.name });
			}, __("汇总与打印"));
		}
	},
});
