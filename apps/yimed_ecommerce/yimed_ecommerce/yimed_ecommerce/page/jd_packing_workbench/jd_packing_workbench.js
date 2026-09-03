const JD_PACKING_API = "yimed_ecommerce.jd.packing";

frappe.pages["jd-packing-workbench"].on_page_load = (wrapper) => {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("京东装箱工作台"),
		single_column: true,
	});
	wrapper.jd_packing_workbench = new JDPackingWorkbench(wrapper, page);
};

frappe.pages["jd-packing-workbench"].on_page_show = (wrapper) => {
	wrapper.jd_packing_workbench?.show();
};

frappe.pages["jd-packing-workbench"].on_page_hide = (wrapper) => {
	wrapper.jd_packing_workbench?.cleanup_resize();
};

class JDPackingWorkbench {
	constructor(wrapper, page, options = {}) {
		this.wrapper = wrapper;
		this.page = page;
		this.options = options;
		this.embedded = Boolean(options.embedded);
		this.purchase_order = options.purchase_order || "";
		this.state = {
			data: null,
			selected_carton: null,
			loading: false,
			clear_packing_inputs_on_sync: false,
			packing_draft: { items: {}, repeat_count: 1, start_sequence: 1, packing_date: frappe.datetime.get_today() },
			layout: this.load_layout(),
		};
		const $container = options.container ? $(options.container) : page.main.empty();
		this.$root = $('<div class="jd-packing-workbench"></div>').appendTo($container);
		if (!this.embedded) this.make_toolbar();
		this.bind_events();
		this.observe_container_width();
		$(this.wrapper).off("hide.jd-packing-workbench").on("hide.jd-packing-workbench", () => this.cleanup_resize());
	}

	show() {
		const next_purchase_order = this.get_purchase_order();
		this.purchase_order = next_purchase_order;
		if (!this.state.loading) this.load();
	}

	get_purchase_order() {
		if (this.options.purchase_order) return String(this.options.purchase_order);
		const route_options = frappe.route_options || {};
		const query_value = new URLSearchParams(window.location.search).get("purchase_order");
		const value = route_options.purchase_order || query_value || frappe.utils.get_url_arg("purchase_order") || this.purchase_order;
		if (route_options.purchase_order) frappe.route_options = null;
		return value ? String(value) : "";
	}

	make_toolbar() {
		this.page.set_primary_action(__("刷新"), () => this.load(), "refresh");
		this.toolbar_buttons = {
			auto_carton: this.page.add_inner_button(__("整箱自动生成"), () => this.generate_suggestions()),
			batch: this.page.add_inner_button(__("批量填写批号"), () => this.open_batch_dialog()),
			verify: this.page.add_inner_button(__("确认装箱"), () => this.open_verify_dialog()),
			print: this.page.add_inner_button(__("打印箱贴"), () => this.print_labels()),
		};
		this.page.add_menu_item(__("恢复默认布局"), () => this.reset_layout(), false, false, true);
		this.update_toolbar_state();
	}

	bind_events() {
		this.$root.on("click", "[data-carton-name]", (event) => {
			this.state.selected_carton = $(event.currentTarget).attr("data-carton-name");
			this.render_cartons();
		});
		this.$root.on("click", "[data-action='choose-order']", () => this.choose_purchase_order());
		this.$root.on("click", "[data-action='retry']", () => this.load());
		this.$root.on("click", "[data-action='open-carton']", (event) => {
			event.stopPropagation();
			const name = $(event.currentTarget).attr("data-name");
			if (name) frappe.set_route("Form", "JD Carton", name);
		});
		this.$root.on("change", ".jd-pack-item-check", (event) => this.update_item_selection(event));
		this.$root.on("input", ".jd-pack-qty", (event) => this.update_item_qty(event));
		this.$root.on("input change", "[data-packing-control]", (event) => this.update_packing_control(event));
		this.$root.on("click", "[data-action='generate-cartons']", () => this.generate_selected_cartons());
		this.$root.on("click", "[data-action='add-to-current-carton']", () => this.add_selected_to_current_carton());
		this.$root.on("click", "[data-action='remove-allocation']", (event) => this.remove_carton_allocation(event));
		this.$root.on("click", "[data-action='save-carton']", () => this.save_current_carton());
		this.$root.on("click", "[data-action='delete-carton']", () => this.delete_current_carton());
		this.$root.on("click", "[data-action='copy-carton']", () => this.copy_current_carton());
		this.$root.on("pointerdown", "[data-resizer]", (event) => this.start_resize(event));
		this.$root.on("dblclick", "[data-resizer]", () => this.reset_layout());
	}

	async load() {
		this.cleanup_resize();
		if (!this.purchase_order) {
			this.render_missing_order();
			return;
		}
		this.state.loading = true;
		Object.values(this.toolbar_buttons || {}).forEach(($button) => $button?.prop("disabled", true));
		this.render_loading();
		try {
			const result = await frappe.xcall(`${JD_PACKING_API}.get_packing_workbench`, {
				purchase_order: this.purchase_order,
			});
			this.state.data = this.normalize_data(result || {});
			this.sync_packing_draft();
			const names = this.state.data.cartons.map((row) => row.name);
			if (!names.includes(this.state.selected_carton)) {
				this.state.selected_carton = names[0] || null;
			}
			if (!this.embedded) this.update_page_title(this.state.data.order);
			this.update_toolbar_state();
			this.render();
			this.options.on_loaded?.(this);
		} catch (error) {
			this.render_error(error);
		} finally {
			this.state.loading = false;
		}
	}

