const JD_IMPORT_READY_STATUSES = ["导入成功", "部分失败"];
const JD_IMPORT_WORKBOOK_METHOD = "yimed_ecommerce.jd.import_workbooks";

function download_jd_import_template() {
	open_url_post(frappe.request.url, {
		cmd: `${JD_IMPORT_WORKBOOK_METHOD}.download_import_template`,
	});
}

function download_jd_failed_details(batch_name) {
	open_url_post(frappe.request.url, {
		cmd: `${JD_IMPORT_WORKBOOK_METHOD}.download_failed_details`,
		batch_name,
	});
}

function is_jd_import_ready(doc) {
	return JD_IMPORT_READY_STATUSES.includes(doc.status);
}

function get_single_failed_batch(listview) {
	const selected = listview.get_checked_items();
	if (selected.length !== 1) {
		frappe.msgprint(__("请只选择一个采购单导入批次后再下载失败明细。"));
		return null;
	}
	if (!selected[0].failed_count) {
		frappe.msgprint(__("该批次没有失败明细。"));
		return null;
	}
	return selected[0];
}

function open_jd_purchase_workbench(doc) {
	if (!is_jd_import_ready(doc)) {
		frappe.msgprint({
			title: __("暂不能进入工作台"),
			message: __("批次 {0} 当前状态为“{1}”。只有“导入成功”或“部分失败”的批次可以进入工作台。", [
				doc.name,
				doc.status || __("未设置"),
			]),
			indicator: "orange",
		});
		return;
	}
	window.location.assign(
		`/desk/jd-purchase-workbench?import_batch=${encodeURIComponent(doc.name)}`
	);
}

async function show_jd_import_dialog(listview) {
	const batch_code = await frappe.xcall(
		"yimed_ecommerce.yimed_ecommerce.doctype.jd_purchase_import_batch.jd_purchase_import_batch.get_default_batch_code"
	);
	const dialog = new frappe.ui.Dialog({
		title: __("创建采购单导入批次"),
		fields: [
			{
				fieldname: "batch_code",
				fieldtype: "Data",
				label: __("批次号"),
				default: batch_code,
				reqd: 1,
				description: __("默认按年月日和流水号生成，创建前可以修改。"),
			},
			{
				fieldname: "company",
				fieldtype: "Link",
				options: "Company",
				label: __("公司"),
				default: frappe.defaults.get_user_default("Company"),
				reqd: 1,
			},
			{
				fieldname: "file_url",
				fieldtype: "Attach",
				label: __("采购单文件"),
				reqd: 1,
				description: __("可直接上传京东后台导出的采购订单明细；也可先下载标准模板填写。"),
			},
		],
		primary_action_label: __("创建批次"),
		primary_action(values) {
			frappe.call({
				method:
					"yimed_ecommerce.jd.import_purchase_orders.validate_purchase_orders_file",
				args: { file_url: values.file_url },
				freeze: true,
				freeze_message: __("正在校验采购单文件..."),
				callback: (response) => {
					if (!response.message) return;
					const result = response.message;
					const failed = result.failed_count || 0;
					dialog.hide();
					frappe.route_options = {
						batch_code: values.batch_code,
						company: values.company,
						import_file: values.file_url,
						status: failed ? "导入失败" : "待导入",
						purchase_order_count: result.total || 0,
						item_row_count: result.item_row_count || 0,
						success_count: result.success_count || 0,
						failed_count: failed,
						error_log: (result.errors || []).join("\\n"),
					};
					frappe.set_route("Form", "JD Purchase Import Batch", "new");
					frappe.show_alert({
						message: failed
							? __("预检完成：共 {0} 条采购单，失败 {1} 条。失败数量和原因已带入批次表单。", [result.total || 0, failed])
							: __("文件共 {0} 条采购单，全部可导入。", [result.total || 0]),
						indicator: failed ? "red" : "green",
					});
				},
			});
		},
	});
	dialog.show();
}

frappe.listview_settings["JD Purchase Import Batch"] = {
	add_fields: ["status", "failed_count"],

	onload(listview) {
		listview.settings.primary_action = () => show_jd_import_dialog(listview);
		// 覆盖列表页右上角标准「新建」按钮：改名「采购单导入」，点击打开导入对话框
		listview.set_primary_action = () => {
			listview.page.set_primary_action(__("采购单导入"), () => show_jd_import_dialog(listview), "add");
		};
		listview.set_primary_action();
		listview.page.add_inner_button(__("下载导入模板"), download_jd_import_template);

		// Some Desk list layouts attach the row navigation listener before the
		// delegated ListView action handler. Intercept this one action during the
		// capture phase so clicking it never opens the batch form by accident.
		const result_element = listview.$result?.get(0);
		if (result_element && !result_element.dataset.jdWorkbenchHandler) {
			result_element.dataset.jdWorkbenchHandler = "1";
			result_element.addEventListener(
				"click",
				(event) => {
					const button = event.target.closest(".btn-action[data-name]");
					if (!button || !result_element.contains(button)) return;
					event.preventDefault();
					event.stopPropagation();
					event.stopImmediatePropagation();
					const doc = listview.data.find((row) => row.name === button.dataset.name);
					if (doc) open_jd_purchase_workbench(doc);
				},
				true
			);
		}

		listview.page.add_action_item(__("下载失败明细"), () => {
			const batch = get_single_failed_batch(listview);
			if (batch) download_jd_failed_details(batch.name);
		});
	},

	button: {
		show() {
			return true;
		},
		get_label() {
			return __("进入工作台");
		},
		get_description(doc) {
			return is_jd_import_ready(doc)
				? __("进入批次 {0} 的京东采购备货工作台", [doc.name])
				: __("当前状态“{0}”暂不能进入工作台", [doc.status || __("未设置")]);
		},
		action(doc) {
			open_jd_purchase_workbench(doc);
		},
	},

	get_indicator(doc) {
		const indicators = {
			待导入: [__("待导入"), "gray", "status,=,待导入"],
			导入中: [__("导入中"), "blue", "status,=,导入中"],
			导入成功: [__("导入成功"), "green", "status,=,导入成功"],
			部分失败: [__("部分失败"), "orange", "status,=,部分失败"],
			导入失败: [__("导入失败"), "red", "status,=,导入失败"],
		};
		return indicators[doc.status];
	},
};
