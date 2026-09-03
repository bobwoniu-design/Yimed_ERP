frappe.ui.form.on("JD Purchase Order", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("装箱工作台"), () => {
			frappe.route_options = { purchase_order: frm.doc.name };
			frappe.set_route("jd-packing-workbench");
		});
		frm.change_custom_button_type(__("装箱工作台"), null, "primary");
		if (frm.doc.total_cartons) {
			frm.add_custom_button(__("批量填写批号"), () => open_batch_dialog(frm), __("装箱"));
			frm.add_custom_button(__("批量确认装箱"), () => open_verify_dialog(frm), __("装箱"));
			frm.add_custom_button(__("批量打印箱贴"), () => print_carton_labels(frm), __("装箱"));
		}
		if (frm.doc.packing_status === "已装箱" && !frm.doc.stock_entry) {
			frm.add_custom_button(__("生成调拨单"), () => open_transfer_dialog(frm));
		}
		if (frm.doc.total_cartons && frm.doc.packing_status === "装箱中" && !frm.doc.stock_entry) {
			frm.add_custom_button(__("清空未确认装箱"), () => open_rework_dialog(frm, {
				title: __("清空未确认装箱"),
				method: "yimed_ecommerce.jd.rework.clear_unverified_cartons",
				warning: __("将删除本采购单全部未确认箱记录，删除后需要重新装箱。"),
			}), __("异常返工"));
		}
		if (frm.doc.packing_status === "已装箱" && !frm.doc.stock_entry) {
			frm.add_custom_button(__("撤销装箱确认"), () => open_rework_dialog(frm, {
				title: __("撤销装箱确认"),
				method: "yimed_ecommerce.jd.rework.undo_carton_verification",
				warning: __("将全部箱记录恢复为未确认状态，以便修改装箱明细。"),
			}), __("异常返工"));
		}
		if (frm.doc.transfer_status === "调拨草稿" && frm.doc.stock_entry) {
			frm.add_custom_button(__("删除调拨草稿并返工"), () => open_rework_dialog(frm, {
				title: __("删除调拨草稿并返工"),
				method: "yimed_ecommerce.jd.rework.delete_draft_transfer_and_rework",
				warning: __("仅能删除尚未提交的调拨草稿；已提交调拨必须在 ERPNext 中取消。"),
				extra_args: { stock_entry: frm.doc.stock_entry },
			}), __("异常返工"));
		}
	},
});

function open_rework_dialog(frm, options) {
	const dialog = new frappe.ui.Dialog({
		title: options.title,
		fields: [
			{ fieldname: "warning", fieldtype: "HTML", options: `<div class="alert alert-warning">${frappe.utils.escape_html(options.warning)}</div>` },
			{ fieldname: "reason", fieldtype: "Small Text", label: __("返工原因"), reqd: 1, max_length: 500 },
		],
		primary_action_label: __("确认返工"),
		primary_action(values) {
			frappe.call({
				method: options.method,
				args: { purchase_order: frm.doc.name, reason: values.reason, ...(options.extra_args || {}) },
				freeze: true,
				freeze_message: __("正在执行返工操作..."),
				callback: () => {
					dialog.hide();
					frm.reload_doc();
				},
			});
		},
	});
	dialog.show();
}

function open_equal_packing_dialog(frm) {
	const sku_options = (frm.doc.items || []).map((row) => row.jd_sku).filter(Boolean);
	const dialog = new frappe.ui.Dialog({
		title: __("单品等量快速装箱"),
		fields: [
			{ fieldname: "jd_sku", fieldtype: "Select", label: __("京东SKU"), options: sku_options, reqd: 1 },
			{ fieldname: "total_qty", fieldtype: "Float", label: __("本次装箱数量"), reqd: 1 },
			{ fieldname: "qty_per_carton", fieldtype: "Float", label: __("每箱数量"), reqd: 1 },
			{ fieldname: "start_sequence", fieldtype: "Int", label: __("起始箱号"), default: (frm.doc.total_cartons || 0) + 1, reqd: 1 },
			{ fieldname: "packing_date", fieldtype: "Date", label: __("装箱日期"), default: frappe.datetime.get_today(), reqd: 1 },
		],
		primary_action_label: __("批量生成箱记录"),
		primary_action(values) {
			frappe.call({
				method: "yimed_ecommerce.jd.packing.create_equal_cartons",
				args: { purchase_order: frm.doc.name, ...values },
				freeze: true,
				callback: () => {
					dialog.hide();
					frm.reload_doc();
				},
			});
		},
	});
	dialog.show();
}