	normalize_data(source) {
		const raw_order = source.purchase_order || source.order || source.header || {};
		const order = typeof raw_order === "string" ? { name: raw_order } : raw_order;
		const items = source.items || source.purchase_items || source.item_rows || [];
		const cartons = source.cartons || source.carton_rows || [];
		const summary = source.summary || source.stats || source.statistics || source.metrics || {};
		const total_qty = this.number(summary.total_purchase_qty ?? summary.ordered_qty ?? order.total_purchase_qty);
		const packed_qty = this.number(summary.packed_qty);
		const remaining_qty = this.number(summary.remaining_qty ?? Math.max(total_qty - packed_qty, 0));
		const total_cartons = this.number(
			summary.total_cartons ?? summary.carton_count ?? order.total_cartons ?? cartons.length
		);
		const verified_cartons = this.number(
			summary.verified_cartons ?? summary.verified_carton_count ?? cartons.filter((carton) => Boolean(carton.verified)).length
		);
		const progress = this.number(
			summary.progress_percent ?? (total_qty ? Math.min((packed_qty / total_qty) * 100, 100) : 0)
		);
		return {
			order: { ...order, name: order.name || this.purchase_order },
			items: items.map((item) => ({
				...item,
				purchase_qty: this.number(item.purchase_qty ?? item.ordered_qty),
				packed_qty: this.number(item.packed_qty),
				remaining_qty: this.number(
					item.remaining_qty ?? this.number(item.purchase_qty ?? item.ordered_qty) - this.number(item.packed_qty)
				),
			})),
			cartons,
			summary: { total_qty, packed_qty, remaining_qty, total_cartons, verified_cartons, progress },
			next_carton_sequence: this.number(source.next_carton_sequence ?? total_cartons + 1),
		};
	}

	render() {
		this.cleanup_resize();
		const { order, summary } = this.state.data;
		this.$root.html(`
			<section class="jd-packing-compact-header">
				<div class="jd-packing-summary-bar">
					<div class="jd-packing-header-fact"><span>${__("目的仓/城市")}</span><strong>${this.escape(order.jd_warehouse || order.destination_city || "—")}</strong></div>
					<div class="jd-packing-header-fact"><span>${__("预计送达")}</span><strong>${this.escape(order.expected_arrival_date || "—")}</strong></div>
					${this.compact_metric(__("采购"), summary.total_qty)}
					${this.compact_metric(__("已装"), summary.packed_qty, "green")}
					${this.compact_metric(__("待装"), summary.remaining_qty, summary.remaining_qty ? "orange" : "green")}
					${this.compact_metric(__("箱数"), summary.total_cartons)}
					${this.compact_metric(__("已确认"), summary.verified_cartons, "blue")}
					<div class="jd-packing-header-progress"><div><span>${__("进度")}</span><strong>${this.format_number(summary.progress)}%</strong></div><div class="jd-packing-progress"><span style="width:${Math.max(0, Math.min(summary.progress, 100))}%"></span></div></div>
				</div>
			</section>
			<div class="jd-packing-layout-shell">
				<section class="jd-packing-panel jd-packing-items-panel">
					<div class="jd-packing-panel-title"><h3>${__("待装商品清单")}</h3><span>${__("选择1个SKU生成单品箱，选择多个SKU生成混装箱")}</span></div>
					<div class="jd-packing-table-wrap">${this.render_items_table()}</div>
				</section>
				<div class="jd-layout-resizer is-horizontal" data-resizer="horizontal" title="${__("拖动调整高度，双击恢复默认")}"><span></span></div>
				<div class="jd-packing-lower-layout">
					<section class="jd-packing-panel jd-packing-operation-panel"><div class="jd-packing-panel-title"><h3>${__("装箱操作")}</h3><span data-selected-count></span></div>${this.render_packing_bar()}</section>
					<div class="jd-layout-resizer is-vertical" data-resizer="vertical" data-divider="0" title="${__("拖动调整宽度，双击恢复默认")}"><span></span></div>
					<section class="jd-packing-panel jd-packing-carton-list-panel"><div class="jd-packing-panel-title"><h3>${__("箱号清单")}</h3><span>${__("点击查看")}</span></div><div class="jd-packing-carton-list"></div></section>
					<div class="jd-layout-resizer is-vertical" data-resizer="vertical" data-divider="1" title="${__("拖动调整宽度，双击恢复默认")}"><span></span></div>
					<section class="jd-packing-panel jd-packing-carton-detail-panel"><div class="jd-packing-panel-title"><h3>${__("当前箱明细")}</h3><span>${__("编辑、复制与批号")}</span></div><div class="jd-packing-carton-detail"></div></section>
				</div>
			</div>`);
		this.apply_layout();
		this.render_cartons();
	}

