const JD_PURCHASE_WORKBENCH_CONTRACT = Object.freeze({
	load_overview: "yimed_ecommerce.jd.workflow.get_workflow_overview",
	load_order: "yimed_ecommerce.jd.workflow.get_workflow_purchase_order",
	load_stocking_pool: "yimed_ecommerce.jd.workflow.get_erp_stocking_summary",
	load_stocking_workbench: "yimed_ecommerce.jd.workflow.get_stocking_workbench",
	load_sorting_matrix: "yimed_ecommerce.jd.workflow.get_sorting_matrix",
	batch_candidates: "yimed_ecommerce.jd.workflow.get_batch_candidates",
	save_stocking: "yimed_ecommerce.jd.workflow.confirm_stocking",
	auto_allocate: "yimed_ecommerce.jd.workflow.auto_sort",
	save_allocations: "yimed_ecommerce.jd.workflow.update_sorting",
});

frappe.pages["jd-purchase-workbench"].on_page_load = (wrapper) => {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("京东采购备货工作台"), single_column: true });
	wrapper.jd_purchase_workbench = new JDPurchaseWorkbench(wrapper, page);
};

frappe.pages["jd-purchase-workbench"].on_page_show = (wrapper) => {
	wrapper.jd_purchase_workbench?.show();
};
frappe.pages["jd-purchase-workbench"].on_page_hide = (wrapper) => wrapper.jd_purchase_workbench?.cleanup_packing_component();

class JDWorkbenchAdapter {
	async load_batch(batch_name) {
		return frappe.xcall(JD_PURCHASE_WORKBENCH_CONTRACT.load_overview, { import_batch: batch_name });
	}

	async load_order(order_name) {
		const workflow = await frappe.xcall(JD_PURCHASE_WORKBENCH_CONTRACT.load_order, { purchase_order: order_name });
		return { order: workflow.purchase_order, workflow };
	}

	async load_workflow_order(order_name) {
		return frappe.xcall(JD_PURCHASE_WORKBENCH_CONTRACT.load_order, { purchase_order: order_name });
	}

	async get_stocking_pool(batch_name) {
		const data = await frappe.xcall(JD_PURCHASE_WORKBENCH_CONTRACT.load_stocking_workbench, { import_batch: batch_name });
		const pools = data.pool_rows || [];
		const items = [];
		for (const requirement of data.requirements || []) {
			const matching = pools.filter((row) => row.stock_item === requirement.stock_item);
			if (!matching.length) items.push({ ...requirement, stocked_qty: requirement.required_qty, _split: false });
			else matching.forEach((row, index) => items.push({ ...requirement, ...row, required_qty: requirement.required_qty, _split: index > 0 }));
		}
		return { ...data, items };
	}

	async get_sorting_matrix(batch_name) {
		return frappe.xcall(JD_PURCHASE_WORKBENCH_CONTRACT.load_sorting_matrix, { import_batch: batch_name });
	}

	async get_default_warehouse(company) {
		if (!company) return "";
		try {
			const result = await frappe.db.get_value("JD Self Operated Settings", { company }, "default_source_warehouse");
			return result?.message?.default_source_warehouse || "";
		} catch (error) { return ""; }
	}
}

class JDPurchaseWorkbench {
	constructor(wrapper, page) {
		this.wrapper = wrapper;
		this.page = page;
		this.adapter = new JDWorkbenchAdapter();
		this.state = { batch: null, orders: [], order: null, workflow: null, packing: null, stocking: null, sorting_matrix: null, sorting_order_filter: "", default_warehouse: "", step_counts: {}, exception_count: 0, step: "import" };
		this.$root = $('<div class="jd-purchase-workbench"></div>').appendTo(page.main.empty());
		this.make_toolbar();
		this.bind_events();
	}

	make_toolbar() {
		this.packing_toolbar = {
			auto: this.page.add_inner_button(__("整箱自动生成"), () => this.packing_component?.generate_suggestions()),
			batch: this.page.add_inner_button(__("批量填写批号"), () => this.packing_component?.open_batch_dialog()),
			verify: this.page.add_inner_button(__("确认装箱"), () => this.packing_component?.open_verify_dialog()),
			print: this.page.add_inner_button(__("打印箱贴"), () => this.packing_component?.print_labels()),
		};
		this.page.add_menu_item(__("商品备货汇总 PDF"), () => this.download_stocking_pdf(), false, false, true);
		this.page.add_menu_item(__("分仓/分拣汇总 PDF"), () => this.download_warehouse_pdf(), false, false, true);
		this.page.add_menu_item(__("导出与打印：箱贴"), () => this.print_carton_labels(), false, false, true);
		this.page.add_menu_item(__("创建上门交接单"), () => this.create_handover(), false, false, true);
		this.toggle_packing_toolbar(false);
	}