function open_mixed_packing_dialog(frm) {
	const item_rows = (frm.doc.items || []).map((row) => ({
		include: 0,
		jd_sku: row.jd_sku,
		item_name: row.jd_item_name,
		qty: 0,
	}));
	const dialog = new frappe.ui.Dialog({
		title: __("混装模板批量装箱"),
		fields: [
			{
				fieldname: "items",
				fieldtype: "Table",
				label: __("每箱商品模板"),
				cannot_add_rows: true,
				in_place_edit: true,
				data: item_rows,
				fields: [
					{ fieldname: "include", fieldtype: "Check", label: __("装入"), in_list_view: 1 },
					{ fieldname: "jd_sku", fieldtype: "Data", label: __("京东SKU"), read_only: 1, in_list_view: 1 },
					{ fieldname: "item_name", fieldtype: "Data", label: __("商品名称"), read_only: 1, in_list_view: 1 },
					{ fieldname: "qty", fieldtype: "Float", label: __("每箱数量"), in_list_view: 1 },
				],
			},
			{ fieldname: "repeat_count", fieldtype: "Int", label: __("重复箱数"), reqd: 1 },
			{ fieldname: "start_sequence", fieldtype: "Int", label: __("起始箱号"), default: (frm.doc.total_cartons || 0) + 1, reqd: 1 },
			{ fieldname: "packing_date", fieldtype: "Date", label: __("装箱日期"), default: frappe.datetime.get_today(), reqd: 1 },
		],
		primary_action_label: __("批量生成混装箱"),
		primary_action(values) {
			const template_items = (values.items || [])
				.filter((row) => row.include && row.qty > 0)
				.map((row) => ({ jd_sku: row.jd_sku, qty: row.qty }));
			if (!template_items.length) {
				frappe.msgprint(__("请至少勾选一个商品并填写每箱数量。"));
				return;
			}
			frappe.call({
				method: "yimed_ecommerce.jd.packing.create_mixed_cartons",
				args: { purchase_order: frm.doc.name, template_items, repeat_count: values.repeat_count, start_sequence: values.start_sequence, packing_date: values.packing_date },
				freeze: true,
				callback: () => {
					dialog.hide();
					frm.reload_doc();
				},
			});
		},
	});
	dialog.show();
}

function sequence_fields(frm) {
	return [
		{ fieldname: "start_sequence", fieldtype: "Int", label: __("起始箱号"), default: 1 },
		{ fieldname: "end_sequence", fieldtype: "Int", label: __("结束箱号"), default: frm.doc.total_cartons },
	];
}

function open_batch_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("批量填写批号"),
		fields: [
			{ fieldname: "jd_sku", fieldtype: "Data", label: __("京东SKU（可选）") },
			{ fieldname: "stock_item", fieldtype: "Link", label: __("实际库存物料"), options: "Item", reqd: 1 },
			{ fieldname: "batch_no", fieldtype: "Link", label: __("批号"), options: "Batch", reqd: 1, get_query: () => ({ filters: { item: dialog.get_value("stock_item"), disabled: 0 } }) },
			...sequence_fields(frm),
			{ fieldname: "only_empty", fieldtype: "Check", label: __("仅填写空白批号"), default: 1 },
		],
		primary_action_label: __("应用到箱明细"),
		primary_action(values) {
			frappe.call({
				method: "yimed_ecommerce.jd.packing.assign_batch_to_cartons",
				args: { purchase_order: frm.doc.name, ...values },
				freeze: true,
				callback: () => {
					dialog.hide();
					frm.reload_doc();
				},
			});
		},
	});
	dialog.show();
}