	render_items_table() {
		const items = this.state.data.items;
		if (!items.length) return this.empty_block(__("采购单没有商品明细"));
		const rows = items.map((item) => {
			const draft = this.state.packing_draft.items[item.jd_sku] || {};
			const disabled = item.remaining_qty <= 0;
			const product_name = item.platform_item_name || item.jd_item_name || "";
			return `
			<tr>
				<td class="jd-packing-check-cell"><input type="checkbox" class="jd-pack-item-check" data-sku="${this.escape(item.jd_sku)}" ${draft.selected ? "checked" : ""} ${disabled ? "disabled" : ""}></td>
				<td class="jd-item-code" title="${this.escape(item.platform_item || "")}">${this.escape(item.platform_item || "—")}</td>
				<td class="jd-item-name"><span title="${this.escape(product_name)}">${this.escape(product_name || "—")}</span></td>
				<td class="jd-item-sku" title="${this.escape(item.jd_sku || "")}">${this.escape(item.jd_sku || "—")}</td>
				<td class="text-right jd-number-col">${this.format_number(item.purchase_qty)}</td>
				<td class="text-right text-success jd-number-col">${this.format_number(item.packed_qty)}</td>
				<td class="text-right jd-number-col ${item.remaining_qty ? "text-warning" : "text-success"}">${this.format_number(item.remaining_qty)}</td>
				<td class="jd-uom-col">${this.escape(item.purchase_uom || item.uom || "—")}</td>
				<td class="jd-carton-spec-col">${this.escape(this.whole_carton_label(item))}</td>
				<td class="jd-pack-qty-cell"><input type="number" min="0" step="any" class="form-control input-xs jd-pack-qty" data-sku="${this.escape(item.jd_sku)}" value="${this.escape(draft.qty || "")}" ${disabled ? "disabled" : ""} aria-label="${__("每箱数量")}"></td>
			</tr>`;
		}).join("");
		return `<table class="table table-hover jd-packing-table jd-packing-items-table">
			<thead><tr><th>${__("选择")}</th><th>${__("ERP物料编码")}</th><th>${__("产品名称")}</th><th>${__("京东SKU")}</th><th class="text-right">${__("采购")}</th><th class="text-right">${__("已装")}</th><th class="text-right">${__("待装")}</th><th>${__("单位")}</th><th>${__("整箱规格")}</th><th>${__("每箱数量")}</th></tr></thead>
			<tbody>${rows}</tbody>
		</table>`;
	}

	render_packing_bar() {
		const draft = this.state.packing_draft;
		const selected_count = Object.values(draft.items).filter((row) => row.selected).length;
		const current_carton = this.current_carton();
		const can_add_to_current = selected_count && current_carton && !current_carton.verified;
		return `<div class="jd-packing-action-bar">
			<div class="jd-packing-action-help"><strong data-selected-count>${__("已选择 {0} 个 SKU", [selected_count])}</strong><span>${__("从上方清单勾选商品并填写每箱数量")}</span></div>
			<label><span>${__("重复箱数")}</span><input type="number" min="1" step="1" class="form-control input-sm" data-packing-control="repeat_count" value="${this.escape(draft.repeat_count)}"></label>
			<label><span>${__("起始箱号")}</span><input type="number" min="1" step="1" class="form-control input-sm" data-packing-control="start_sequence" value="${this.escape(draft.start_sequence)}"></label>
			<label><span>${__("装箱日期")}</span><input type="date" class="form-control input-sm" data-packing-control="packing_date" value="${this.escape(draft.packing_date)}"></label>
			<button class="btn btn-default" data-action="add-to-current-carton" ${can_add_to_current ? "" : "disabled"}>${__("加入当前箱")}</button>
			<button class="btn btn-primary" data-action="generate-cartons" ${selected_count ? "" : "disabled"}>${__("生成箱记录")}</button>
		</div>`;
	}

	render_cartons() {
		if (!this.state.data || !this.$root.find(".jd-packing-carton-list").length) return;
		const cartons = this.state.data.cartons;
		const $list = this.$root.find(".jd-packing-carton-list");
		const $detail = this.$root.find(".jd-packing-carton-detail");
		if (!cartons.length) {
			$list.html(this.empty_block(__("尚未生成箱记录")));
			$detail.html(this.empty_block(__("在左侧选择商品和每箱数量，或使用整箱自动生成")));
			this.refresh_packing_bar_state();
			return;
		}
		$list.html(cartons.map((carton) => {
			const active = carton.name === this.state.selected_carton ? "is-active" : "";
			return `<button type="button" class="jd-packing-carton-card ${active}" data-carton-name="${this.escape(carton.name)}">
				<span><strong>${__("第 {0} 箱", [carton.carton_sequence])}</strong><small>${this.escape(carton.name)}</small></span>
				<span>${carton.verified ? `<span class="indicator-pill green">${__("已确认")}</span>` : `<span class="indicator-pill orange">${__("待确认")}</span>`}<small>${__("{0} 个 SKU", [carton.sku_count || (carton.allocations || []).length])}</small></span>
			</button>`;
		}).join(""));
		const carton = cartons.find((row) => row.name === this.state.selected_carton) || cartons[0];
		$detail.html(this.render_carton_detail(carton));
		this.refresh_packing_bar_state();
	}

