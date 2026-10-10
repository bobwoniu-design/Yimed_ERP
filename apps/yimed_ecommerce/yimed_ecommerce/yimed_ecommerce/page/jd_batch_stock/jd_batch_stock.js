const JD_BATCH_STOCK_CONTRACT = Object.freeze({
	get_batch_stock: "yimed_ecommerce.jd.batch_stock.get_batch_stock",
	set_inspection_attachment: "yimed_ecommerce.jd.batch_stock.set_inspection_attachment",
	download_inspection_reports: "yimed_ecommerce.jd.batch_stock.download_inspection_reports",
});

frappe.pages["jd-batch-stock"].on_page_load = (wrapper) => {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("批次库存与检测报告"), single_column: true });
	wrapper.jd_batch_stock = new JDBatchStockPage(wrapper, page);
};

frappe.pages["jd-batch-stock"].on_page_show = (wrapper) => {
	wrapper.jd_batch_stock?.show();
};

class JDBatchStockPage {
	constructor(wrapper, page) {
		this.wrapper = wrapper;
		this.page = page;
		this.state = { rows: [], truncated: false, limit: 500, loaded: false };
		this.$root = $('<div class="jd-batch-stock"></div>').appendTo(page.main.empty());
		this.make_toolbar();
		this.make_filters();
		this.bind_events();
	}

	make_toolbar() {
		this.page.set_primary_action(__("查询"), () => this.search(), "search");
		this.page.add_inner_button(__("重置"), () => this.reset_filters());
		this.page.add_inner_button(__("批量打印检测报告"), () => this.print_selected_reports());
	}

	make_filters() {
		this.$root.html(`
			<div class="jd-bs-filters">
				<div class="row">
					<div class="col-sm-3"><div data-filter="company"></div></div>
					<div class="col-sm-3"><div data-filter="warehouse"></div></div>
					<div class="col-sm-3"><div data-filter="item_code"></div></div>
					<div class="col-sm-3"><input data-filter-input="batch_no" class="form-control input-sm" placeholder="${__("批号关键字")}"></div>
				</div>
				<div class="row">
					<div class="col-sm-3"><input data-filter-input="keyword" class="form-control input-sm" placeholder="${__("关键词：批号 / 物料编码 / 物料名称")}"></div>
					<div class="col-sm-3"><select data-filter-input="report_status" class="form-control input-sm">
						<option value="all">${__("检测报告：全部")}</option>
						<option value="reported">${__("检测报告：已上传")}</option>
						<option value="unreported">${__("检测报告：未上传")}</option>
					</select></div>
					<div class="col-sm-3" style="padding-top: 8px;"><div class="checkbox"><label><input type="checkbox" data-filter-input="only_in_stock" checked> ${__("仅看有库存")}</label></div></div>
				</div>
			</div>
			<div class="jd-bs-result"></div>
		`);
		this.filters = {};
		const host = (fieldname, options, label) => {
			const $host = this.$root.find(`[data-filter='${fieldname}']`);
			const control = frappe.ui.form.make_control({
				parent: $host, render_input: true,
				df: { fieldtype: "Link", options, placeholder: label, filters: fieldname === "company" ? undefined : () => this.link_filters(fieldname) },
			});
			this.filters[fieldname] = control;
		};
		host("company", "Company", __("公司"));
		host("warehouse", "Warehouse", __("仓库"));
		host("item_code", "Item", __("物料"));
		this.filters.company.$input.on("change", () => { this.filters.warehouse.set_value(""); });
		this.$root.on("keydown", "[data-filter-input]", (event) => {
			if (event.key === "Enter") this.search();
		});
	}

	link_filters(fieldname) {
		const company = this.filters.company?.get_value?.();
		if (!company) return {};
		if (fieldname === "warehouse") return { company };
		return {};
	}