	bind_events() {
		this.$root.on("click", "[data-step]", async (event) => {
			this.state.step = $(event.currentTarget).attr("data-step");
			if (this.state.step === "sorting") {
				try { await this.load_sorting_rows(); }
				catch (error) { this.show_action_error(error); return; }
			}
			this.render_steps_and_content();
		});
		this.$root.on("change", "[data-sorting-order-filter]", (event) => {
			this.state.sorting_order_filter = $(event.currentTarget).val() || "";
			this.render_steps_and_content();
		});
		this.$root.on("change", "[data-packing-order-filter]", (event) => this.select_order($(event.currentTarget).val()));
		this.$root.on("click", "[data-open-order]", (event) => frappe.set_route("Form", "JD Purchase Order", $(event.currentTarget).attr("data-open-order")));
		this.$root.on("click", "[data-action='open-order']", () => this.state.order && frappe.set_route("Form", "JD Purchase Order", this.state.order.name));
		this.$root.on("click", "[data-action='packing-workbench']", () => this.open_packing_workbench());
		this.$root.on("click", "[data-action='open-handovers']", () => frappe.set_route("List", "JD Handover"));
		this.$root.on("click", "[data-action='fefo-row']", (event) => this.choose_fefo_batch(event));
		this.$root.on("click", "[data-action='split-stocking-row']", (event) => this.split_stocking_row(event));
		this.$root.on("click", "[data-action='remove-stocking-row']", (event) => this.remove_stocking_row(event));
		this.$root.on("click", "[data-action='save-stocking']", () => this.confirm_stocking());
		this.$root.on("click", "[data-action='auto-allocate']", () => this.auto_sort());
		this.$root.on("click", "[data-action='save-allocations']", () => this.save_sorting());
	}

	async show() {
		const requested = frappe.route_options?.import_batch || new URLSearchParams(window.location.search).get("import_batch");
		if (frappe.route_options?.import_batch) delete frappe.route_options.import_batch;
		if (!requested) {
			frappe.set_route("List", "JD Purchase Import Batch");
			return;
		}
		this.render_loading();
		try {
			await this.select_batch(requested);
		} catch (error) {
			this.render_error(error);
		}
	}

	async select_batch(batch_name) {
		if (!batch_name) return;
		this.cleanup_packing_component();
		this.$root.html(this.loading_block(__("正在加载批次")));
		try {
			const data = await this.adapter.load_batch(batch_name);
			this.state.batch = data.batch;
			this.state.orders = data.orders;
			this.state.step_counts = data.step_counts || {};
			this.state.exception_count = data.exception_count || 0;
			this.state.order = null;
			this.state.packing = null;
			this.state.workflow = null;
			this.state.sorting_matrix = null;
			this.state.sorting_order_filter = "";
			const [stocking, default_warehouse] = await Promise.all([this.adapter.get_stocking_pool(batch_name), this.adapter.get_default_warehouse(data.batch.company)]);
			this.state.default_warehouse = default_warehouse;
			stocking.items.forEach((row) => { if (!row.warehouse) row.warehouse = default_warehouse; });
			this.state.stocking = stocking;
			this.state.step = "import";
			this.update_page_title();
			this.render_flow();
			if (data.orders.length) await this.select_order(data.orders[0].name, false);
		} catch (error) {
			this.render_flow_error(error);
		}
	}

	render_flow() {
		this.$root.html(`<nav class="jd-pw-steps">${this.render_steps()}</nav><div class="jd-pw-step-content"></div>`);
		this.render_steps_and_content();
	}

	order_options(selected = this.state.order?.name, include_all = false) {
		if (!this.state.orders.length) return `<option value="">${__("本批次没有采购单")}</option>`;
		const all = include_all ? `<option value="" ${!selected ? "selected" : ""}>${__("全部采购单")}</option>` : "";
		return all + this.state.orders.map((order) => `<option value="${this.escape(order.name)}" ${selected === order.name ? "selected" : ""}>${this.escape(order.name)} · ${this.escape(order.destination_city || order.jd_warehouse || "")}</option>`).join("");
	}

	order_stage(order) {
		if (order.workflow_stage) return order.workflow_stage;
		if (order.transfer_status && order.transfer_status !== "待调拨") return __("交接发运");
		if (order.packing_status === "已装箱") return __("装箱完成");
		if (order.packing_status === "装箱中") return __("装箱中");
		return order.status || __("待备货");
	}

	render_steps() {
		const steps = [["import", "采购单导入"], ["stocking", "备货"], ["sorting", "分拣"], ["packing", "装箱"], ["handover", "交接发运"]];
		return steps.map(([key, label], index) => `<button data-step="${key}" class="${this.state.step === key ? "is-active" : ""}"><i>${index + 1}</i><span><strong>${__(label)}</strong><small>${this.escape(this.step_progress(key))}</small></span></button>`).join("");
	}