	render_carton_detail(carton) {
		const allocations = carton.allocations || carton.items || [];
		const read_only = Boolean(carton.verified);
		const allocation_rows = allocations.length ? allocations.map((row) => `
			<tr data-allocation-row><td><strong>${this.escape(row.jd_sku || "—")}</strong><small>${this.escape(row.jd_item_name || row.platform_item_name || "")}</small></td><td class="text-right"><input type="number" min="0" step="any" class="form-control input-xs jd-carton-qty" data-sku="${this.escape(row.jd_sku)}" value="${this.escape(row.qty)}" ${read_only ? "disabled" : ""}></td><td>${this.escape(row.uom || "—")}</td><td class="text-right">${read_only ? "" : `<button type="button" class="btn btn-default btn-xs jd-remove-allocation" data-action="remove-allocation">${__("移除")}</button>`}</td></tr>`).join("") : `<tr><td colspan="4" class="text-muted text-center">${__("没有箱内商品数据")}</td></tr>`;
		const components = carton.components || [];
		const batch_rows = components.length ? components.map((row) => `
			<div class="jd-packing-batch-row"><span>${this.escape(row.stock_item || row.item_code || "—")}</span><strong>${this.escape(row.batch_no || __("未填写批号"))}</strong><small>${this.escape(row.expiry_date || "")}</small></div>`).join("") : `<div class="text-muted">${__("暂无批号信息")}</div>`;
		const edit_actions = read_only ? `<span class="text-muted jd-packing-readonly-note">${__("已确认箱不可修改或删除")}</span>` : `<div class="jd-packing-inline-actions"><button class="btn btn-primary btn-sm" data-action="save-carton">${__("保存本箱")}</button><button class="btn btn-danger btn-sm" data-action="delete-carton">${__("删除本箱")}</button></div>`;
		const open_carton_action = read_only ? `<span class="text-muted jd-packing-readonly-note">${__("已确认，只读")}</span>` : `<button class="btn btn-default btn-xs" data-action="open-carton" data-name="${this.escape(carton.name)}">${__("打开箱记录")}</button>`;
		const copy_bar = read_only ? "" : `<div class="jd-packing-copy-bar">
			<div><strong>${__("复制当前箱")}</strong><small>${__("按当前箱商品和数量生成新箱")}</small></div>
			<label><span>${__("复制箱数")}</span><input type="number" min="1" step="1" class="form-control input-xs" data-copy-control="repeat_count" value="1"></label>
			<label><span>${__("起始箱号")}</span><input type="number" min="1" step="1" class="form-control input-xs" data-copy-control="start_sequence" value="${this.escape(this.state.data.next_carton_sequence)}"></label>
			<label><span>${__("装箱日期")}</span><input type="date" class="form-control input-xs" data-copy-control="packing_date" value="${this.escape(frappe.datetime.get_today())}"></label>
			<button class="btn btn-default btn-sm" data-action="copy-carton">${__("复制当前箱")}</button>
		</div>`;
		return `<div class="jd-packing-detail-head">
			<div><span class="jd-packing-eyebrow">${__("当前箱")}</span><h3>${__("第 {0} 箱", [carton.carton_sequence])}</h3></div>
			${open_carton_action}
		</div>
		<div class="jd-packing-detail-meta"><span>${this.escape(carton.packing_date || "—")}</span><span>${carton.verified ? __("已确认") : __("待确认")}</span></div>
		<table class="table jd-packing-table"><thead><tr><th>${__("京东 SKU")}</th><th class="text-right">${__("数量")}</th><th>${__("单位")}</th><th class="text-right">${read_only ? "" : __("操作")}</th></tr></thead><tbody>${allocation_rows}</tbody></table>
		<div class="jd-packing-carton-edit-actions">${edit_actions}</div>
		${copy_bar}
		<div class="jd-packing-batches"><h4>${__("批号与有效期")}</h4>${batch_rows}</div>`;
	}

	render_loading() {
		this.$root.html(`<div class="jd-packing-state"><div class="jd-packing-spinner"></div><h3>${__("正在加载装箱数据")}</h3><p>${__("正在汇总采购数量、箱记录和批号信息…")}</p></div>`);
	}

	render_missing_order() {
		this.$root.html(`<div class="jd-packing-state"><div class="jd-packing-state-icon">📦</div><h3>${__("请选择京东采购单")}</h3><p>${__("从采购单进入装箱工作台，或在这里选择一张采购单。")}</p><button class="btn btn-primary" data-action="choose-order">${__("选择采购单")}</button></div>`);
	}

	render_error(error) {
		const message = error?.message || error?.exc || __("装箱数据加载失败，请稍后重试。") ;
		this.$root.html(`<div class="jd-packing-state is-error"><div class="jd-packing-state-icon">!</div><h3>${__("无法加载装箱工作台")}</h3><p>${this.escape(message)}</p><button class="btn btn-primary" data-action="retry">${__("重新加载")}</button></div>`);
	}

	choose_purchase_order() {
		const dialog = new frappe.ui.Dialog({
			title: __("选择京东采购单"),
			fields: [{ fieldname: "purchase_order", fieldtype: "Link", options: "JD Purchase Order", label: __("京东采购单"), reqd: 1 }],
			primary_action_label: __("进入装箱"),
			primary_action: ({ purchase_order }) => {
				dialog.hide();
				window.location.href = `${window.location.pathname}?purchase_order=${encodeURIComponent(purchase_order)}`;
			},
		});
		dialog.show();
	}

	async generate_suggestions() {
		if (!this.ensure_order()) return;
		try {
			const result = await frappe.xcall(`${JD_PACKING_API}.generate_whole_carton_suggestions`, {
				purchase_order: this.purchase_order,
				packing_date: frappe.datetime.get_today(),
			});
			if (result?.cartons?.length) {
				frappe.show_alert({ message: __("已生成 {0} 个箱记录", [result.cartons.length]), indicator: "green" });
				await this.load();
			}
			this.show_suggestions(result?.items || result?.suggestions || []);
		} catch (error) {
			this.show_action_error(error);
		}
	}

	show_suggestions(suggestions) {
		if (!Array.isArray(suggestions) || !suggestions.length) {
			frappe.msgprint({ title: __("整箱建议"), message: __("当前没有可生成的整箱建议。"), indicator: "blue" });
			return;
		}
		const rows = suggestions.map((row) => `<tr><td>${this.escape(row.jd_sku || "—")}</td><td>${this.escape(row.status || "—")}</td><td class="text-right">${this.format_number(this.suggestion_carton_qty(row))}</td><td class="text-right">${this.format_number(row.created_cartons)}</td><td class="text-right">${this.format_number(row.remainder_qty)}</td><td>${this.escape(row.message || "")}</td></tr>`).join("");
		const dialog = new frappe.ui.Dialog({
			title: __("整箱处理结果"),
			fields: [{ fieldname: "preview", fieldtype: "HTML", options: `<div class="table-responsive"><table class="table"><thead><tr><th>${__("京东 SKU")}</th><th>${__("处理结果")}</th><th class="text-right">${__("每箱")}</th><th class="text-right">${__("已生成箱")}</th><th class="text-right">${__("剩余零头")}</th><th>${__("说明")}</th></tr></thead><tbody>${rows}</tbody></table></div>` }],
			primary_action_label: __("关闭"),
			primary_action: () => dialog.hide(),
		});
		dialog.show();
	}