function open_verify_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("批量确认装箱"),
		fields: sequence_fields(frm),
		primary_action_label: __("校验有效期并确认"),
		primary_action(values) {
			frappe.call({
				method: "yimed_ecommerce.jd.packing.verify_cartons",
				args: { purchase_order: frm.doc.name, ...values },
				freeze: true,
				callback: () => {
					dialog.hide();
					frm.reload_doc();
				},
			});
		},
	});
	dialog.show();
}

function print_carton_labels(frm) {
	frappe.call({
		method: "yimed_ecommerce.jd.packing.get_carton_names",
		args: { purchase_order: frm.doc.name },
		callback: (response) => {
			const names = response.message || [];
			if (!names.length) return;
			const params = new URLSearchParams({
				doctype: "JD Carton",
				name: JSON.stringify(names),
				format: "JD Carton Label",
				no_letterhead: "1",
				options: JSON.stringify({
					"page-width": "59.8mm",
					"page-height": "71mm",
					"margin-top": "1.5mm",
					"margin-bottom": "1.5mm",
					"margin-left": "1.5mm",
					"margin-right": "1.5mm",
				}),
			});
			window.open(`/api/method/frappe.utils.print_format.download_multi_pdf?${params.toString()}`);
		},
	});
}

function open_transfer_dialog(frm) {
	frappe.call({
		method: "yimed_ecommerce.jd.transfer.get_transfer_defaults",
		args: { purchase_order: frm.doc.name },
		callback: (response) => show_transfer_dialog(frm, response.message || {}),
	});
}

function show_transfer_dialog(frm, defaults) {
	const route_locked = defaults.configured && !defaults.allow_warehouse_override;
	const dialog = new frappe.ui.Dialog({
		title: __("生成调拨单"),
		fields: [
			{ fieldname: "transfer_mode", fieldtype: "Select", label: __("调拨方式"), options: ["一步调拨", "两步调拨"], default: defaults.default_transfer_mode || "一步调拨", read_only: route_locked, reqd: 1 },
			{ fieldname: "source_warehouse", fieldtype: "Link", label: __("发货仓"), options: "Warehouse", default: defaults.default_source_warehouse, read_only: route_locked, reqd: 1, get_query: () => warehouse_query(frm.doc.company) },
			{ fieldname: "target_warehouse", fieldtype: "Link", label: __("京东自营仓"), options: "Warehouse", default: defaults.default_target_warehouse, read_only: route_locked, reqd: 1, get_query: () => warehouse_query(frm.doc.company) },
			{ fieldname: "transit_warehouse", fieldtype: "Link", label: __("在途仓"), options: "Warehouse", default: defaults.default_transit_warehouse, read_only: route_locked, depends_on: "eval:doc.transfer_mode === '两步调拨'", mandatory_depends_on: "eval:doc.transfer_mode === '两步调拨'", get_query: () => warehouse_query(frm.doc.company) },
			{ fieldname: "posting_date", fieldtype: "Date", label: __("记账日期"), default: frappe.datetime.get_today(), reqd: 1 },
		],
		primary_action_label: __("生成草稿"),
		primary_action(values) {
			frappe.call({
				method: "yimed_ecommerce.jd.transfer.create_transfer",
				args: { purchase_order: frm.doc.name, ...values },
				freeze: true,
				callback: (response) => {
					dialog.hide();
					if (response.message) frappe.set_route("Form", "Stock Entry", response.message);
				},
			});
		},
	});
	dialog.show();
}

function warehouse_query(company) {
	return { filters: { company, is_group: 0, disabled: 0 } };
}
