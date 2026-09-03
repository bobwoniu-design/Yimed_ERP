frappe.ui.form.on("Jackyun Connection", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("测试连接"), () => test_connection(frm));
		frm.add_custom_button(__("数据核对"), () => run_reconciliation(frm), __("运行与治理"));
		frm.add_custom_button(__("同步预演"), () => preview_sync(frm), __("运行与治理"));
		frm.add_custom_button(__("同步全部"), () => enqueue_sync(frm, "All"), __("立即同步"));
		frm.add_custom_button(__("选择类型"), async () => {
			const response = await frappe.call({
				method: "channel_erp.integrations.tasks.get_sync_resources",
			});
			const options = (response.message || []).map((row) => row.value).join("\n");
			frappe.prompt(
				[{fieldname: "resource", fieldtype: "Select", label: __("同步类型"), options, reqd: 1}],
				(values) => enqueue_sync(frm, values.resource),
				__("选择吉客云同步类型"),
				__("开始同步")
			);
		}, __("立即同步"));
	},
});

async function run_reconciliation(frm) {
	const response = await frappe.call({
		method: "channel_erp.integrations.connector_operations.run_reconciliation",
		args: {connection: frm.doc.name}, freeze: true, freeze_message: __("正在核对数据…"),
	});
	const result = response.message || {};
	frappe.set_route("Form", "Integration Reconciliation Run", result.name);
}

async function preview_sync(frm) {
	const resources = await frappe.call({method: "channel_erp.integrations.tasks.get_sync_resources"});
	frappe.prompt([
		{fieldname: "resource", fieldtype: "Select", label: __("同步类型"), options: (resources.message || []).map((r) => r.value).join("\n"), reqd: 1},
		{fieldname: "limit", fieldtype: "Int", label: __("抽样数量"), default: 20, reqd: 1},
	], async (values) => {
		const response = await frappe.call({
			method: "channel_erp.integrations.connector_operations.preview_pull",
			args: {connection: frm.doc.name, ...values}, freeze: true, freeze_message: __("正在只读预演…"),
		});
		frappe.set_route("Form", "Integration Reconciliation Run", response.message.name);
	}, __("同步预演（不写入）"), __("开始预演"));
}

async function test_connection(frm) {
	const response = await frappe.call({
		method: "channel_erp.jackyun_integration.doctype.jackyun_connection.jackyun_connection.test_connection",
		args: {
			name: frm.doc.name,
			app_key: frm.doc.app_key,
			app_secret: frm.doc.app_secret,
			gateway: frm.doc.gateway,
		},
		freeze: true,
		freeze_message: __("正在测试连接…"),
	});
	const result = response.message || {};
	frappe.msgprint({
		title: result.ok ? __("连接成功") : __("连接失败"),
		indicator: result.ok ? "green" : "red",
		message: result.message || __("未知错误"),
	});
}

async function enqueue_sync(frm, resource) {
	const response = await frappe.call({
		method: "channel_erp.integrations.tasks.enqueue_manual_sync",
		args: {connection_name: frm.doc.name, resource},
		freeze: true,
		freeze_message: __("正在加入同步队列…"),
	});
	const queued = response.message?.queued || [];
	const skipped = response.message?.skipped || [];
	frappe.msgprint({
		title: __("同步任务已处理"),
		indicator: queued.length ? "green" : "orange",
		message: [
			queued.length ? __("已排队：{0}", [queued.join("、")]) : __("没有新增任务"),
			skipped.length ? __("正在运行或已排队：{0}", [skipped.join("、")]) : "",
		].filter(Boolean).join("<br>"),
	});
}