	sync_packing_draft() {
		const current = this.state.packing_draft.items;
		const clear_inputs = this.state.clear_packing_inputs_on_sync;
		const next = {};
		for (const item of this.state.data.items) {
			const old = current[item.jd_sku] || {};
			const suggested_qty = this.number(item.whole_carton_qty) || item.remaining_qty;
			next[item.jd_sku] = {
				selected: !clear_inputs && item.remaining_qty > 0 && Boolean(old.selected),
				qty: item.remaining_qty > 0 && !clear_inputs ? old.qty || suggested_qty : "",
			};
		}
		this.state.packing_draft.items = next;
		this.state.clear_packing_inputs_on_sync = false;
		this.state.packing_draft.start_sequence = this.state.data.next_carton_sequence;
	}

	update_item_selection(event) {
		const $input = $(event.currentTarget);
		const sku = $input.attr("data-sku");
		if (this.state.packing_draft.items[sku]) {
			this.state.packing_draft.items[sku].selected = $input.prop("checked");
		}
		this.refresh_packing_bar_state();
	}

	update_item_qty(event) {
		const $input = $(event.currentTarget);
		const sku = $input.attr("data-sku");
		if (this.state.packing_draft.items[sku]) {
			this.state.packing_draft.items[sku].qty = $input.val();
			if (this.number($input.val()) > 0) {
				this.state.packing_draft.items[sku].selected = true;
				this.$root.find(`.jd-pack-item-check[data-sku="${this.selector_escape(sku)}"]`).prop("checked", true);
			}
		}
		this.refresh_packing_bar_state();
	}

	update_packing_control(event) {
		const $input = $(event.currentTarget);
		this.state.packing_draft[$input.attr("data-packing-control")] = $input.val();
	}

	refresh_packing_bar_state() {
		const selected_count = Object.values(this.state.packing_draft.items).filter((row) => row.selected).length;
		this.$root.find("[data-selected-count]").text(__("已选择 {0} 个 SKU", [selected_count]));
		this.$root.find("[data-action='generate-cartons']").prop("disabled", !selected_count);
		const carton = this.current_carton();
		this.$root.find("[data-action='add-to-current-carton']").prop("disabled", !selected_count || !carton || Boolean(carton.verified));
	}

	async add_selected_to_current_carton() {
		const carton = this.current_carton();
		if (!carton || carton.verified) {
			frappe.msgprint(__("请选择一个尚未确认的箱。"));
			return;
		}
		const merged = new Map((carton.allocations || []).map((row) => [row.jd_sku, this.number(row.qty)]));
		let selected_count = 0;
		for (const item of this.state.data.items) {
			const row = this.state.packing_draft.items[item.jd_sku];
			if (!row?.selected) continue;
			selected_count += 1;
			const qty = this.number(row.qty);
			if (qty <= 0 || qty > item.remaining_qty + 1e-9) {
				frappe.msgprint(__("京东 SKU {0} 加入数量必须大于 0，且不能超过待装数量 {1}。", [item.jd_sku, this.format_number(item.remaining_qty)]));
				return;
			}
			merged.set(item.jd_sku, this.number(merged.get(item.jd_sku)) + qty);
		}
		if (!selected_count) {
			frappe.msgprint(__("请至少勾选一个待装商品。"));
			return;
		}
		const allocations = Array.from(merged, ([jd_sku, qty]) => ({ jd_sku, qty }));
		await this.run_inline_action(
			"update_carton_allocations",
			{ carton: carton.name, allocations },
			__("商品已加入当前箱"),
			() => {
				this.state.packing_draft.items = {};
				this.state.clear_packing_inputs_on_sync = true;
			}
		);
	}

	async generate_selected_cartons() {
		if (!this.ensure_order()) return;
		const draft = this.state.packing_draft;
		const repeat_count = this.positive_integer(draft.repeat_count);
		const start_sequence = this.positive_integer(draft.start_sequence);
		if (!repeat_count || !start_sequence || !draft.packing_date) {
			frappe.msgprint(__("重复箱数、起始箱号和装箱日期必须填写正确。"));
			return;
		}
		const template_items = [];
		for (const item of this.state.data.items) {
			const row = draft.items[item.jd_sku];
			if (!row?.selected) continue;
			const qty = this.number(row.qty);
			if (qty <= 0) {
				frappe.msgprint(__("京东 SKU {0} 的每箱数量必须大于 0。", [item.jd_sku]));
				return;
			}
			if (qty * repeat_count > item.remaining_qty + 1e-9) {
				frappe.msgprint(__("京东 SKU {0} 本次需要 {1}，超过待装数量 {2}。", [item.jd_sku, this.format_number(qty * repeat_count), this.format_number(item.remaining_qty)]));
				return;
			}
			template_items.push({ jd_sku: item.jd_sku, qty });
		}
		if (!template_items.length) {
			frappe.msgprint(__("请至少勾选一个待装商品。"));
			return;
		}
		const $button = this.$root.find("[data-action='generate-cartons']").prop("disabled", true);
		try {
			await frappe.xcall(`${JD_PACKING_API}.create_mixed_cartons`, {
				purchase_order: this.purchase_order,
				template_items,
				repeat_count,
				start_sequence,
				packing_date: draft.packing_date,
			});
			frappe.show_alert({ message: __("箱记录已生成"), indicator: "green" });
			this.state.packing_draft.items = {};
			await this.load();
		} catch (error) {
			$button.prop("disabled", false);
			this.show_action_error(error);
		}
	}

