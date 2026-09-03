frappe.pages["ecommerce-promotion-dashboard"].on_page_load = function (wrapper) {
	frappe.ecommerce_promotion_dashboard = new EcommercePromotionDashboard(wrapper)
}

frappe.pages["ecommerce-promotion-dashboard"].on_page_show = function () {
	frappe.app?.sidebar?.setup("Ecommerce Dashboard")
	frappe.ecommerce_promotion_dashboard?.show()
}

class EcommercePromotionDashboard {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({ parent: wrapper, title: __("Promotion Analytics Dashboard"), single_column: true })
		this.controls = {}
		this.page.add_inner_button(__("Business Dashboard"), () => {
			frappe.route_options = this.values()
			frappe.set_route("ecommerce-business-dashboard")
		})
		this.page.set_primary_action(__("Refresh"), () => this.load(), "refresh")
		this.$root = $("<div class='pa-dashboard'></div>").appendTo(this.page.main)
		this.$root.html(`<div class="pa-filters" data-filters></div><div class="pa-note" data-note></div><div class="pa-kpis" data-kpis></div><div class="pa-card"><header>${__("Promotion Trend")}</header><div data-trend></div></div><div class="pa-card"><header>${__("Campaign and Product Performance")}</header><div class="table-responsive"><table class="table table-bordered"><thead data-head></thead><tbody data-body></tbody></table></div></div>`)
	}

	async show() {
		if (!this.ready) await this.make_filters()
		await this.load()
	}

	async make_filters() {
		const options = await frappe.xcall("yimed_ecommerce.ecommerce_dashboard.promotion_dashboard.get_filter_options")
		const today = frappe.datetime.get_today()
		const yesterday = frappe.datetime.add_days(today, -1).slice(0, 10)
		const defs = [
			{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date", default: `${yesterday.slice(0, 8)}01`, reqd: 1 },
			{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", default: yesterday, reqd: 1 },
			{ fieldname: "company", label: __("Company"), fieldtype: "Select", options: ["", ...options.companies] },
			{ fieldname: "store_name", label: __("Store"), fieldtype: "Select", options: ["", ...options.stores] },
			{ fieldname: "promotion_scene", label: __("Promotion Scene"), fieldtype: "Select", options: ["", ...options.scenes] },
			{ fieldname: "attribution_window", label: __("Attribution Window"), fieldtype: "Select", options: options.attribution_windows, default: "1天转化", reqd: 1 },
		]
		for (const df of defs) {
			const control = frappe.ui.form.make_control({ parent: $("<div></div>").appendTo(this.$root.find("[data-filters]")), df, render_input: true })
			await control.set_value(frappe.route_options?.[df.fieldname] || df.default || "")
			this.controls[df.fieldname] = control
		}
		frappe.route_options = null
		this.ready = true
	}

	values() {
		return Object.fromEntries(Object.entries(this.controls).map(([key, control]) => [key, control.get_value() || ""]))
	}

	async load() {
		if (!this.ready) return
		const data = await frappe.xcall("yimed_ecommerce.ecommerce_dashboard.promotion_dashboard.get_promotion_dashboard", this.values())
		this.render(data)
	}

	render(data) {
		this.chart?.destroy()
		this.$root.find("[data-note]").text(`${data.source_note} ${__("Default attribution window")}: ${data.filters.attribution_window}`)
		const specs = [[__("Impressions"), data.summary.impressions, 0], [__("Clicks"), data.summary.clicks, 0], [__("CTR"), data.summary.ctr_pct, 2], [__("CPC"), data.summary.cpc, 2], [__("Promotion Expense"), data.summary.amount, 2], [__("Attributed Sales"), data.summary.attributed_sales, 2], [__("Conversion Rate"), data.summary.conversion_rate_pct, 2], [__("ROI"), data.summary.roi, 2]]
		this.$root.find("[data-kpis]").html(specs.map(([label, value, digits]) => `<div class="pa-kpi"><small>${label}</small><strong>${format_number(value || 0, null, digits)}</strong></div>`).join(""))
		const target = this.$root.find("[data-trend]").empty().get(0)
		if (data.trend.length) this.chart = new frappe.Chart(target, { type: "line", height: 260, data: { labels: data.trend.map(r => frappe.datetime.str_to_user(r.date)), datasets: [{ name: __("Promotion Expense"), values: data.trend.map(r => r.amount) }, { name: __("Attributed Sales"), values: data.trend.map(r => r.attributed_sales) }] }, colors: ["#f59e0b", "#2563eb"] })
		const columns = [__("Store"), __("Promotion Scene"), __("Campaign"), __("Product"), __("Impressions"), __("Clicks"), __("CTR"), __("CPC"), __("Promotion Expense"), __("Attributed Sales"), __("Conversion Rate"), __("ROI"), __("Match Status")]
		this.$root.find("[data-head]").html(`<tr>${columns.map(x => `<th>${x}</th>`).join("")}</tr>`)
		this.$root.find("[data-body]").html(data.details.map(row => `<tr><td>${frappe.utils.escape_html(row.store || "")}</td><td>${frappe.utils.escape_html(row.scene || "")}</td><td>${frappe.utils.escape_html(row.campaign_name || row.campaign_id)}</td><td><a data-listing="${frappe.utils.escape_html(row.listing || "")}">${frappe.utils.escape_html(row.subject_name || row.subject_id)}</a></td><td>${format_number(row.impressions, null, 0)}</td><td>${format_number(row.clicks, null, 0)}</td><td>${format_number(row.ctr_pct, null, 2)}%</td><td>${format_number(row.cpc, null, 2)}</td><td>${format_number(row.amount, null, 2)}</td><td>${format_number(row.attributed_sales, null, 2)}</td><td>${format_number(row.conversion_rate_pct, null, 2)}%</td><td>${format_number(row.roi, null, 2)}</td><td>${row.match_status}</td></tr>`).join(""))
		this.$root.find("[data-listing]").on("click", event => { const name = $(event.currentTarget).data("listing"); if (name) frappe.set_route("Form", "Ecommerce Product Listing", name) })
	}
}
