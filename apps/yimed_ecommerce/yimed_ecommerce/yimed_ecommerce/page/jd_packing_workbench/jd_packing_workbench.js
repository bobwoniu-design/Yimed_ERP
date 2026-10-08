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
			checked_cartons: new Set(),
			deleting_cartons: false,
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
			auto_carton: this.page.add_inner_button(__("自动装箱"), () => this.auto_whole_cartons()),
			generate: this.page.add_inner_button(__("生成新箱"), () => this.generate_selected_cartons()),
			add: this.page.add_inner_button(__("加入当前箱"), () => this.add_selected_to_current_carton()),
			print: this.page.add_inner_button(__("打印箱贴"), () => this.print_labels()),
		};
		this.page.add_menu_item(__("恢复默认布局"), () => this.reset_layout(), false, false, true);
		this.update_toolbar_state();
	}

	bind_events() {
		this.$root.on("click", "[data-carton-name]", (event) => {
			if (event.target.closest("input, label")) return;
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
		this.$root.on("change", ".jd-pack-item-check-all", (event) => this.select_all_items(event));
		this.$root.on("change", ".jd-pack-item-check", (event) => this.update_item_selection(event));
		this.$root.on("input", ".jd-pack-qty", (event) => this.update_item_qty(event));
		this.$root.on("input change", "[data-packing-control]", (event) => this.update_packing_control(event));
		this.$root.on("click", "[data-action='generate-cartons']", () => this.generate_selected_cartons());
		this.$root.on("click", "[data-action='auto-whole-cartons']", () => this.auto_whole_cartons());
		this.$root.on("click", "[data-action='add-to-current-carton']", () => this.add_selected_to_current_carton());
		this.$root.on("click", "[data-action='remove-allocation']", (event) => this.remove_carton_allocation(event));
		// 修改箱内数量后失焦/回车即自动保存（输入即保存，无需手动确认）
		this.$root.on("change", ".jd-carton-qty", () => this.save_current_carton());
		this.$root.on("click", "[data-bundle-toggle]", (event) => {
			const sku = $(event.currentTarget).attr("data-bundle-toggle");
			const $detail = this.$root.find(`[data-bundle-detail='${CSS.escape(sku)}']`);
			$detail.toggle();
		});
		this.$root.on("click", "[data-action='save-carton']", () => this.save_current_carton());
		this.$root.on("click", "[data-action='delete-cartons']", () => this.delete_checked_cartons());
		this.$root.on("click", "[data-action='print-cartons']", () => this.print_labels(this.checked_carton_names()));
		this.$root.on("change", ".jd-carton-check", (event) => {
			const input = event.currentTarget;
			if (input.checked) this.state.checked_cartons.add(input.dataset.name);
			else this.state.checked_cartons.delete(input.dataset.name);
			this.refresh_carton_selection();
		});
		this.$root.on("change", ".jd-carton-check-all", (event) => {
			this.state.checked_cartons = new Set(event.currentTarget.checked ? this.state.data.cartons.map((row) => row.name) : []);
			this.refresh_carton_selection();
		});
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
			this.state.checked_cartons = new Set([...this.state.checked_cartons].filter((name) => names.includes(name)));
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
			<div class="jd-packing-layout-shell">
				<section class="jd-packing-panel jd-packing-items-panel">
					<div class="jd-packing-panel-title"><h3>${__("待装商品清单")}</h3><span>${__("选择1个SKU生成单品箱，选择多个SKU生成混装箱")}</span></div>
					<div class="jd-packing-table-wrap">${this.render_items_table()}</div>
					<div class="jd-packing-items-summary">${this.render_items_summary()}</div>
				</section>
				<div class="jd-layout-resizer is-horizontal" data-resizer="horizontal" title="${__("拖动调整高度，双击恢复默认")}"><span></span></div>
				<div class="jd-packing-lower-layout">
					<section class="jd-packing-panel jd-packing-carton-list-panel"><div class="jd-packing-panel-title"><h3>${__("箱号清单")}</h3>${this.render_carton_actions()}</div><div class="jd-packing-carton-list"></div><div class="jd-carton-selection-footer"><span data-carton-selected-count></span></div></section>
					<div class="jd-layout-resizer is-vertical" data-resizer="vertical" data-divider="0" title="${__("拖动调整宽度，双击恢复默认")}"><span></span></div>
					<section class="jd-packing-panel jd-packing-carton-detail-panel"><div class="jd-packing-panel-title"><h3>${__("当前箱明细")}</h3><span>${__("商品数量与批号")}</span></div><div class="jd-packing-carton-detail"></div></section>
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
				<td class="jd-batch-col" title="${this.escape(item.stock_batches || "")}"><span>${this.escape(item.stock_batches || "—")}</span></td>
				<td class="text-right jd-number-col">${this.format_number(item.purchase_qty)}</td>
				<td class="text-right text-success jd-number-col">${this.format_number(item.packed_qty)}</td>
				<td class="text-right jd-number-col ${item.remaining_qty ? "text-warning" : "text-success"}">${this.format_number(item.remaining_qty)}</td>
				<td class="jd-uom-col">${this.escape(item.purchase_uom || item.uom || "—")}</td>
				<td class="jd-carton-spec-col">${this.escape(this.whole_carton_label(item))}</td>
				<td class="jd-pack-qty-cell"><input type="number" min="0" step="any" class="form-control input-xs jd-pack-qty" data-sku="${this.escape(item.jd_sku)}" value="${this.escape(draft.qty || "")}" ${disabled ? "disabled" : ""} aria-label="${__("装箱数量")}"></td>
			</tr>`;
		}).join("");
		return `<table class="table table-hover jd-packing-table jd-packing-items-table">
			<thead><tr><th class="jd-packing-check-cell"><input type="checkbox" class="jd-pack-item-check-all" title="${__("全选待装商品")}" aria-label="${__("全选待装商品")}"></th><th>${__("ERP物料编码")}</th><th>${__("产品名称")}</th><th>${__("京东SKU")}</th><th>${__("备货批号")}</th><th class="text-right">${__("采购")}</th><th class="text-right">${__("已装")}</th><th class="text-right">${__("待装")}</th><th>${__("单位")}</th><th>${__("整箱规格")}</th><th>${__("装箱数量")}</th></tr></thead>
			<tbody>${rows}</tbody>
		</table>`;
	}

	render_items_summary() {
		const items = this.state.data?.items || [];
		const selected = items.filter((item) => this.state.packing_draft.items[item.jd_sku]?.selected);
		const totals = (rows, quantity) => {
			const units = new Map();
			for (const row of rows) {
				const unit = row.purchase_uom || row.uom || "";
				units.set(unit, (units.get(unit) || 0) + this.number(quantity(row)));
			}
			return [...units].map(([unit, qty]) => `${this.format_number(qty)} ${this.escape(unit)}`).join(" / ") || "0";
		};
		return `<strong>${__("共 {0} 条 · 已选 {1} 条", [items.length, selected.length])}</strong>
			<span>${__("采购")} <b>${totals(items, (row) => row.purchase_qty)}</b></span>
			<span>${__("已装")} <b>${totals(items, (row) => row.packed_qty)}</b></span>
			<span>${__("待装")} <b>${totals(items, (row) => row.remaining_qty)}</b></span>
			<span class="jd-packing-selected-total">${__("本次装箱")} <b>${totals(selected, (row) => this.state.packing_draft.items[row.jd_sku]?.qty)}</b></span>`;
	}

	can_generate_carton() {
		return Boolean(this.state.data) && !this.state.creating_cartons && !this.transfer_started() &&
			Object.values(this.state.packing_draft.items).some((row) => row.selected);
	}

	can_add_to_carton() {
		return this.can_generate_carton() && Boolean(this.current_carton());
	}

	can_auto_pack() {
		return !this.transfer_started() && (this.state.data?.items || []).some((item) =>
			this.state.packing_draft.items[item.jd_sku]?.selected &&
			this.number(item.whole_carton_qty) > 0 && item.remaining_qty >= this.number(item.whole_carton_qty)
		);
	}

	render_cartons() {
		if (!this.state.data || !this.$root.find(".jd-packing-carton-list").length) return;
		const cartons = this.state.data.cartons;
		const $list = this.$root.find(".jd-packing-carton-list");
		const $detail = this.$root.find(".jd-packing-carton-detail");
		if (!cartons.length) {
			$list.html(this.empty_block(__("尚未生成箱记录")));
			$detail.html(this.empty_block(__("在上方勾选商品并填写数量，再点击顶部生成新箱或自动装箱")));
			this.refresh_packing_bar_state();
			return;
		}
		$list.html(`<table class="table jd-carton-list-table">
			<thead><tr><th class="jd-carton-check-cell"><input type="checkbox" class="jd-carton-check-all" aria-label="${__("全选箱子")}"></th><th>${__("箱号")}</th><th>${__("状态")}</th><th class="text-right">SKU</th></tr></thead>
			<tbody>${cartons.map((carton) => `<tr class="${carton.name === this.state.selected_carton ? "is-active" : ""}" data-carton-name="${this.escape(carton.name)}">
				<td class="jd-carton-check-cell"><input type="checkbox" class="jd-carton-check" data-name="${this.escape(carton.name)}" aria-label="${__("选择第 {0} 箱", [carton.carton_sequence])}"></td>
				<td><button type="button" class="jd-carton-view" title="${this.escape(carton.name)}">${__("第 {0} 箱", [carton.carton_sequence])}</button><small title="${this.escape(carton.name)}">${this.escape(carton.name)}</small></td>
				<td><span class="indicator-pill ${carton.verified ? "green" : "orange"}">${carton.verified ? __("已装箱") : __("待装箱")}</span></td>
				<td class="text-right">${carton.sku_count || (carton.allocations || []).length}</td>
			</tr>`).join("")}</tbody></table>`);

		const carton = cartons.find((row) => row.name === this.state.selected_carton) || cartons[0];
		$detail.html(this.render_carton_detail(carton));
		this.refresh_packing_bar_state();
	}

	render_carton_detail(carton) {
		const allocations = carton.allocations || carton.items || [];
		const read_only = this.transfer_started();
		// 批次号映射：carton.components 按 jd_sku 汇总（一个 SKU 可能多批次）
		const components = carton.components || [];
		const batches_by_sku = {};
		const erp_by_sku = {};
		for (const c of components) {
			const sku = c.jd_sku || c.stock_item || "";
			if (!sku) continue;
			const bn = c.batch_no || "";
			(batches_by_sku[sku] = batches_by_sku[sku] || new Set()).add(bn || __("未填写批号"));
			if (c.stock_item) (erp_by_sku[sku] = erp_by_sku[sku] || new Set()).add(c.stock_item);
		}
		const components_by_sku = {};
		for (const c of components) {
			const sku = c.jd_sku || c.stock_item || "";
			if (!sku) continue;
			(components_by_sku[sku] = components_by_sku[sku] || []).push(c);
		}
		const set_text = (map, sku) => (map[sku] && map[sku].size ? Array.from(map[sku]).join("、") : "—");
		const batch_text = (sku) => {
			const set = batches_by_sku[sku];
			return set && set.size ? Array.from(set).join("、") : __("未填写批号");
		};
		// 组合装判定：同一 SKU 拆出多个库存组件
		const is_bundle = (sku) => (components_by_sku[sku] || []).filter((c) => c.stock_item).length > 1;
		const allocation_rows = allocations.length ? allocations.map((row) => {
			const bundle = is_bundle(row.jd_sku);
			const expand = bundle ? ` <button type="button" class="btn btn-default btn-xs jd-bundle-toggle" data-bundle-toggle="${this.escape(row.jd_sku)}" title="${__("查看组件批次明细")}">${__("组件")}</button>` : "";
			const detail_rows = bundle ? `
			<tr class="jd-bundle-detail" data-bundle-detail="${this.escape(row.jd_sku)}" style="display:none"><td colspan="6"><div class="jd-packing-batches jd-bundle-batches">${components_by_sku[row.jd_sku].map((c) => `
				<div class="jd-packing-batch-row"><span>${this.escape(c.stock_item)}<small>${this.escape(c.stock_item_name || "")}</small></span><strong>${this.escape(c.batch_no || __("未填写批号"))}</strong><small>${this.escape(c.expiry_date || "")}</small></div>`).join("")}</div></td></tr>` : "";
			return `
			<tr data-allocation-row><td class="jd-cd-code-col"><span title="${this.escape(set_text(erp_by_sku, row.jd_sku))}">${this.escape(set_text(erp_by_sku, row.jd_sku))}</span></td><td class="jd-cd-sku-col"><strong>${this.escape(row.jd_sku || "—")}</strong>${expand}<small>${this.escape(row.jd_item_name || row.platform_item_name || "")}</small></td><td class="text-right jd-cd-qty-col"><input type="number" min="0" step="any" class="form-control input-xs jd-carton-qty" data-sku="${this.escape(row.jd_sku)}" value="${this.escape(row.qty)}" ${read_only ? "disabled" : ""}></td><td class="jd-cd-uom-col">${this.escape(row.uom || "—")}</td><td class="jd-cd-batch-col"><span title="${this.escape(batch_text(row.jd_sku))}">${this.escape(batch_text(row.jd_sku))}</span></td><td class="text-right jd-cd-action-col">${read_only ? "" : `<button type="button" class="btn btn-default btn-xs jd-remove-allocation" data-action="remove-allocation">${__("移除")}</button>`}</td></tr>${detail_rows}`;
		}).join("") : `<tr><td colspan="6" class="text-muted text-center">${__("没有箱内商品数据")}</td></tr>`;
		const edit_actions = read_only ? `<span class="text-muted jd-packing-readonly-note">${__("转移单已生成，箱已锁定")}</span>` : "";
		return `<table class="table jd-packing-table jd-carton-detail-table"><thead><tr><th>${__("ERP 编码")}</th><th>${__("京东 SKU")}</th><th class="text-right">${__("数量")}</th><th>${__("单位")}</th><th>${__("批次号")}</th><th class="text-right">${read_only ? "" : __("操作")}</th></tr></thead><tbody>${allocation_rows}</tbody></table>
		<div class="jd-packing-carton-edit-actions">${edit_actions}</div>`;
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

	async auto_whole_cartons() {
		if (!this.ensure_order() || this.transfer_started()) return;
		// 勾选的 SKU（draft.items 以 jd_sku 为键）
		const selected_skus = Object.entries(this.state.packing_draft.items)
			.filter(([, row]) => row.selected)
			.map(([sku]) => sku)
			.filter(Boolean);
		const selected_items = (this.state.data.items || []).filter((item) => selected_skus.includes(item.jd_sku));
		if (!selected_skus.length) {
			frappe.msgprint(__("请先勾选需要自动装整箱的商品。"));
			return;
		}
		const whole_ok = selected_items.filter((item) => this.number(item.whole_carton_qty) > 0 && item.remaining_qty > 0);
		const confirm_msg = __(
			"已勾选 {0} 个商品，其中 {1} 个配置了整箱规格。确认自动生成整箱吗？\n（零头和未配置整箱规格的将留待手动装箱）",
			[selected_skus.length, whole_ok.length]
		);
		frappe.confirm(
			confirm_msg,
			async () => {
				try {
					const result = await frappe.xcall(`${JD_PACKING_API}.generate_whole_carton_suggestions`, {
						purchase_order: this.purchase_order,
						packing_date: frappe.datetime.get_today(),
						selected_skus: JSON.stringify(selected_skus),
					});
					if (result?.cartons?.length) {
						frappe.show_alert({ message: __("已生成 {0} 个整箱", [result.cartons.length]), indicator: "green" });
					} else {
						frappe.show_alert({ message: __("勾选的商品没有可凑整箱的数量"), indicator: "orange" });
					}
					await this.load();
					this.show_suggestions(result?.items || result?.suggestions || []);
				} catch (error) {
					this.show_action_error(error);
				}
			}
		);
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
			const whole = this.number(item.whole_carton_qty);
			// 装箱数量默认值：待装数量与整箱规格取小（整箱优先，余数保留）
			const suggested_qty = whole > 0 ? Math.min(whole, item.remaining_qty) : item.remaining_qty;
			next[item.jd_sku] = {
				selected: !clear_inputs && item.remaining_qty > 0 && Boolean(old.selected),
				qty: item.remaining_qty > 0 && !clear_inputs ? old.qty || suggested_qty : "",
			};
		}
		this.state.packing_draft.items = next;
		this.state.clear_packing_inputs_on_sync = false;
		this.state.packing_draft.start_sequence = this.state.data.next_carton_sequence;
	}

	select_all_items(event) {
		const checked = $(event.currentTarget).prop("checked");
		for (const item of this.state.data?.items || []) {
			const draft = this.state.packing_draft.items[item.jd_sku];
			if (draft) draft.selected = item.remaining_qty > 0 && checked;
		}
		this.$root.find(".jd-pack-item-check").each((_, input) => {
			input.checked = Boolean(this.state.packing_draft.items[input.dataset.sku]?.selected);
		});
		this.refresh_packing_bar_state();
	}

	refresh_item_selection() {
		const selectable = (this.state.data?.items || []).filter((item) => item.remaining_qty > 0);
		const selected = selectable.filter((item) => this.state.packing_draft.items[item.jd_sku]?.selected).length;
		this.$root.find(".jd-pack-item-check-all").prop({
			checked: selectable.length > 0 && selected === selectable.length,
			indeterminate: selected > 0 && selected < selectable.length,
			disabled: selectable.length === 0,
		});
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
			// 装箱数量上限 = min(整箱规格, 待装数量)：超限时自动钳到上限并提示
			const item = (this.state.data.items || []).find((row) => row.jd_sku === sku);
			let qty = this.number($input.val());
			let limit = item ? item.remaining_qty : 0;
			const whole = this.number(item?.whole_carton_qty);
			if (whole > 0) limit = Math.min(limit, whole);
			if (qty > limit && limit > 0) {
				qty = limit;
				$input.val(limit);
				frappe.show_alert({
					message: __("京东 SKU {0} 的装箱数量已按上限 {1} 填写（整箱规格 {2}、待装 {3}）。", [
						sku,
						this.format_number(limit),
						whole > 0 ? this.format_number(whole) : "—",
						this.format_number(item ? item.remaining_qty : 0),
					]),
					indicator: "orange",
				});
			}
			this.state.packing_draft.items[sku].qty = qty > 0 ? qty : $input.val();
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
		this.refresh_item_selection();
		const selected_count = Object.values(this.state.packing_draft.items).filter((row) => row.selected).length;
		this.$root.find(".jd-packing-items-summary").html(this.render_items_summary());
		this.update_toolbar_state();
		this.options.on_selection_changed?.(this);
		this.$root.find("[data-action='generate-cartons']").prop("disabled", !selected_count || this.transfer_started());
		this.$root.find("[data-action='auto-whole-cartons']").prop("disabled", !selected_count || this.transfer_started());
		const carton = this.current_carton();
		this.$root.find("[data-action='add-to-current-carton']").prop("disabled", !selected_count || !carton || this.transfer_started());
		this.refresh_carton_selection();
	}

	async add_selected_to_current_carton() {
		const carton = this.current_carton();
		if (!carton) {
			frappe.msgprint(__("请先选择一个箱。"));
			return;
		}
		if (this.transfer_started()) {
			frappe.msgprint(__("转移单已生成，箱已锁定。请先在 ERPNext 取消转移单。"));
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
		if (!this.ensure_order() || !this.can_generate_carton()) return;
		const draft = this.state.packing_draft;
		draft.repeat_count = 1;
		draft.start_sequence = this.state.data.next_carton_sequence;
		draft.packing_date = frappe.datetime.get_today();
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
				frappe.msgprint(__("京东 SKU {0} 的装箱数量必须大于 0。", [item.jd_sku]));
				return;
			}
			if (qty * repeat_count > item.remaining_qty + 1e-9) {
				frappe.msgprint(__("京东 SKU {0} 本次需要 {1}，超过待装数量 {2}。", [item.jd_sku, this.format_number(qty * repeat_count), this.format_number(item.remaining_qty)]));
				return;
			}
			// 装箱数量不得超过整箱规格（单箱容量）
			const whole_limit = this.number(item.whole_carton_qty);
			if (whole_limit > 0 && qty > whole_limit + 1e-9) {
				frappe.msgprint(__("京东 SKU {0} 的装箱数量 {1} 超过整箱规格 {2}。", [item.jd_sku, this.format_number(qty), this.format_number(whole_limit)]));
				return;
			}
			template_items.push({ jd_sku: item.jd_sku, qty });
		}
		if (!template_items.length) {
			frappe.msgprint(__("请至少勾选一个待装商品。"));
			return;
		}
		this.state.creating_cartons = true;
		this.refresh_packing_bar_state();
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
			this.show_action_error(error);
		} finally {
			this.state.creating_cartons = false;
			this.refresh_packing_bar_state();
		}
	}

	// 待装=0 即装箱完成（无独立确认环节）；只有转移开始后才锁定
	transfer_started() {
		return Boolean(this.state.data?.order?.stock_entry);
	}

	current_carton() {
		return this.state.data?.cartons.find((row) => row.name === this.state.selected_carton) || null;
	}

	remove_carton_allocation(event) {
		const carton = this.current_carton();
		if (!carton || this.transfer_started()) return;
		const $rows = this.$root.find("[data-allocation-row]");
		if ($rows.length <= 1) {
			frappe.msgprint(__("一个箱至少要保留一个商品；如需移除全部商品，请在箱号清单中勾选该箱后删除。"));
			return;
		}
		$(event.currentTarget).closest("[data-allocation-row]").remove();
	}

	async save_current_carton() {
		const carton = this.current_carton();
		if (!carton || this.transfer_started()) return;
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

	render_carton_actions() {
		return `<div class="jd-carton-actions">
			<div class="jd-carton-action-group">
				<button type="button" class="btn btn-danger btn-sm" data-action="delete-cartons" disabled>${__("删除")}</button>
				<button type="button" class="btn btn-default btn-sm" data-action="print-cartons" disabled>${__("打印")}</button>
			</div>
		</div>`;
	}

	checked_carton_names() {
		return (this.state.data?.cartons || []).filter((row) => this.state.checked_cartons.has(row.name)).map((row) => row.name);
	}

	refresh_carton_selection() {
		const names = this.checked_carton_names();
		const total = this.state.data?.cartons.length || 0;
		this.$root.find("[data-carton-selected-count]").text(__("已选 {0} / {1} 箱", [names.length, total]));
		this.$root.find(".jd-carton-check").each((index, input) => {
			input.checked = this.state.checked_cartons.has(input.dataset.name);
			$(input).closest("tr").toggleClass("is-checked", input.checked);
		});
		this.$root.find(".jd-carton-check-all").prop("checked", total > 0 && names.length === total).prop("indeterminate", names.length > 0 && names.length < total);
		this.$root.find("[data-action='delete-cartons']").prop("disabled", !names.length || this.transfer_started() || this.state.deleting_cartons);
		this.$root.find("[data-action='print-cartons']").prop("disabled", !names.length || this.state.deleting_cartons);
	}

	delete_checked_cartons() {
		const names = this.checked_carton_names();
		if (!names.length || this.transfer_started() || this.state.deleting_cartons) return;
		const sequences = this.state.data.cartons.filter((row) => names.includes(row.name)).map((row) => row.carton_sequence);
		frappe.confirm(__("确定删除选中的 {0} 个箱（箱号：{1}）吗？删除后数量将恢复为待装。", [names.length, sequences.join("、")]), async () => {
			if (this.state.deleting_cartons || this.transfer_started()) return;
			this.state.deleting_cartons = true;
			this.refresh_carton_selection();
			let deleted = 0;
			try {
				for (const name of names) {
					await frappe.xcall(`${JD_PACKING_API}.delete_unverified_carton`, { carton: name });
					this.state.checked_cartons.delete(name);
					deleted++;
				}
				frappe.show_alert({ message: __("已删除 {0} 个箱", [deleted]), indicator: "green" });
			} catch (error) {
				this.show_action_error(error);
				frappe.msgprint(__("已删除 {0} / {1} 个箱，其余箱未删除，请刷新结果后重试。", [deleted, names.length]));
			} finally {
				this.state.deleting_cartons = false;
				await this.load();
			}
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

	undo_verify() {
		// 待装=0 即装箱完成，无独立确认环节；此方法保留占位避免旧引用报错
	}

	open_verify_dialog() {
		// 待装=0 即装箱完成，无独立确认环节；此方法保留占位避免旧引用报错
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

	async print_labels(selected_names = null) {
		if (!this.ensure_cartons()) return;
		try {
			if (selected_names && !selected_names.length) return;
			const names = await frappe.xcall(`${JD_PACKING_API}.get_carton_names`, {
				purchase_order: this.purchase_order,
				...(selected_names ? { cartons: selected_names } : {}),
			});
			if (!names?.length) return;
			const params = new URLSearchParams({
				doctype: "JD Carton",
				name: JSON.stringify(names),
				format: "JD Carton Label",
				no_letterhead: "1",
				options: JSON.stringify({ "page-width": "100mm", "page-height": "80mm", "margin-top": "1.5mm", "margin-bottom": "1.5mm", "margin-left": "1.5mm", "margin-right": "1.5mm" }),
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
		const mapping_ready = !order.mapping_status || order.mapping_status === "已映射";
		const transfer_started = Boolean(order.stock_entry);
		this.toolbar_buttons.auto_carton?.prop("disabled", !this.can_auto_pack());
		this.toolbar_buttons.generate?.prop("disabled", !this.can_generate_carton());
		this.toolbar_buttons.add?.prop("disabled", !this.can_add_to_carton());
		this.toolbar_buttons.batch?.prop("disabled", !data || transfer_started || !has_cartons);
		this.toolbar_buttons.print?.prop("disabled", !has_cartons);
	}

	get layout_storage_key() {
		return `yimed_ecommerce:jd-packing-layout-v2:${frappe.session?.user || "guest"}`;
	}

	default_layout() {
		return { upper: 40, columns: [35, 65] };
	}

	load_layout() {
		const fallback = this.default_layout();
		try {
			const parsed = JSON.parse(window.localStorage.getItem(this.layout_storage_key));
			if (!parsed || !Array.isArray(parsed.columns) || parsed.columns.length !== 2) return fallback;
			const upper = Number(parsed.upper);
			const columns = parsed.columns.map(Number);
			const sum = columns.reduce((total, value) => total + value, 0);
			if (!Number.isFinite(upper) || upper < 25 || upper > 65) return fallback;
			if (columns.some((value) => !Number.isFinite(value)) || columns[0] < 25 || columns[1] < 40 || Math.abs(sum - 100) > 0.5) return fallback;
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
			`minmax(280px, ${layout.columns[0]}fr) 9px minmax(360px, ${layout.columns[1]}fr)`
		);
	}

	observe_container_width() {
		const target = this.embedded ? this.$root.parent()[0] : (this.page.main?.[0] || this.$root[0]);
		const update = (width) => {
			// Stack the two panels only when their minimum widths no longer fit.
			const compact = width > 0 && width <= 700;
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
				columns[0] = this.clamp(initial.columns[0] + delta, 25, 60);
				columns[1] = 100 - columns[0];
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