	current_carton() {
		return this.state.data?.cartons.find((row) => row.name === this.state.selected_carton) || null;
	}

	remove_carton_allocation(event) {
		const carton = this.current_carton();
		if (!carton || carton.verified) return;
		const $rows = this.$root.find("[data-allocation-row]");
		if ($rows.length <= 1) {
			frappe.msgprint(__("一个箱至少要保留一个商品；如需移除全部商品，请删除本箱。"));
			return;
		}
		$(event.currentTarget).closest("[data-allocation-row]").remove();
	}

	async save_current_carton() {
		const carton = this.current_carton();
		if (!carton || carton.verified) return;
		const allocations = [];
		this.$root.find(".jd-carton-qty").each((index, input) => {
			allocations.push({ jd_sku: $(input).attr("data-sku"), qty: this.number($(input).val()) });
		});
		if (!allocations.length || allocations.some((row) => row.qty <= 0)) {
			frappe.msgprint(__("箱内每个商品的数量必须大于 0。"));
			return;
		}
		await this.run_inline_action("update_carton_allocations", { carton: carton.name, allocations }, __("本箱已保存"));
	}

	delete_current_carton() {
		const carton = this.current_carton();
		if (!carton || carton.verified) return;
		frappe.confirm(__("确定删除第 {0} 箱吗？删除后该箱数量将恢复为待装。", [carton.carton_sequence]), async () => {
			await this.run_inline_action("delete_unverified_carton", { carton: carton.name }, __("箱记录已删除"));
		});
	}

	async copy_current_carton() {
		const carton = this.current_carton();
		if (!carton) return;
		const repeat_count = this.positive_integer(this.$root.find("[data-copy-control='repeat_count']").val());
		const start_sequence = this.positive_integer(this.$root.find("[data-copy-control='start_sequence']").val());
		const packing_date = this.$root.find("[data-copy-control='packing_date']").val();
		if (!repeat_count || !start_sequence || !packing_date) {
			frappe.msgprint(__("复制箱数、起始箱号和装箱日期必须填写正确。"));
			return;
		}
		await this.run_inline_action("copy_carton", { carton: carton.name, repeat_count, start_sequence, packing_date }, __("当前箱已复制"));
	}

	async run_inline_action(method, args, success_message, before_reload = null) {
		try {
			await frappe.xcall(`${JD_PACKING_API}.${method}`, args);
			frappe.show_alert({ message: success_message, indicator: "green" });
			if (before_reload) before_reload();
			await this.load();
		} catch (error) {
			this.show_action_error(error);
		}
	}

	open_batch_dialog() {
		if (!this.ensure_cartons()) return;
		const dialog = new frappe.ui.Dialog({
			title: __("批量填写批号"),
			fields: [
				{ fieldname: "jd_sku", fieldtype: "Data", label: __("京东 SKU（可选）") },
				{ fieldname: "stock_item", fieldtype: "Link", label: __("实际库存物料"), options: "Item", reqd: 1 },
				{ fieldname: "batch_no", fieldtype: "Link", label: __("批号"), options: "Batch", reqd: 1, get_query: () => ({ filters: { item: dialog.get_value("stock_item"), disabled: 0 } }) },
				...this.sequence_fields(),
				{ fieldname: "only_empty", fieldtype: "Check", label: __("仅填写空白批号"), default: 1 },
			],
			primary_action_label: __("应用到箱明细"),
			primary_action: (values) => this.call_and_reload("assign_batch_to_cartons", values, dialog),
		});
		dialog.show();
	}

	open_verify_dialog() {
		if (!this.ensure_cartons()) return;
		const dialog = new frappe.ui.Dialog({
			title: __("批量确认装箱"),
			fields: this.sequence_fields(),
			primary_action_label: __("校验有效期并确认"),
			primary_action: (values) => this.call_and_reload("verify_cartons", values, dialog),
		});
		dialog.show();
	}

	sequence_fields() {
		return [
			{ fieldname: "start_sequence", fieldtype: "Int", label: __("起始箱号"), default: 1 },
			{ fieldname: "end_sequence", fieldtype: "Int", label: __("结束箱号"), default: this.state.data.summary.total_cartons },
		];
	}

	async call_and_reload(method, args, dialog) {
		try {
			await frappe.xcall(`${JD_PACKING_API}.${method}`, { purchase_order: this.purchase_order, ...args });
			dialog.hide();
			await this.load();
		} catch (error) {
			this.show_action_error(error);
		}
	}

	async print_labels() {
		if (!this.ensure_cartons()) return;
		try {
			const names = await frappe.xcall(`${JD_PACKING_API}.get_carton_names`, { purchase_order: this.purchase_order });
			if (!names?.length) return;
			const params = new URLSearchParams({
				doctype: "JD Carton",
				name: JSON.stringify(names),
				format: "JD Carton Label",
				no_letterhead: "1",
				options: JSON.stringify({ "page-width": "59.8mm", "page-height": "71mm", "margin-top": "1.5mm", "margin-bottom": "1.5mm", "margin-left": "1.5mm", "margin-right": "1.5mm" }),
			});
			window.open(`/api/method/frappe.utils.print_format.download_multi_pdf?${params.toString()}`);
			await this.load();
		} catch (error) {
			this.show_action_error(error);
		}
	}

