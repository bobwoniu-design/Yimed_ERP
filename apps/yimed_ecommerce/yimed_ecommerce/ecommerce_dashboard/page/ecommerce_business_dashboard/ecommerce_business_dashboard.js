frappe.pages["ecommerce-business-dashboard"].on_page_load = function (wrapper) {
	frappe.ecommerce_dashboard = new EcommerceDashboard(wrapper);
	$(wrapper)
		.off("hide.ecommerce-business-dashboard")
		.on("hide.ecommerce-business-dashboard", () => frappe.ecommerce_dashboard?.hide());
};

frappe.pages["ecommerce-business-dashboard"].on_page_show = function () {
	frappe.app?.sidebar?.setup("Ecommerce Dashboard");
	frappe.ecommerce_dashboard?.show();
};

const format_amount = (value) => format_number(value || 0, null, 2);
const format_count = (value) => format_number(Math.round(value || 0), null, 0);

class EcommerceDashboard {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.page = frappe.ui.make_app_page({ parent: wrapper, title: __("Profit Estimate Dashboard"), single_column: true });
		this.filters = {};
		this.charts = [];
		this.datatables = {};
		this.analysis_layout = this.load_analysis_layout();
		this.make_actions();
		this.make_body();
		this.observe_analysis_width();
	}

	make_actions() {
		this.page.add_inner_button(__("Promotion Analytics Dashboard"), () => {
			frappe.route_options = this.get_values()
			frappe.set_route("ecommerce-promotion-dashboard")
		});
		this.page.add_inner_button(__("Product Listings"), () => frappe.set_route("List", "Ecommerce Product Listing"));
		this.page.add_inner_button(__("Fee Rules"), () => frappe.set_route("List", "Ecommerce Fee Rule"));
		this.page.add_inner_button(__("Promotion Expenses"), () => frappe.set_route("List", "Ecommerce Promotion Expense"));
		this.page.add_menu_item(__("Restore Analysis Layout"), () => this.reset_analysis_layout(), false, false, true);
		this.page.add_menu_item(__("Restore Table Column Layout"), () => this.reset_table_layouts(), false, false, true);
		this.page.set_primary_action(__("Refresh"), () => this.load(), "refresh");
	}

	make_body() {
		this.$root = $("<div class='ec-dashboard'></div>").appendTo(this.page.main);
		this.$root.html(`
			<div class="ec-filter-panel">
				<div class="ec-filter-grid" data-filter-grid></div>
				<div class="ec-filter-actions"><button class="btn btn-primary btn-sm" data-refresh>${__("Query")}</button></div>
			</div>
			<div class="ec-source-note" data-source-note></div>
			<div class="ec-kpis" data-kpis></div>
			<div class="ec-analysis-layout" data-analysis-layout>
				<section class="ec-card ec-analysis-panel" data-analysis-panel="trend"><header>${__("Sales and Contribution Profit Trend")}</header><div class="ec-chart" data-trend></div></section>
				<div class="ec-analysis-divider" data-analysis-divider="0" title="${__("Drag to resize; double-click to restore")}"><span></span></div>
				<section class="ec-card ec-analysis-panel" data-analysis-panel="fees"><header>${__("Expense Mix")}</header><div class="ec-chart" data-fees></div></section>
				<div class="ec-analysis-divider" data-analysis-divider="1" title="${__("Drag to resize; double-click to restore")}"><span></span></div>
				<section class="ec-card ec-analysis-panel" data-analysis-panel="quality"><header>${__("Data Quality")}</header><div data-quality></div></section>
			</div>
			<section class="ec-card ec-store-card"><header>${__("Store Performance")}</header><div class="ec-table-wrap" data-store-table></div></section>
			<section class="ec-card"><header>${__("Product Listing Profit")}</header><div class="ec-table-wrap" data-listing-table></div></section>
		`);
		this.$root.on("click", "[data-refresh]", () => this.load());
		this.$root.on("pointerdown", "[data-analysis-divider]", (event) => this.start_analysis_resize(event));
		this.$root.on("dblclick", "[data-analysis-divider]", () => this.reset_analysis_layout());
		this.$root.on("mousedown", ".dt-cell__resize-handle", (event) => {
			this.resizing_table = $(event.currentTarget).closest("[data-dashboard-table]").attr("data-dashboard-table");
		});
		this.$root.on("dblclick", ".dt-cell__resize-handle", (event) => {
			const table_name = $(event.currentTarget).closest("[data-dashboard-table]").attr("data-dashboard-table");
			window.setTimeout(() => this.save_datatable_layout(table_name), 0);
		});
		this.bind_table_layout_events();
		this.apply_analysis_layout();
	}

	async show() {
		this.is_visible = true;
		this.bind_table_layout_events();
		this.observe_analysis_width();
		if (!this.controls_ready) {
			this.filters_promise ||= this.make_filters();
			await this.filters_promise;
		}
		await this.load();
	}

	hide() {
		this.is_visible = false;
		this.cleanup_analysis_resize();
		this.analysis_resize_observer?.disconnect();
		this.analysis_resize_observer = null;
		this.destroy_charts();
		this.destroy_datatables();
		this.unbind_table_layout_events();
	}

	async make_filters() {
		const options = await frappe.xcall("yimed_ecommerce.ecommerce_dashboard.dashboard.get_filter_options");
		this.store_options = options.stores || [];
		const today = frappe.datetime.get_today();
		const yesterday = frappe.datetime.add_days(today, -1).slice(0, 10);
		const month_start = `${yesterday.slice(0, 8)}01`;
		const definitions = [
			{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date", default: month_start, reqd: 1 },
			{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", default: yesterday, reqd: 1 },
			{ fieldname: "channel_name", label: __("Channel"), fieldtype: "Select", options: ["", ...options.channels], onchange: () => this.update_store_filter() },
			{ fieldname: "store_name", label: __("Store"), fieldtype: "Select", options: ["", ...new Set(this.store_options.map((row) => row.store))] },
			{ fieldname: "listing_dimension", label: __("Listing Dimension"), fieldtype: "Select", options: [{ label: __("Main Listing"), value: "main" }, { label: __("Child Listing"), value: "child" }], default: "main" },
		];
		const grid = this.$root.find("[data-filter-grid]").empty();
		for (const df of definitions) {
			const host = $("<div></div>").appendTo(grid);
			const control = frappe.ui.form.make_control({ parent: host, df, render_input: true });
			await control.set_value(frappe.route_options?.[df.fieldname] || df.default || "");
			this.filters[df.fieldname] = control;
		}
		frappe.route_options = null;
		this.controls_ready = true;
		this.update_store_filter();
	}

	update_store_filter() {
		const channel = this.filters.channel_name?.get_value() || "";
		const store_control = this.filters.store_name;
		if (!store_control) return;
		const stores = [...new Set(this.store_options
			.filter((row) => !channel || row.channel === channel)
			.map((row) => row.store))].sort();
		const current = store_control.get_value() || "";
		store_control.df.options = ["", ...stores];
		store_control.set_options?.();
		if (current && !stores.includes(current)) store_control.set_value("");
	}

	get_values() {
		const values = {};
		for (const [key, control] of Object.entries(this.filters)) {
			values[key] = control.get_value() || control.$input?.val() || "";
		}
		if (!values.from_date || !values.to_date) {
			frappe.throw(__("Date range is required."));
		}
		return values;
	}

	async load() {
		if (!this.controls_ready || this.loading) return;
		this.loading = true;
		this.page.set_primary_action(__("Loading..."), null, "refresh");
		try {
			const data = await frappe.xcall("yimed_ecommerce.ecommerce_dashboard.dashboard.get_dashboard", this.get_values());
			if (this.is_visible) this.render(data);
		} finally {
			this.loading = false;
			this.page.set_primary_action(__("Refresh"), () => this.load(), "refresh");
		}
	}

	render(data) {
		this.destroy_charts();
		this.destroy_datatables();
		this.last_dashboard_data = data;
		this.render_source(data);
		this.render_kpis(data.summary, data.previous, data.fee_columns || []);
		this.render_trend(data.trend);
		this.render_fees(data.fee_breakdown);
		this.render_stores(data.stores, data.fee_columns || []);
		this.render_listings(data.listings, data.filters.listing_dimension, data.fee_columns || []);
		this.render_quality(data.data_quality, data.filters.listing_dimension);
	}

	render_source(data) {
		const date_text = `${frappe.datetime.str_to_user(data.filters.from_date)} — ${frappe.datetime.str_to_user(data.filters.to_date)}`;
		this.$root.find("[data-source-note]").html(
			`<span>${frappe.utils.escape_html(date_text)}</span><span>${__("Operational basis: submitted sales orders; costs use ERP inventory valuation")}</span>`,
		);
	}

	render_kpis(summary, previous, fee_columns) {
		const promotion_visible = fee_columns.some((item) => item.dashboard_field === "promotion_expense");
		const specs = [
			[__("Net Sales"), summary.net_sales, previous.net_sales, "currency"],
			[__("Order Count"), summary.orders, previous.orders, "integer"],
			[__("Gross Profit"), summary.gross_profit, previous.gross_profit, "currency"],
			[__("Gross Margin"), summary.gross_margin_pct, previous.gross_margin_pct, "percent"],
			...(promotion_visible ? [
				[__("Promotion Expense"), summary.promotion_expense, previous.promotion_expense, "currency"],
				[__("Promotion Expense Rate"), summary.promotion_ratio_pct, previous.promotion_ratio_pct, "percent"],
			] : []),
			[__("Contribution Profit"), summary.contribution_profit, previous.contribution_profit, "currency"],
			[__("Contribution Margin"), summary.contribution_margin_pct, previous.contribution_margin_pct, "percent"],
			[__("Return Rate"), summary.return_ratio_pct, previous.return_ratio_pct, "percent"],
		];
		const html = specs.map(([label, value, prior, type]) => {
			const delta = prior ? ((value - prior) / Math.abs(prior)) * 100 : null;
			const delta_text = delta === null ? __("No prior-period baseline") : `${delta >= 0 ? "+" : ""}${format_number(delta, null, 1)}% ${__("vs prior")}`;
			return `<article class="ec-kpi"><div class="ec-kpi-label">${label}</div><div class="ec-kpi-value">${this.format(value, type)}</div><div class="ec-kpi-delta">${delta_text}</div></article>`;
		}).join("");
		this.$root.find("[data-kpis]").html(html);
	}

	render_trend(rows) {
		const target = this.$root.find("[data-trend]").empty().get(0);
		if (!rows.length) return this.render_empty(target);
		this.trend_chart = new frappe.Chart(target, {
			data: { labels: rows.map((row) => frappe.datetime.str_to_user(row.date)), datasets: [
				{ name: __("Net Sales"), values: rows.map((row) => row.net_sales) },
				{ name: __("Contribution Profit"), values: rows.map((row) => row.contribution_profit) },
			] },
			type: "line", height: 260, colors: ["#2563eb", "#16a34a"], animate: 0, disableEntryAnimation: 1, axisOptions: { xIsSeries: true },
			tooltipOptions: { formatTooltipY: (value) => format_amount(value) },
		});
		this.charts.push(this.trend_chart);
	}

	render_fees(rows) {
		const target = this.$root.find("[data-fees]").empty().get(0);
		if (!rows.length || !rows.some((row) => row.amount)) return this.render_empty(target);
		this.fees_chart = new frappe.Chart(target, {
			data: { labels: rows.map((row) => row.fee_type), datasets: [{ values: rows.map((row) => row.amount) }] },
			type: "donut", height: 260, colors: ["#2563eb", "#f59e0b", "#ef4444", "#8b5cf6", "#14b8a6", "#64748b"], animate: 0, disableEntryAnimation: 1,
			tooltipOptions: { formatTooltipY: (value) => format_amount(value) },
		});
		this.charts.push(this.fees_chart);
	}

	render_stores(rows, fee_columns) {
		const columns = [
			this.text_column("channel", __("Channel"), 130, true),
			this.text_column("store", __("Store"), 150, true),
			this.count_column("orders", __("Order Count"), 100),
			this.amount_column("net_sales", __("Net Sales")),
			this.amount_column("product_cost", __("Product Cost")),
			...fee_columns.map((fee) => this.amount_column(this.fee_column_id(fee), this.fee_label(fee))),
			this.amount_column("contribution_profit", __("Contribution Profit"), 140),
			this.percent_column("contribution_margin_pct", __("Margin")),
		];
		const data = rows.map((row) => ({
			...row,
			...Object.fromEntries(fee_columns.map((fee) => [this.fee_column_id(fee), this.fee_value(row, fee)])),
		}));
		this.render_datatable("stores", "[data-store-table]", columns, data);
	}

	render_listings(rows, dimension, fee_columns) {
		const id_label = dimension === "child" ? __("Child Listing ID") : __("Main Listing ID");
		const columns = [
			this.text_column("channel", __("Channel"), 130, true),
			this.text_column("store", __("Store"), 150, true),
			this.text_column("listing_id", id_label, 150),
			this.text_column("listing_name", __("Product"), 220),
			this.count_column("orders", __("Order Count"), 100),
			this.amount_column("net_sales", __("Net Sales")),
			this.amount_column("product_cost", __("Product Cost")),
			...fee_columns.map((fee) => this.amount_column(this.fee_column_id(fee), this.fee_label(fee))),
			this.amount_column("rule_fees", __("Allocated Fees Total")),
			this.amount_column("contribution_profit", __("Contribution Profit"), 140),
			this.percent_column("contribution_margin_pct", __("Margin")),
		];
		const data = rows.map((row) => ({
			...row,
			...Object.fromEntries(fee_columns.map((fee) => [this.fee_column_id(fee), this.fee_value(row, fee)])),
		}));
		this.render_datatable("listings", "[data-listing-table]", columns, data);
	}

	text_column(id, name, width = 140, sticky = false) {
		return { id, name, width, sticky, format: (value) => frappe.utils.escape_html(value || "—") };
	}

	amount_column(id, name, width = 120) {
		return { id, name: frappe.utils.escape_html(name), width, align: "right", format: format_amount };
	}

	count_column(id, name, width = 100) {
		return { id, name, width, align: "right", format: format_count };
	}

	percent_column(id, name, width = 90) {
		return { id, name, width, align: "right", format: (value) => `${format_number(value || 0, null, 1)}%` };
	}

	fee_column_id(fee) { return `fee::${fee.dashboard_field || fee.fee_item}`; }

	fee_value(row, fee) {
		return fee.dashboard_field === "promotion_expense"
			? row.promotion_expense || 0
			: row.fee_breakdown?.[fee.label] || 0;
	}

	render_datatable(table_name, selector, default_columns, data) {
		const host = this.$root.find(selector).empty().attr("data-dashboard-table", table_name).get(0);
		this.destroy_datatable(table_name);
		if (!data.length) {
			$(host).html(`<div class="ec-empty">${__("No data")}</div>`);
			return;
		}
		const columns = this.apply_saved_table_layout(table_name, default_columns);
		const DataTableClass = frappe.DataTable || window.DataTable;
		if (!DataTableClass) {
			$(host).html(`<div class="ec-empty">${__("Table component is unavailable")}</div>`);
			return;
		}
		this.datatables[table_name] = this.create_datatable(DataTableClass, host, {
			columns,
			data,
			layout: "fixed",
			serialNoColumn: false,
			checkboxColumn: false,
			cellHeight: 38,
			minimumColumnWidth: 54,
			language: frappe.boot.lang,
			translations: frappe.utils.datatable.get_translations(),
			noDataMessage: __("No data"),
			events: {
				onSwitchColumn: () => window.setTimeout(() => this.save_datatable_layout(table_name), 0),
			},
		});
	}

	create_datatable(DataTableClass, host, options) {
		// frappe-datatable 1.20.7 registers its dropdown scroll listener with capture=true,
		// but destroy() removes it without the capture flag. Record only listeners added
		// synchronously by this constructor so we can remove those exact functions later.
		const capture_scroll_handlers = [];
		const add_event_listener = document.addEventListener;
		document.addEventListener = function (type, listener, listener_options) {
			if (type === "scroll" && (listener_options === true || listener_options?.capture === true)) {
				capture_scroll_handlers.push(listener);
			}
			return add_event_listener.call(this, type, listener, listener_options);
		};
		try {
			const table = new DataTableClass(host, options);
			table.__dashboard_capture_scroll_handlers = capture_scroll_handlers;
			return table;
		} catch (error) {
			for (const handler of capture_scroll_handlers) document.removeEventListener("scroll", handler, true);
			throw error;
		} finally {
			document.addEventListener = add_event_listener;
		}
	}

	get table_layout_key_prefix() {
		return `yimed_ecommerce:ecommerce-table-layout:${frappe.session?.user || "guest"}`;
	}

	table_layout_key(table_name) { return `${this.table_layout_key_prefix}:${table_name}`; }

	load_table_layout(table_name) {
		try {
			const value = JSON.parse(window.localStorage.getItem(this.table_layout_key(table_name)) || "null");
			return value && Array.isArray(value.order) && value.widths && typeof value.widths === "object" ? value : null;
		} catch (error) {
			return null;
		}
	}

	apply_saved_table_layout(table_name, default_columns) {
		const saved = this.load_table_layout(table_name);
		if (!saved) return default_columns;
		const by_id = new Map(default_columns.map((column) => [column.id, column]));
		const saved_order = [...new Set(saved.order.filter((id) => typeof id === "string" && by_id.has(id)))];
		const saved_ids = new Set(saved_order);
		const ordered_ids = [...saved_order, ...default_columns.map((column) => column.id).filter((id) => !saved_ids.has(id))];
		return ordered_ids.map((id) => {
			const column = { ...by_id.get(id) };
			const width = Number(saved.widths[id]);
			if (Number.isFinite(width) && width >= 54 && width <= 800) column.width = width;
			return column;
		});
	}

	save_datatable_layout(table_name) {
		const table = this.datatables[table_name];
		if (!table?.getColumns) return;
		const columns = table.getColumns().filter((column) => !column.id.startsWith("_"));
		const value = {
			order: columns.map((column) => column.id),
			widths: Object.fromEntries(columns.map((column) => [column.id, Math.round(Number(column.width) || 0)])),
		};
		try { window.localStorage.setItem(this.table_layout_key(table_name), JSON.stringify(value)); }
		catch (error) { /* localStorage may be unavailable in private browsing. */ }
	}

	bind_table_layout_events() {
		if (this.table_layout_events_bound) return;
		this.table_layout_events_bound = true;
		$(document.body).on("mouseup.ecommerce-dashboard-table-layout", () => {
			const table_name = this.resizing_table;
			this.resizing_table = null;
			if (table_name) window.setTimeout(() => this.save_datatable_layout(table_name), 0);
		});
	}

	unbind_table_layout_events() {
		$(document.body).off("mouseup.ecommerce-dashboard-table-layout");
		this.table_layout_events_bound = false;
		this.resizing_table = null;
	}

	reset_table_layouts() {
		for (const table_name of ["stores", "listings"]) {
			try { window.localStorage.removeItem(this.table_layout_key(table_name)); }
			catch (error) { /* localStorage may be unavailable in private browsing. */ }
		}
		if (this.last_dashboard_data) {
			this.render_stores(this.last_dashboard_data.stores || [], this.last_dashboard_data.fee_columns || []);
			this.render_listings(this.last_dashboard_data.listings || [], this.last_dashboard_data.filters?.listing_dimension, this.last_dashboard_data.fee_columns || []);
		}
	}

	destroy_datatable(table_name) {
		const table = this.datatables[table_name];
		for (const handler of table?.__dashboard_capture_scroll_handlers || []) {
			document.removeEventListener("scroll", handler, true);
		}
		try {
			table?.destroy?.();
		} finally {
			document.body.classList.remove("dt-resize");
			this.resizing_table = null;
			delete this.datatables[table_name];
		}
	}

	destroy_datatables() {
		for (const table_name of Object.keys(this.datatables)) this.destroy_datatable(table_name);
	}

	fee_label(fee) { return fee.label === "管理分摊" ? __("管理费") : fee.label; }

	render_quality(quality, dimension) {
		const promotion_pct = quality.period_days ? Math.min(100, quality.promotion_days / quality.period_days * 100) : 0;
		const listing_label = dimension === "child" ? __("Child Listing ID coverage") : __("Main Listing ID coverage");
		const items = [
			[__("Cost coverage"), quality.cost_coverage_pct, __("Percentage of sales/return quantity with an ERP inventory valuation rate")],
			[__("Promotion data coverage"), promotion_pct, `${quality.promotion_days}/${quality.period_days} ${__("days have imported promotion expenses")}`],
			[listing_label, quality.listing_coverage_pct, __("Percentage of sales amount carrying the selected listing ID")],
		];
		this.$root.find("[data-quality]").html(items.map(([label, value, note]) => `<div class="ec-quality"><div><strong>${label}</strong><span>${format_number(value, null, 1)}%</span></div><div class="progress"><div class="progress-bar" style="width:${Math.max(0, Math.min(100, value))}%"></div></div><p>${note}</p></div>`).join("") + (quality.unmapped_sales_amount ? `<div class="alert alert-warning">${__("Unmapped-channel sales")}: ${format_amount(quality.unmapped_sales_amount)}</div>` : "") + (quality.unidentified_listing_sales ? `<div class="alert alert-warning">${__("Sales without listing ID")}: ${format_amount(quality.unidentified_listing_sales)}</div>` : ""));
	}

	default_analysis_layout() { return [50, 25, 25]; }

	get analysis_layout_key() {
		return `yimed_ecommerce:ecommerce-analysis-layout:${frappe.session?.user || "guest"}`;
	}

	load_analysis_layout() {
		try {
			const values = JSON.parse(window.localStorage.getItem(this.analysis_layout_key));
			if (!Array.isArray(values) || values.length !== 3) return this.default_analysis_layout();
			const normalized = values.map(Number);
			const total = normalized.reduce((sum, value) => sum + value, 0);
			if (normalized.some((value) => !Number.isFinite(value) || value < 10) || Math.abs(total - 100) > 0.5) {
				return this.default_analysis_layout();
			}
			return normalized;
		} catch (error) {
			return this.default_analysis_layout();
		}
	}

	save_analysis_layout() {
		try { window.localStorage.setItem(this.analysis_layout_key, JSON.stringify(this.analysis_layout)); }
		catch (error) { /* localStorage may be unavailable in private browsing. */ }
	}

	apply_analysis_layout() {
		const [trend, fees, quality] = this.analysis_layout;
		this.$root?.find("[data-analysis-layout]").css(
			"grid-template-columns",
			`minmax(320px, ${trend}fr) 9px minmax(210px, ${fees}fr) 9px minmax(230px, ${quality}fr)`,
		);
	}

	reset_analysis_layout() {
		this.cleanup_analysis_resize();
		this.analysis_layout = this.default_analysis_layout();
		this.save_analysis_layout();
		this.apply_analysis_layout();
	}

	start_analysis_resize(event) {
		if (event.button !== 0 || this.analysis_compact) return;
		this.cleanup_analysis_resize();
		const divider = event.currentTarget;
		const index = Number($(divider).attr("data-analysis-divider"));
		const panels = this.$root.find("[data-analysis-panel]").get();
		if (![0, 1].includes(index) || panels.length !== 3) return;

		const start_x = event.clientX;
		const start_widths = panels.map((panel) => panel.getBoundingClientRect().width);
		const minimums = [320, 210, 230];
		const total_width = start_widths.reduce((sum, width) => sum + width, 0);
		event.preventDefault();
		divider.setPointerCapture?.(event.pointerId);
		$(divider).addClass("is-active");
		$(document.body).addClass("ec-analysis-resizing");

		const on_move = (move_event) => {
			const delta = Math.min(
				start_widths[index + 1] - minimums[index + 1],
				Math.max(minimums[index] - start_widths[index], move_event.clientX - start_x),
			);
			const next = [...start_widths];
			next[index] = start_widths[index] + delta;
			next[index + 1] = start_widths[index + 1] - delta;
			this.analysis_layout = next.map((width) => width / total_width * 100);
			this.apply_analysis_layout();
		};
		let cleaned = false;
		const cleanup = () => {
			if (cleaned) return;
			cleaned = true;
			divider.removeEventListener("pointermove", on_move);
			divider.removeEventListener("pointerup", cleanup);
			divider.removeEventListener("pointercancel", cleanup);
			divider.removeEventListener("lostpointercapture", cleanup);
			$(divider).removeClass("is-active");
			$(document.body).removeClass("ec-analysis-resizing");
			this.save_analysis_layout();
			if (this.analysis_resize_cleanup === cleanup) this.analysis_resize_cleanup = null;
		};
		this.analysis_resize_cleanup = cleanup;
		divider.addEventListener("pointermove", on_move);
		divider.addEventListener("pointerup", cleanup);
		divider.addEventListener("pointercancel", cleanup);
		divider.addEventListener("lostpointercapture", cleanup);
	}

	cleanup_analysis_resize() {
		this.analysis_resize_cleanup?.();
		$(document.body).removeClass("ec-analysis-resizing");
	}

	observe_analysis_width() {
		const target = this.$root?.find("[data-analysis-layout]").get(0);
		if (!target || this.analysis_resize_observer || !window.ResizeObserver) return;
		this.analysis_resize_observer = new ResizeObserver((entries) => {
			const width = entries[0]?.contentRect?.width || target.getBoundingClientRect().width;
			const compact = width > 0 && width < 900;
			if (compact !== this.analysis_compact) {
				this.analysis_compact = compact;
				this.$root.toggleClass("is-analysis-compact", compact);
				if (compact) this.cleanup_analysis_resize();
				else this.apply_analysis_layout();
			}
		});
		this.analysis_resize_observer.observe(target);
	}

	destroy_charts() {
		for (const chart of this.charts) chart?.destroy?.();
		this.charts = [];
		this.trend_chart = null;
		this.fees_chart = null;
	}

	render_empty(target) { $(target).html(`<div class="ec-empty">${__("No data in the selected range")}</div>`); }
	format(value, type) {
		if (type === "currency") return format_amount(value);
		if (type === "integer") return format_count(value);
		if (type === "percent") return `${format_number(value, null, 1)}%`;
		return format_number(value);
	}
}