	bind_events() {
		this.$root.on("click", "[data-action='upload-attachment']", (event) => this.upload_attachment(event));
		this.$root.on("click", "[data-action='remove-attachment']", (event) => this.remove_attachment(event));
		this.$root.on("change", "[data-select-all]", (event) => {
			this.$root.find("[data-select-row]:not(:disabled)").prop("checked", event.target.checked);
		});
		this.$root.on("change", "[data-select-row]", () => {
			const $rows = this.$root.find("[data-select-row]:not(:disabled)");
			const count = $rows.filter(":checked").length;
			this.$root.find("[data-select-all]").prop("checked", count === $rows.length && count > 0);
		});
	}

	async show() {
		const route = frappe.route_options || {};
		if (route.batch_no) { this.$root.find("[data-filter-input='batch_no']").val(route.batch_no); delete frappe.route_options.batch_no; }
		if (route.item_code) { await this.filters.item_code.set_value(route.item_code); delete frappe.route_options.item_code; }
		if (!this.state.loaded || route.batch_no || route.item_code) await this.search();
	}

	async search() {
		const args = {
			company: this.filters.company.get_value() || "",
			warehouse: this.filters.warehouse.get_value() || "",
			item_code: this.filters.item_code.get_value() || "",
			batch_no: this.$root.find("[data-filter-input='batch_no']").val() || "",
			keyword: this.$root.find("[data-filter-input='keyword']").val() || "",
			only_in_stock: this.$root.find("[data-filter-input='only_in_stock']").is(":checked") ? 1 : 0,
		};
		const status = this.$root.find("[data-filter-input='report_status']").val();
		args.only_reported = status === "reported" ? 1 : 0;
		args.only_unreported = status === "unreported" ? 1 : 0;
		this.render_loading();
		try {
			const data = await frappe.xcall(JD_BATCH_STOCK_CONTRACT.get_batch_stock, args);
			this.state.rows = data.rows || [];
			this.state.truncated = Boolean(data.truncated);
			this.state.limit = data.limit || 500;
			this.state.loaded = true;
			this.render_result();
		} catch (error) {
			this.render_error(error);
		}
	}

	async reset_filters() {
		await this.filters.company.set_value("");
		await this.filters.warehouse.set_value("");
		await this.filters.item_code.set_value("");
		this.$root.find("[data-filter-input='batch_no'],[data-filter-input='keyword']").val("");
		this.$root.find("[data-filter-input='report_status']").val("all");
		this.$root.find("[data-filter-input='only_in_stock']").prop("checked", true);
		await this.search();
	}

	render_loading() {
		this.$root.find(".jd-bs-result").html(`<div class="jd-bs-empty"><span class="jd-bs-spinner"></span><p>${__("正在查询批次库存…")}</p></div>`);
	}

	render_error(error) {
		this.$root.find(".jd-bs-result").html(`<div class="jd-bs-empty is-error"><h3>${__("查询失败")}</h3><p>${this.escape(error?.message || error?.exc || "")}</p></div>`);
	}