	ensure_order() {
		if (this.purchase_order && this.state.data) return true;
		frappe.msgprint(__("请先选择京东采购单。"));
		return false;
	}

	ensure_cartons() {
		if (!this.ensure_order()) return false;
		if (this.state.data.cartons.length) return true;
		frappe.msgprint(__("当前采购单还没有箱记录。"));
		return false;
	}

	show_action_error(error) {
		if (!error?._server_messages && !error?.exc) {
			frappe.msgprint({ title: __("操作失败"), message: this.escape(error?.message || __("请稍后重试。")), indicator: "red" });
		}
	}

	compact_metric(label, value, tone = "") {
		return `<div class="jd-packing-compact-metric ${tone}"><span>${this.escape(label)}</span><strong>${this.format_number(value)}</strong></div>`;
	}

	update_page_title(order) {
		const purchase_order = order.jd_purchase_order_no || order.name || this.purchase_order;
		const status = order.packing_status || order.status || __("待装箱");
		// Current Desk renders page titles through navbar breadcrumbs rather than
		// Page#set_title's legacy .title-text element. Build the two breadcrumb
		// levels explicitly so the order number and status remain visible.
		frappe.breadcrumbs.add({
			type: "Custom",
			label: __("装箱工作台"),
			route: frappe.get_route_str(),
		});
		frappe.breadcrumbs.append_breadcrumb_element(
			`/desk/jd-purchase-order/${encodeURIComponent(order.name || this.purchase_order)}`,
			this.escape(purchase_order),
			"jd-packing-title-order"
		);
		this.page.set_indicator(
			status,
			status === "已装箱" ? "green" : status === "装箱中" ? "orange" : "gray"
		);
		frappe.utils.set_title(`${__("装箱工作台")} / ${purchase_order}`);
		this.page.get_title_area().off("click.jd-packing-title").on("click.jd-packing-title", ".jd-packing-title-order", (event) => {
			event.preventDefault();
			frappe.set_route("Form", "JD Purchase Order", order.name || this.purchase_order);
		});
	}

	update_toolbar_state() {
		if (!this.toolbar_buttons) return;
		const data = this.state.data;
		const order = data?.order || {};
		const cartons = data?.cartons || [];
		const has_cartons = cartons.length > 0;
		const has_unverified = cartons.some((carton) => !carton.verified);
		const mapping_ready = !order.mapping_status || order.mapping_status === "已映射";
		const transfer_started = Boolean(order.stock_entry);
		this.toolbar_buttons.auto_carton?.prop("disabled", !data || transfer_started || !mapping_ready || data.summary.remaining_qty <= 0);
		this.toolbar_buttons.batch?.prop("disabled", !data || transfer_started || !has_unverified);
		this.toolbar_buttons.verify?.prop("disabled", !data || transfer_started || !has_unverified || data.summary.remaining_qty !== 0);
		this.toolbar_buttons.print?.prop("disabled", !has_cartons);
	}

	get layout_storage_key() {
		return `yimed_ecommerce:jd-packing-layout:${frappe.session?.user || "guest"}`;
	}

	default_layout() {
		return { upper: 40, columns: [25, 25, 50] };
	}

	load_layout() {
		const fallback = this.default_layout();
		try {
			const parsed = JSON.parse(window.localStorage.getItem(this.layout_storage_key));
			if (!parsed || !Array.isArray(parsed.columns) || parsed.columns.length !== 3) return fallback;
			const upper = Number(parsed.upper);
			const columns = parsed.columns.map(Number);
			const sum = columns.reduce((total, value) => total + value, 0);
			if (!Number.isFinite(upper) || upper < 25 || upper > 65) return fallback;
			if (columns.some((value) => !Number.isFinite(value)) || columns[0] < 18 || columns[1] < 18 || columns[2] < 35 || Math.abs(sum - 100) > 0.5) return fallback;
			return { upper, columns };
		} catch (error) {
			return fallback;
		}
	}

	save_layout() {
		try {
			window.localStorage.setItem(this.layout_storage_key, JSON.stringify(this.state.layout));
		} catch (error) {
			// Private browsing or a restrictive browser policy may disable localStorage.
		}
	}

	apply_layout() {
		const layout = this.state.layout || this.default_layout();
		const bounds = this.get_upper_resize_bounds();
		layout.upper = this.clamp(layout.upper, bounds.minimum, bounds.maximum);
		this.$root.find(".jd-packing-layout-shell").css("--jd-upper-ratio", `${layout.upper}%`);
		this.$root.find(".jd-packing-lower-layout").css(
			"grid-template-columns",
			`minmax(220px, ${layout.columns[0]}fr) 9px minmax(220px, ${layout.columns[1]}fr) 9px minmax(360px, ${layout.columns[2]}fr)`
		);
	}

	observe_container_width() {
		const target = this.embedded ? this.$root.parent()[0] : (this.page.main?.[0] || this.$root[0]);
		const update = (width) => {
			// The three lower panels need about 818px including splitters. Keep a
			// little operating room, but do not collapse merely because Desk's
			// sidebar makes a normal laptop content area narrower than 1100px.
			const compact = width > 0 && width <= 900;
			this.$root.toggleClass("is-compact-layout", compact);
			if (compact) this.cleanup_resize();
			else this.apply_layout();
		};
		if (window.ResizeObserver && target) {
			this.container_resize_observer = new ResizeObserver((entries) => {
				update(entries[0]?.contentRect?.width || target.getBoundingClientRect().width);
			});
			this.container_resize_observer.observe(target);
		} else if (target) {
			update(target.getBoundingClientRect().width);
		}
	}