	step_progress(step) {
		const total = this.state.orders.length;
		if (step === "import") return __("共 {0} 单", [total]);
		const completed = {
			stocking: this.state.orders.filter((row) => row.stocking_status === "备货完成").length,
			sorting: this.state.orders.filter((row) => row.sorting_status === "分拣完成").length,
			packing: this.state.orders.filter((row) => row.packing_status === "已装箱").length,
			handover: this.state.orders.filter((row) => ["已调拨", "在途", "已完成"].includes(row.transfer_status)).length,
		};
		return `${completed[step] || 0}/${total}`;
	}

	update_page_title() {
		const batch = this.state.batch;
		if (!batch) return;
		const title = `${__("京东采购备货")} / ${batch.name}`;
		this.page.set_title(__("京东采购备货"));
		this.page.set_indicator(batch.status || __("未知状态"), this.status_tone(batch.status));
		frappe.breadcrumbs.add({ type: "Custom", label: __("京东采购备货"), route: "List/JD Purchase Import Batch" });
		frappe.breadcrumbs.append_breadcrumb_element(
			`/desk/jd-purchase-import-batch/${encodeURIComponent(batch.name)}`,
			this.escape(batch.name),
			"jd-pw-title-batch"
		);
		frappe.utils.set_title(title);
		this.page.get_title_area().off("click.jd-pw-title").on("click.jd-pw-title", ".jd-pw-title-batch", (event) => {
			event.preventDefault();
			frappe.set_route("Form", "JD Purchase Import Batch", batch.name);
		});
	}

	status_tone(status) {
		if (["导入成功", "已完成"].includes(status)) return "green";
		if (["部分失败", "处理中"].includes(status)) return "orange";
		if (["导入失败", "失败"].includes(status)) return "red";
		return "gray";
	}

	async select_order(order_name, rerender = true) {
		if (!order_name) return;
		this.$root.find(".jd-pw-step-content").html(this.loading_block(__("正在加载采购单")));
		try {
			const data = await this.adapter.load_order(order_name);
			this.state.order = data.order;
			this.state.packing = data.packing;
			this.state.workflow = data.workflow;
			this.render_steps_and_content();
		} catch (error) {
			this.render_flow_error(error);
		}
	}

	render_steps_and_content() {
		this.toggle_packing_toolbar(this.state.step === "packing");
		const can_mount_packing = this.packing_gate_allows();
		if (this.state.step !== "packing" || !can_mount_packing) this.cleanup_packing_component();
		this.$root.find(".jd-pw-steps").html(this.render_steps());
		const renderers = { import: () => this.render_import(), stocking: () => this.render_stocking(), sorting: () => this.render_sorting(), packing: () => this.render_packing(), handover: () => this.render_handover() };
		this.$root.find(".jd-pw-step-content").html(renderers[this.state.step]?.() || "");
		if (this.state.step === "stocking") this.hydrate_stocking_warehouse_links();
		if (this.state.step === "packing" && this.state.order && can_mount_packing) this.mount_packing_component();
	}

	render_import() {
		const rows = this.state.orders.map((order) => `<tr><td><button class="btn btn-link btn-xs jd-pw-order-link" data-open-order="${this.escape(order.name)}">${this.escape(order.name)}</button></td><td>${this.escape(order.destination_city || "—")}</td><td>${this.escape(order.jd_warehouse || "—")}</td><td>${this.format(order.total_purchase_qty)}</td><td>${this.escape(order.mapping_status || "—")}</td><td>${this.escape(order.stocking_status || "—")}</td><td>${this.escape(order.sorting_status || "—")}</td><td>${this.escape(order.packing_status || "—")}</td><td>${this.escape(order.transfer_status || "—")}</td><td>${this.badge(this.order_stage(order))}</td></tr>`).join("");
		const table = rows ? `<div class="table-responsive"><table class="table jd-pw-order-table"><thead><tr><th>${__("京东采购单")}</th><th>${__("目的城市")}</th><th>${__("京东仓")}</th><th>${__("采购数量")}</th><th>${__("映射")}</th><th>${__("备货")}</th><th>${__("分拣")}</th><th>${__("装箱")}</th><th>${__("调拨")}</th><th>${__("当前环节")}</th></tr></thead><tbody>${rows}</tbody></table></div>` : this.empty_step(__("本批次没有采购单。"));
		return `<div class="jd-pw-flat-section"><div class="jd-pw-inline-summary"><strong>${__("采购单清单")}</strong><span>${this.escape(this.state.batch.company || "")}</span><span>${__("共 {0} 单", [this.state.orders.length])}</span><span class="${this.state.exception_count ? "text-danger" : "text-muted"}">${__("流程异常 {0}", [this.state.exception_count])}</span></div>${table}</div>`;
	}