	render_result() {
		const rows = this.state.rows;
		if (!rows.length) {
			this.$root.find(".jd-bs-result").html(`<div class="jd-bs-empty"><h3>${__("没有符合条件的批次库存")}</h3><p>${__("请调整筛选条件后重试。")}</p></div>`);
			return;
		}
		const truncate_hint = this.state.truncated ? `<span class="text-warning">${__("结果超过 {0} 行，仅显示前 {0} 行，请缩小筛选范围", [this.state.limit])}</span>` : `<span class="text-muted">${__("共 {0} 行", [rows.length])}</span>`;
		const reported_count = rows.filter((row) => this.has_attachment(row)).length;
		const body = rows.map((row, index) => this.row_html(row, index)).join("");
		this.$root.find(".jd-bs-result").html(`
			<div class="jd-bs-summary">
				<strong>${__("批次库存与检测报告")}</strong>
				${truncate_hint}
				<span class="text-muted" data-reported-count>${__("已上传检测报告 {0} / {1} 批", [reported_count, rows.length])}</span>
				<span class="text-muted">${__("点击「上传」维护检测报告附件，无需填写其他信息；勾选批次后可批量打印检测报告")}</span>
			</div>
			<div class="table-responsive"><table class="table table-hover jd-bs-table">
				<thead><tr>
					<th style="width:30px;"><input type="checkbox" data-select-all aria-label="${__("全选")}"></th>
					<th>${__("物料")}</th><th>${__("批号")}</th><th>${__("仓库")}</th>
					<th class="text-right">${__("库存")}</th><th>${__("有效期")}</th>
					<th style="width:220px;">${__("检测报告附件")}</th>
				</tr></thead>
				<tbody>${body}</tbody>
			</table></div>
			<style>
				.jd-bs-filters { background: #fff; border: 1px solid #e2e2e2; border-radius: 6px; padding: 12px 15px 4px; margin-bottom: 12px; }
				.jd-bs-filters .control-label { font-weight: 600; margin-bottom: 3px; display: block; }
				.jd-bs-summary { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; padding: 6px 2px 10px; }
				.jd-bs-empty { text-align: center; padding: 48px 12px; color: #8a8a8a; }
				.jd-bs-empty.is-error { color: #c92a2a; }
				.jd-bs-spinner { display: inline-block; width: 22px; height: 22px; border: 2px solid #d1d5db; border-top-color: #2563eb; border-radius: 50%; animation: jd-bs-spin 0.8s linear infinite; }
				@keyframes jd-bs-spin { to { transform: rotate(360deg); } }
				.jd-bs-table th { white-space: nowrap; font-size: 12px; }
				.jd-bs-table td { vertical-align: middle; font-size: 12px; }
				.jd-bs-expired { color: #c92a2a; font-weight: 600; }
				.jd-bs-batch { font-family: monospace; }
				.jd-bs-attach { display: flex; align-items: center; gap: 4px; min-width: 0; }
				.jd-bs-attach a { display: inline-block; max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: middle; }
				.jd-bs-item small { display: block; color: #6b7280; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
			</style>
		`);
	}

	has_attachment(row) {
		return Boolean(row.inspection_report?.report_attachment);
	}

	row_html(row, index) {
		const expiry_class = row.expired ? "jd-bs-expired" : "";
		const shelf_title = row.remaining_shelf_life_percent === null || row.remaining_shelf_life_percent === undefined
			? __("无有效期")
			: __("剩余效期 {0}%", [this.format(row.remaining_shelf_life_percent)]) + (row.shelf_life_eligible ? "" : "（不符合京东2/3效期规则）");
		return `<tr data-report-row data-index="${index}" data-batch-no="${this.escape(row.batch_no)}">
			<td><input type="checkbox" data-select-row data-batch-no="${this.escape(row.batch_no)}" ${this.has_attachment(row) ? "" : "disabled title='" + this.escape("未上传检测报告附件，不能打印") + "'"}></td>
			<td class="jd-bs-item"><strong>${this.escape(row.item_code)}</strong><small title="${this.escape(row.item_name || "")}">${this.escape(row.item_name || "")}</small></td>
			<td class="jd-bs-batch">${this.escape(row.batch_no)}</td>
			<td>${this.escape(row.warehouse)}</td>
			<td class="text-right" title="${this.escape("实际 " + this.format(row.actual_qty) + "，备货占用 " + this.format(row.soft_reserved_qty))}">${this.format(row.actual_qty)}</td>
			<td class="${expiry_class}" title="${this.escape(shelf_title)}">${this.escape(row.expiry_date || "—")}${row.expired ? ` (${__("已过期")})` : ""}</td>
			<td>${this.attachment_html(row)}</td>
		</tr>`;
	}