	get_upper_resize_bounds() {
		const shell = this.$root.find(".jd-packing-layout-shell")[0];
		const height = shell?.getBoundingClientRect().height || 0;
		if (!height) return { minimum: 25, maximum: 65 };
		const divider_height = 9;
		const minimum_upper_height = 150;
		const minimum_lower_height = 240;
		const minimum = this.clamp((minimum_upper_height / height) * 100, 20, 50);
		const maximum = this.clamp(((height - divider_height - minimum_lower_height) / height) * 100, minimum, 70);
		return { minimum, maximum };
	}

	reset_layout() {
		this.state.layout = this.default_layout();
		this.apply_layout();
		this.save_layout();
		frappe.show_alert({ message: __("已恢复默认布局"), indicator: "blue" });
	}

	start_resize(event) {
		if (this.$root.hasClass("is-compact-layout") || window.matchMedia("(max-width: 1100px)").matches || event.button !== 0) return;
		this.cleanup_resize();
		const handle = event.currentTarget;
		const kind = $(handle).attr("data-resizer");
		const divider = Number($(handle).attr("data-divider") || 0);
		const start_x = event.clientX;
		const start_y = event.clientY;
		const initial = { upper: this.state.layout.upper, columns: [...this.state.layout.columns] };
		const shell = this.$root.find(".jd-packing-layout-shell")[0];
		const lower = this.$root.find(".jd-packing-lower-layout")[0];
		if (!shell || !lower) return;
		event.preventDefault();
		handle.setPointerCapture?.(event.pointerId);
		$(handle).addClass("is-active");
		$(document.body).addClass("jd-layout-resizing");

		const on_move = (move_event) => {
			if (kind === "horizontal") {
				const delta = ((move_event.clientY - start_y) / shell.getBoundingClientRect().height) * 100;
				const bounds = this.get_upper_resize_bounds();
				this.state.layout.upper = this.clamp(initial.upper + delta, bounds.minimum, bounds.maximum);
			} else {
				const delta = ((move_event.clientX - start_x) / lower.getBoundingClientRect().width) * 100;
				const columns = [...initial.columns];
				if (divider === 0) {
					columns[0] = this.clamp(initial.columns[0] + delta, 18, initial.columns[0] + initial.columns[1] - 18);
					columns[1] = initial.columns[0] + initial.columns[1] - columns[0];
				} else {
					columns[1] = this.clamp(initial.columns[1] + delta, 18, initial.columns[1] + initial.columns[2] - 35);
					columns[2] = initial.columns[1] + initial.columns[2] - columns[1];
				}
				this.state.layout.columns = columns;
			}
			this.apply_layout();
		};
		let cleaned = false;
		const cleanup = () => {
			if (cleaned) return;
			cleaned = true;
			handle.removeEventListener("pointermove", on_move);
			handle.removeEventListener("pointerup", cleanup);
			handle.removeEventListener("pointercancel", cleanup);
			handle.removeEventListener("lostpointercapture", cleanup);
			$(handle).removeClass("is-active");
			$(document.body).removeClass("jd-layout-resizing");
			this.save_layout();
			if (this.active_resize_cleanup === cleanup) this.active_resize_cleanup = null;
		};
		this.active_resize_cleanup = cleanup;
		handle.addEventListener("pointermove", on_move);
		handle.addEventListener("pointerup", cleanup);
		handle.addEventListener("pointercancel", cleanup);
		handle.addEventListener("lostpointercapture", cleanup);
	}

	cleanup_resize() {
		if (this.active_resize_cleanup) this.active_resize_cleanup();
		$(document.body).removeClass("jd-layout-resizing");
	}

	clamp(value, minimum, maximum) {
		return Math.min(Math.max(value, minimum), maximum);
	}

	suggestion_carton_qty(row) {
		return this.number(row.qty_per_carton ?? row.whole_carton_qty ?? row.full_carton_qty ?? row.carton_qty);
	}

	whole_carton_label(item) {
		const qty = this.number(item.whole_carton_qty ?? item.carton_spec ?? item.qty_per_carton ?? item.pack_size);
		if (!qty) return "—";
		return `${this.format_number(qty)} ${item.purchase_uom || item.uom || ""}/${item.whole_carton_uom || __("箱")}`.trim();
	}

	stat_card(label, value, tone = "") {
		return `<div class="jd-packing-stat ${tone}"><span>${this.escape(label)}</span><strong>${this.format_number(value)}</strong></div>`;
	}

	status_badge(status) {
		const tone = status === "已装箱" ? "green" : status === "装箱中" ? "orange" : "gray";
		return `<span class="indicator-pill ${tone}">${this.escape(status)}</span>`;
	}

	empty_block(message) {
		return `<div class="jd-packing-empty"><span>📦</span><p>${this.escape(message)}</p></div>`;
	}

	format_number(value) {
		return format_number(this.number(value), null, 2);
	}

	positive_integer(value) {
		const number = Number(value);
		return Number.isInteger(number) && number > 0 ? number : 0;
	}

	selector_escape(value) {
		if (window.CSS?.escape) return window.CSS.escape(String(value));
		return String(value).replace(/["\\]/g, "\\$&");
	}

	number(value) {
		const number = Number(value || 0);
		return Number.isFinite(number) ? number : 0;
	}

	escape(value) {
		return frappe.utils.escape_html(String(value ?? ""));
	}
}

window.JDPackingWorkbench = JDPackingWorkbench;