	render_stocking() {
		if (!this.state.stocking) return this.empty_step(__("正在加载批次备货数据。"));
		const items = this.state.stocking.items || [];
		if (!items.length) return this.empty_step(__("当前批次尚无可备货的ERP实物物料。请确认SKU映射及组合装配置。"));
		const rows = items.map((row, index) => this.stocking_row_html(row, index, Boolean(row._split))).join("");
		return `<div class="jd-pw-card"><div class="jd-pw-card-head"><div><h3>${__("批次级实际物料备货")}</h3><span>${this.escape(this.state.batch.name)} · ${__("同一物料可按实际取货拆分到多个仓库或批号")}</span></div><button class="btn btn-primary btn-sm" data-action="save-stocking">${__("确认批次备货")}</button></div><div class="table-responsive"><table class="table"><thead><tr><th>${__("ERP库存物料")}</th><th>${__("批次需求")}</th><th>${__("仓库")}</th><th>${__("批号/FEFO")}</th><th>${__("实际备货")}</th><th>${__("短缺")}</th><th>${__("短缺原因")}</th><th>${__("操作")}</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
	}

	stocking_row_html(row, index, split = false) {
		return `<tr data-stocking-row data-index="${index}" data-stock-item="${this.escape(row.stock_item)}" data-required-qty="${this.escape(row.required_qty || 0)}" data-split="${split ? 1 : 0}"><td><strong>${split ? "↳ " : ""}${this.escape(row.stock_item)}</strong><small>${this.escape(row.stock_item_name || "")}</small></td><td>${split ? "—" : this.format(row.required_qty)}</td><td><div data-warehouse-control data-value="${this.escape(row.warehouse || this.state.default_warehouse || "")}"></div></td><td><div class="jd-pw-inline-control"><input data-stocking-field="batch_no" class="form-control input-xs" value="${this.escape(row.batch_no || "")}" placeholder="${__("批号")}"><button class="btn btn-default btn-xs" data-action="fefo-row">${__("FEFO")}</button></div></td><td><input data-stocking-field="stocked_qty" type="number" min="0" step="any" class="form-control input-xs" value="${this.escape(row.stocked_qty || 0)}"></td><td>${split ? "—" : this.format(row.shortage_qty || 0)}</td><td><input data-stocking-field="shortage_reason" class="form-control input-xs" value="${this.escape(row.shortage_reason || "")}" placeholder="${__("有短缺时必填")}"></td><td><div class="jd-pw-row-actions">${split ? `<button class="btn btn-default btn-xs" data-action="remove-stocking-row">${__("删除")}</button>` : `<button class="btn btn-default btn-xs" data-action="split-stocking-row">${__("拆分")}</button>`}</div></td></tr>`;
	}

	hydrate_stocking_warehouse_links($scope = this.$root) {
		$scope.find("[data-warehouse-control]").each((_, element) => {
			const $host = $(element);
			if ($host.data("control")) return;
			const control = frappe.ui.form.make_control({ parent: $host, render_input: true, df: { fieldtype: "Link", options: "Warehouse", placeholder: __("仓库"), get_query: () => ({ filters: { company: this.state.batch?.company } }) } });
			control.set_value($host.attr("data-value") || "");
			$host.data("control", control);
		});
	}

	render_sorting() {
		if (!this.state.stocking) return this.empty_step(__("正在加载批次备货池。"));
		return `<div class="jd-pw-card"><div class="jd-pw-card-head"><div><h3>${__("批次备货池分拣矩阵")}</h3><span>${__("将已备实物跨采购单分配；自动分配后可人工调整现有分配")}</span></div><div><label class="jd-pw-order-filter">${__("采购单筛选")}<select class="form-control input-sm" data-sorting-order-filter>${this.order_options(this.state.sorting_order_filter, true)}</select></label><button class="btn btn-default btn-sm" data-action="auto-allocate">${__("跨采购单自动分配")}</button><button class="btn btn-primary btn-sm" data-action="save-allocations">${__("保存分拣矩阵")}</button></div></div>${this.sorting_matrix()}</div>`;
	}

	pool_rows() {
		const rows = this.state.stocking?.items || [];
		return rows.length ? rows.map((row) => `<div class="jd-pw-pool-row"><strong>${this.escape(row.stock_item)}</strong><span>${this.escape(row.warehouse || "—")} / ${this.escape(row.batch_no || "—")}</span><b>${this.format(row.available_qty ?? row.stocked_qty)}</b></div>`).join("") : this.empty_step(__("备货池暂无可分配数据"));
	}

	sorting_matrix() {
		const matrix = this.state.sorting_matrix;
		if (!matrix?.pools?.length) return this.empty_step(__("备货池暂无实物库存，请先确认批次备货。"));
		const allocations = matrix.allocations || [];
		const allocation_map = new Map(allocations.map((row) => [`${row.stocking_pool_item}\u0000${row.purchase_order}\u0000${row.jd_sku}\u0000${row.stock_item}`, row]));
		const rows = [];
		for (const pool of matrix.pools) {
			const skus = [...new Set((matrix.requirements || []).filter((row) => row.stock_item === pool.stock_item).map((row) => row.jd_sku))];
			for (const jd_sku of skus) rows.push({ ...pool, jd_sku });
		}
		const visible_orders = this.state.sorting_order_filter ? this.state.orders.filter((order) => order.name === this.state.sorting_order_filter) : this.state.orders;
		const headers = visible_orders.map((order) => `<th title="${this.escape(order.name)}">${this.escape(order.name)}</th>`).join("");
		const body = rows.map((row) => `<tr><td><strong>${this.escape(row.stock_item)}</strong><small>${this.escape(row.jd_sku)} · ${this.escape(row.warehouse || "—")} / ${this.escape(row.batch_no || "—")}</small></td><td>${this.format(row.stocked_qty)}</td>${visible_orders.map((order) => { const requirement = (matrix.requirements || []).find((item) => item.purchase_order === order.name && item.jd_sku === row.jd_sku && item.stock_item === row.stock_item); if (!requirement) return `<td class="text-muted">—</td>`; const existing = allocation_map.get(`${row.name}\u0000${order.name}\u0000${row.jd_sku}\u0000${row.stock_item}`); return `<td><input data-sorting-row type="number" min="0" step="any" class="form-control input-xs" data-po="${this.escape(order.name)}" data-pool="${this.escape(row.name)}" data-jd-sku="${this.escape(row.jd_sku)}" data-stock-item="${this.escape(row.stock_item)}" value="${this.escape(existing?.sorted_qty || 0)}"><small>${__("需 {0}", [this.format(requirement.required_qty)])}</small></td>`; }).join("")}</tr>`).join("");
		return `<div class="table-responsive jd-pw-matrix"><table class="table"><thead><tr><th>${__("ERP物料 / 京东SKU / 批号")}</th><th>${__("池数量")}</th>${headers}</tr></thead><tbody>${body}</tbody></table></div>`;
	}

	async load_sorting_rows() {
		if (!this.state.batch) return;
		this.state.sorting_matrix = await this.adapter.get_sorting_matrix(this.state.batch.name);
	}

	async confirm_stocking() {
		const rows = this.$root.find("[data-stocking-row]").map((_, element) => {
			const $row = $(element);
			const value = (field) => $row.find(`[data-stocking-field='${field}']`).val();
			return { stock_item: $row.attr("data-stock-item"), warehouse: this.warehouse_value($row), batch_no: value("batch_no"), stocked_qty: Number(value("stocked_qty") || 0), shortage_reason: value("shortage_reason") || "" };
		}).get();
		if (!rows.length) return frappe.msgprint(__("当前没有可确认的备货物料。"));
		if (rows.some((row) => !row.warehouse)) return frappe.msgprint(__("请为每一行选择实际备货仓库。"));
		const required = {};
		this.$root.find("[data-stocking-row][data-split='0']").each((_, element) => { required[$(element).attr("data-stock-item")] = Number($(element).attr("data-required-qty") || 0); });
		const totals = rows.reduce((result, row) => ({ ...result, [row.stock_item]: (result[row.stock_item] || 0) + row.stocked_qty }), {});
		const exceeded = Object.keys(totals).find((item) => totals[item] > (required[item] || 0) + 1e-9);
		if (exceeded) return frappe.msgprint(__("物料 {0} 的各仓库/批号备货合计超过批次需求。", [exceeded]));
		try {
			await frappe.xcall(JD_PURCHASE_WORKBENCH_CONTRACT.save_stocking, { import_batch: this.state.batch.name, rows });
			frappe.show_alert({ message: __("批次备货已确认"), indicator: "green" });
			this.state.stocking = await this.adapter.get_stocking_pool(this.state.batch.name);
			await this.refresh_overview();
			this.render_steps_and_content();
		} catch (error) { this.show_action_error(error); }
	}

	split_stocking_row(event) {
		const $source = $(event.currentTarget).closest("[data-stocking-row]");
		const row = { stock_item: $source.attr("data-stock-item"), required_qty: $source.attr("data-required-qty"), warehouse: this.warehouse_value($source), batch_no: "", stocked_qty: 0, shortage_reason: "" };
		const $last = this.$root.find(`[data-stocking-row][data-stock-item="${this.selector_escape(row.stock_item)}"]`).last();
		const $new = $(this.stocking_row_html(row, Date.now(), true));
		$last.after($new);
		this.hydrate_stocking_warehouse_links($new);
	}

	remove_stocking_row(event) { $(event.currentTarget).closest("[data-stocking-row][data-split='1']").remove(); }

	async choose_fefo_batch(event) {
		const $row = $(event.currentTarget).closest("[data-stocking-row]");
		const stock_item = $row.attr("data-stock-item");
		const warehouse = this.warehouse_value($row);
		if (!warehouse) return frappe.msgprint(__("请先填写仓库，再获取FEFO批号建议。"));
		try {
			const candidates = await frappe.xcall(JD_PURCHASE_WORKBENCH_CONTRACT.batch_candidates, { stock_item, warehouse });
			if (!candidates?.length) return frappe.msgprint(__("该仓库没有可用批号。"));
			const dialog = new frappe.ui.Dialog({
				title: __("选择FEFO批号"),
				fields: [{ fieldname: "batch_no", fieldtype: "Select", label: __("批号"), reqd: 1, options: candidates.map((row) => ({ label: `${row.batch_no} · ${row.expiry_date || __("无有效期")} · ${__("可用")} ${this.format(row.available_qty)}${row.eligible ? "" : ` · ${__("不符合2/3效期")}`}`, value: row.batch_no })) }],
				primary_action_label: __("使用该批号"), primary_action: (values) => { $row.find("[data-stocking-field='batch_no']").val(values.batch_no); dialog.hide(); },
			});
			dialog.show();
		} catch (error) { this.show_action_error(error); }
	}

	warehouse_value($row) { return $row.find("[data-warehouse-control]").data("control")?.get_value() || ""; }

	async auto_sort() {
		if (!this.ensure_batch()) return;
		try {
			await frappe.xcall(JD_PURCHASE_WORKBENCH_CONTRACT.auto_allocate, { import_batch: this.state.batch.name });
			await this.load_sorting_rows();
			await this.refresh_overview();
			this.render_steps_and_content();
			frappe.show_alert({ message: __("已按采购单顺序完成自动分配"), indicator: "green" });
		} catch (error) { this.show_action_error(error); }
	}

	async save_sorting() {
		let rows = this.$root.find("[data-sorting-row]").map((_, element) => {
			const $input = $(element);
			return { purchase_order: $input.attr("data-po"), stocking_pool_item: $input.attr("data-pool"), jd_sku: $input.attr("data-jd-sku"), stock_item: $input.attr("data-stock-item"), sorted_qty: Number($input.val() || 0) };
		}).get().filter((row) => row.sorted_qty > 0);
		if (this.state.sorting_order_filter) {
			rows = rows.concat((this.state.sorting_matrix?.allocations || [])
				.filter((row) => row.purchase_order !== this.state.sorting_order_filter && Number(row.sorted_qty || 0) > 0)
				.map((row) => ({ purchase_order: row.purchase_order, stocking_pool_item: row.stocking_pool_item, jd_sku: row.jd_sku, stock_item: row.stock_item, sorted_qty: Number(row.sorted_qty) })));
		}
		const pool_totals = rows.reduce((out, row) => ({ ...out, [row.stocking_pool_item]: (out[row.stocking_pool_item] || 0) + row.sorted_qty }), {});
		const over_pool = (this.state.sorting_matrix?.pools || []).find((pool) => (pool_totals[pool.name] || 0) > Number(pool.stocked_qty || 0) + 1e-9);
		if (over_pool) return frappe.msgprint(__("物料 {0} 在仓库/批号池中的分配数量超过已备货数量。", [over_pool.stock_item]));
		const requirement_totals = rows.reduce((out, row) => { const key = `${row.purchase_order}\u0000${row.jd_sku}\u0000${row.stock_item}`; return { ...out, [key]: (out[key] || 0) + row.sorted_qty }; }, {});
		const over_requirement = (this.state.sorting_matrix?.requirements || []).find((row) => (requirement_totals[`${row.purchase_order}\u0000${row.jd_sku}\u0000${row.stock_item}`] || 0) > Number(row.required_qty || 0) + 1e-9);
		if (over_requirement) return frappe.msgprint(__("采购单 {0} 的京东SKU {1} 分配数量超过需求。", [over_requirement.purchase_order, over_requirement.jd_sku]));
		try {
			await frappe.xcall(JD_PURCHASE_WORKBENCH_CONTRACT.save_allocations, { import_batch: this.state.batch.name, rows });
			await this.load_sorting_rows();
			await this.refresh_overview();
			this.render_steps_and_content();
			frappe.show_alert({ message: __("分拣矩阵已保存"), indicator: "green" });
		} catch (error) { this.show_action_error(error); }
	}

	async refresh_overview() {
		const data = await this.adapter.load_batch(this.state.batch.name);
		this.state.batch = { ...this.state.batch, ...data.batch };
		this.state.orders = data.orders;
		this.state.step_counts = data.step_counts || {};
		this.state.exception_count = data.exception_count || 0;
		if (this.state.order?.name) {
			const workflow = await this.adapter.load_workflow_order(this.state.order.name);
			this.state.workflow = workflow;
			this.state.order = workflow.purchase_order;
		}
	}

	render_packing() {
		const filter = `<div class="jd-pw-step-filter"><label class="jd-pw-order-filter">${__("采购单筛选")}<select class="form-control input-sm" data-packing-order-filter>${this.order_options()}</select></label></div>`;
		if (!this.state.order) return `${filter}${this.empty_step(__("请选择采购单"))}`;
		const gate = this.state.workflow?.packing_gate;
		if (gate && !gate.can_pack) {
			const issues = (gate.issues || []).map((issue) => `<li>${this.escape(issue)}</li>`).join("") || `<li>${__("请先完成当前采购单的备货与分拣。")}</li>`;
			return `${filter}<div class="jd-pw-card jd-pw-gate-block"><div class="jd-pw-gate-icon">!</div><h3>${__("暂不能装箱")}</h3><p>${__("完成备货与分拣并通过库存、批号和效期检查后，装箱工作区才会开放。")}</p><ul>${issues}</ul><div class="jd-pw-gate-actions"><button class="btn btn-default" data-step="stocking">${__("返回备货")}</button><button class="btn btn-primary" data-step="sorting">${__("返回分拣")}</button></div></div>`;
		}
		return `${filter}<div class="jd-pw-packing-mount" data-packing-mount>${this.loading_block(__("正在载入装箱工作区"))}</div>`;
	}

	packing_gate_allows() { const gate = this.state.workflow?.packing_gate; return !gate || Boolean(gate.can_pack); }

	async ensure_packing_component_class() {
		if (window.JDPackingWorkbench) return;
		await new Promise((resolve, reject) => {
			const request = frappe.views.pageview.with_page("jd-packing-workbench", resolve);
			if (request?.fail) request.fail(reject);
		});
		const doc = locals.Page?.["jd-packing-workbench"];
		if (!doc) throw new Error(__("无法载入装箱工作台资源。"));
		const script = doc.__script || doc.script;
		if (script && !window.JDPackingWorkbench) {
			const page_wrapper = frappe.pages["jd-packing-workbench"];
			if (!page_wrapper) frappe.pages["jd-packing-workbench"] = {};
			try { frappe.dom.eval(script); }
			finally { if (!page_wrapper) delete frappe.pages["jd-packing-workbench"]; }
		}
		if (!document.querySelector("style[data-jd-packing-embed]") && doc.style) {
			const style = document.createElement("style");
			style.dataset.jdPackingEmbed = "1";
			style.textContent = doc.style;
			document.head.appendChild(style);
		}
		if (!window.JDPackingWorkbench) throw new Error(__("装箱工作台组件未注册。"));
	}

	async mount_packing_component() {
		const order_name = this.state.order?.name;
		const $mount = this.$root.find("[data-packing-mount]");
		if (!order_name || !$mount.length) return;
		this.cleanup_packing_component();
		try {
			await this.ensure_packing_component_class();
			if (this.state.step !== "packing" || this.state.order?.name !== order_name || !$mount.closest("body").length) return;
			$mount.empty();
			this.packing_component = new window.JDPackingWorkbench(this.wrapper, this.page, { embedded: true, container: $mount, purchase_order: order_name, on_loaded: (component) => this.sync_packing_progress(component) });
			await this.packing_component.load();
			this.update_packing_toolbar_state();
		} catch (error) {
			$mount.html(`<div class="jd-pw-empty is-error"><h3>${__("装箱工作区加载失败")}</h3><p>${this.escape(error?.message || error?.exc || "")}</p></div>`);
		}
	}

	cleanup_packing_component() {
		if (!this.packing_component) return;
		this.packing_component.cleanup_resize?.();
		this.packing_component.container_resize_observer?.disconnect?.();
		this.packing_component.$root?.remove();
		this.packing_component = null;
	}

	sync_packing_progress(component) {
		this.update_packing_toolbar_state();
		const packed_order = component?.state?.data?.order;
		if (!packed_order?.name) return;
		const overview_order = this.state.orders.find((row) => row.name === packed_order.name);
		if (overview_order && packed_order.packing_status) overview_order.packing_status = packed_order.packing_status;
		if (this.state.order?.name === packed_order.name && packed_order.packing_status) this.state.order.packing_status = packed_order.packing_status;
		this.$root.find(".jd-pw-steps").html(this.render_steps());
	}

	render_handover() {
		if (!this.state.order) return this.empty_step(__("请选择采购单"));
		return `<div class="jd-pw-card"><div class="jd-pw-card-head"><div><h3>${__("交接发运")}</h3><span>${__("确认箱数后打印箱贴、上门交接单并生成调拨单")}</span></div></div><div class="jd-pw-action-cards"><button data-action="open-order"><strong>${__("生成调拨单")}</strong><span>${__("进入采购单执行仓库调拨")}</span></button><button data-step="packing"><strong>${__("核对装箱")}</strong><span>${__("在本工作台查看箱数、批号与确认状态")}</span></button><button data-action="open-handovers"><strong>${__("上门交接单")}</strong><span>${__("创建后打印京东格式交接单")}</span></button></div></div>`;
	}

	open_packing_workbench() {
		if (!this.state.order) return;
		frappe.route_options = { purchase_order: this.state.order.name };
		frappe.set_route("jd-packing-workbench");
	}

	download_stocking_pdf() { if (!this.ensure_batch()) return; open_url_post(frappe.request.url, { cmd: "yimed_ecommerce.jd.report_pdf.download_stocking_summary_pdf", import_batch: this.state.batch.name, purchase_order: this.state.order?.name }); }
	download_warehouse_pdf() { if (!this.ensure_batch()) return; open_url_post(frappe.request.url, { cmd: "yimed_ecommerce.jd.report_pdf.download_warehouse_summary_pdf", import_batch: this.state.batch.name, destination_city: this.state.order?.destination_city }); }

	async print_carton_labels() {
		if (!this.state.order) return frappe.msgprint(__("请先选择采购单。"));
		const names = await frappe.xcall("yimed_ecommerce.jd.packing.get_carton_names", { purchase_order: this.state.order.name });
		if (!names?.length) return frappe.msgprint(__("当前采购单没有箱记录。"));
		const params = new URLSearchParams({ doctype: "JD Carton", name: JSON.stringify(names), format: "JD Carton Label", no_letterhead: "1", options: JSON.stringify({ "page-width": "59.8mm", "page-height": "71mm", "margin-top": "1.5mm", "margin-bottom": "1.5mm", "margin-left": "1.5mm", "margin-right": "1.5mm" }) });
		window.open(`/api/method/frappe.utils.print_format.download_multi_pdf?${params.toString()}`);
	}

	async create_handover() { if (!this.ensure_batch()) return; const name = await frappe.xcall("yimed_ecommerce.yimed_ecommerce.doctype.jd_handover.jd_handover.create_from_import_batch", { import_batch: this.state.batch.name }); if (name) frappe.set_route("Form", "JD Handover", name); }
	toggle_packing_toolbar(show) { Object.values(this.packing_toolbar || {}).forEach(($button) => $button?.toggle(Boolean(show))); if (show) this.update_packing_toolbar_state(); }
	update_packing_toolbar_state() {
		if (!this.packing_gate_allows()) {
			Object.values(this.packing_toolbar || {}).forEach(($button) => $button?.prop("disabled", true));
			return;
		}
		const data = this.packing_component?.state?.data;
		const cartons = data?.cartons || [];
		const has_unverified = cartons.some((row) => !row.verified);
		this.packing_toolbar?.auto?.prop("disabled", !data || data.summary.remaining_qty <= 0);
		this.packing_toolbar?.batch?.prop("disabled", !has_unverified);
		this.packing_toolbar?.verify?.prop("disabled", !has_unverified || data?.summary?.remaining_qty !== 0);
		this.packing_toolbar?.print?.prop("disabled", !cartons.length);
	}
	show_action_error(error) { if (!error?._server_messages && !error?.exc) frappe.msgprint({ title: __("操作失败"), message: this.escape(error?.message || __("请稍后重试。")), indicator: "red" }); }
	ensure_batch() { if (this.state.batch) return true; frappe.msgprint(__("请先选择导入批次。")); return false; }

	render_loading() { this.$root.html(this.loading_block(__("正在加载京东采购备货工作台"))); }
	render_error(error) { this.$root.html(`<div class="jd-pw-empty is-error"><h3>${__("工作台加载失败")}</h3><p>${this.escape(error?.message || error?.exc || __("请稍后重试"))}</p></div>`); }
	render_flow_error(error) { this.$root.html(`<div class="jd-pw-empty is-error"><h3>${__("数据加载失败")}</h3><p>${this.escape(error?.message || error?.exc || "")}</p></div>`); }
	loading_block(text) { return `<div class="jd-pw-empty"><span class="jd-pw-spinner"></span><p>${this.escape(text)}</p></div>`; }
	empty_step(text) { return `<div class="jd-pw-inline-empty">${this.escape(text)}</div>`; }
	contract_empty(title, method, description) { return `<div class="jd-pw-contract-empty"><span>◇</span><h3>${this.escape(title)}</h3><p>${this.escape(description)}</p><code>${this.escape(method)}</code></div>`; }
	badge(status) { const tone = ["导入成功", "已装箱", "已确认"].includes(status) ? "green" : ["部分失败", "装箱中", "待确认"].includes(status) ? "orange" : status === "导入失败" ? "red" : "gray"; return `<span class="indicator-pill ${tone}">${this.escape(status || "—")}</span>`; }
	format(value) { return format_number(Number(value || 0), null, 2); }
	selector_escape(value) { return window.CSS?.escape ? window.CSS.escape(String(value)) : String(value).replace(/["\\]/g, "\\$&"); }
	escape(value) { return frappe.utils.escape_html(String(value ?? "")); }
}