	attachment_html(row) {
		const url = row.inspection_report?.report_attachment || "";
		if (!url) {
			return `<div class="jd-bs-attach"><button class="btn btn-primary btn-xs" data-action="upload-attachment">${__("上传")}</button><span class="indicator-pill orange">${__("未上传")}</span></div>`;
		}
		const name = url.split("/").pop() || url;
		return `<div class="jd-bs-attach">
			<span class="indicator-pill green" title="${this.escape(url)}">${__("已上传")}</span>
			<a href="${this.escape(url)}" target="_blank" title="${this.escape(url)}">${this.escape(name)}</a>
			<button class="btn btn-default btn-xs" data-action="upload-attachment" title="${this.escape("更换附件")}">${__("更换")}</button>
			<button class="btn btn-link btn-xs text-danger" data-action="remove-attachment" title="${this.escape("删除附件")}">${__("删除")}</button>
		</div>`;
	}

	refresh_row(index) {
		const row = this.state.rows[index];
		if (!row) return;
		const $row = this.$root.find(`[data-report-row][data-index='${index}']`);
		if ($row.length) $row.replaceWith(this.row_html(row, index).trim());
		const reported_count = this.state.rows.filter((item) => this.has_attachment(item)).length;
		this.$root.find("[data-reported-count]").text(__("已上传检测报告 {0} / {1} 批", [reported_count, this.state.rows.length]));
	}

	upload_attachment(event) {
		const $row = $(event.currentTarget).closest("[data-report-row]");
		const index = Number($row.attr("data-index"));
		const row = this.state.rows[index];
		if (!row) return;
		// 使用 Frappe 标准上传组件（与 Attach 字段一致）：
		// 自带弹窗、拖拽、进度与错误提示，不限制文件类型（PDF/图片均可）
		new frappe.ui.FileUploader({
			allow_multiple: false,
			allow_web_link: false,
			folder: "Home/Attachments",
			upload_notes: __("支持 PDF、图片等常见格式"),
			restrictions: {},
			on_success: async (file_doc) => {
				const file_url = file_doc?.file_url || "";
				if (!file_url) return;
				try {
					await frappe.xcall(JD_BATCH_STOCK_CONTRACT.set_inspection_attachment, { batch_no: row.batch_no, file_url });
					row.inspection_report = { ...(row.inspection_report || {}), report_attachment: file_url };
					this.refresh_row(index);
					frappe.show_alert({ message: __("批次 {0} 的检测报告已上传", [row.batch_no]), indicator: "green" });
				} catch (error) {
					frappe.msgprint({ title: __("保存失败"), message: this.escape(error?.message || error?.exc || ""), indicator: "red" });
				}
			},
		});
	}

	remove_attachment(event) {
		const $row = $(event.currentTarget).closest("[data-report-row]");
		const index = Number($row.attr("data-index"));
		const row = this.state.rows[index];
		if (!row) return;
		frappe.confirm(__("确定删除批次 {0} 的检测报告附件吗？删除后该批次将恢复为未上传状态。", [row.batch_no]), async () => {
			try {
				await frappe.xcall(JD_BATCH_STOCK_CONTRACT.set_inspection_attachment, { batch_no: row.batch_no, file_url: "" });
				if (row.inspection_report) row.inspection_report.report_attachment = "";
				this.refresh_row(index);
				frappe.show_alert({ message: __("批次 {0} 的检测报告附件已删除", [row.batch_no]), indicator: "green" });
			} catch (error) {
				frappe.msgprint({ title: __("删除失败"), message: this.escape(error?.message || error?.exc || ""), indicator: "red" });
			}
		});
	}

	print_selected_reports() {
		const batch_nos = this.$root.find("[data-select-row]:checked").map((_, element) => $(element).attr("data-batch-no")).get();
		if (!batch_nos.length) return frappe.msgprint(__("请先勾选要打印检测报告的批次（未上传检测报告附件的批次不能勾选）。"));
		frappe.confirm(__("将为 {0} 个批次生成检测报告 PDF（每个批次一页），是否继续？", [batch_nos.length]), () => {
			open_url_post(frappe.request.url, {
				cmd: JD_BATCH_STOCK_CONTRACT.download_inspection_reports,
				batch_nos: JSON.stringify(batch_nos),
			}, true);
		});
	}

	format(value) { return format_number(Number(value || 0), null, 2); }
	escape(value) { return frappe.utils.escape_html(String(value ?? "")); }
}
