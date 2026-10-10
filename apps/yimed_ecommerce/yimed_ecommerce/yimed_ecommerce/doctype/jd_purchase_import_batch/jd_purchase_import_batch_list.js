const JD_IMPORT_READY_STATUSES = ["导入成功", "部分失败"];
const JD_IMPORT_WORKBOOK_METHOD = "yimed_ecommerce.jd.import_workbooks";
window.__jdListJsVersion = "v20261010-1";

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
			// 一步到位：后端创建批次并立即导入。不再通过 frappe.route_options
			// 预填 new 表单——残留的 route_options 会泄漏成列表页过滤器，
			// 让“导入成功”的批次被“待导入”筛选隐藏。
			frappe.call({
				method:
					"yimed_ecommerce.yimed_ecommerce.doctype.jd_purchase_import_batch.jd_purchase_import_batch.create_and_import_batch",
				args: {
					company: values.company,
					file_url: values.file_url,
					batch_code: values.batch_code,
				},
				freeze: true,
				freeze_message: __("正在创建批次并导入采购单..."),
			}).then((response) => {
				if (response.exc) return;
				const result = response.message || {};
				const batch_name = result.batch_name;
				const failed = result.failed_count || 0;
				dialog.hide();
				if (!batch_name) {
					frappe.msgprint({
						title: __("创建失败"),
						message: result.error || __("未知错误"),
						indicator: "red",
					});
					return;
				}
				if (failed) {
					frappe.msgprint({
						title: __("导入失败"),
						message: __("全部 {0} 条采购单均未导入。原因：{1}", [
							failed,
							result.error || __("未知"),
						]),
						indicator: "red",
					});
					frappe.set_route("Form", "JD Purchase Import Batch", batch_name);
				} else {
					frappe.show_alert({
						message: __("导入成功：共 {0} 条采购单，正在进入工作台...", [result.success_count || 0]),
						indicator: "green",
					});
					window.location.assign(
						`/app/jd-purchase-workbench?import_batch=${encodeURIComponent(batch_name)}`
					);
				}
			});
		},
	});
	dialog.show();
}

frappe.listview_settings["JD Purchase Import Batch"] = {
	add_fields: ["status", "failed_count"],

	onload(listview) {
		// Desk 保留并隐藏旧页面，必须使用本列表自己的主区域。
		jd_preview_state.section = listview.page.main.get(0);
		// 每页 10 条 + frappe 内置分页，避免批次多时把明细预览区挤到很远处
		listview.page_length = 10;
		listview.refresh();

		// 列表刷新/翻页后恢复选中高亮和明细区位置（rAF 合并，避免 observer 循环）
		const target = jd_preview_state.section;
		if (!target.dataset.jdSelectionObserver) {
			target.dataset.jdSelectionObserver = "1";
			let mo_scheduled = false;
			new MutationObserver(() => {
				if (mo_scheduled) return;
				mo_scheduled = true;
				requestAnimationFrame(() => {
					mo_scheduled = false;
					jd_preview_restore_selection();
				});
			}).observe(target, { childList: true, subtree: true });
		}

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

// ---------------------------------------------------------------------------
// 上下结构预览：点击批次行，列表下方展示两个 Tab
//   Tab1 采购订单列表   Tab2 全批次合并明细（组合装拆到明细物料）
// 用 document 级 capture 委托实现——不依赖 onload 时机和列表 DOM 重建，
// 彻底避开浏览器缓存旧脚本导致的“点击没反应”。
// ---------------------------------------------------------------------------

const jd_preview_state = { batch: null, tab: "orders", cache: {}, section: null };
// 诊断钩子：观察点击处理器的执行路径
window.__jdPreviewDebug = { handlerCalls: 0, activeChecks: 0, earlyReturns: [], renders: 0, lastError: null };

// 样式独立注入：frappe.dom.set_style 幂等（同内容只注入一次），样式更新随脚本即时生效，
// 不再受"明细容器单例带着创建时的旧 <style>" 影响
function jd_preview_inject_styles() {
	frappe.dom.set_style(`
		.jd-preview-card { border:1px solid #d1d8dd; border-radius:4px; padding:10px; background:#fff; margin-bottom:12px; }
		.jd-batch-preview table { width:100%; table-layout:fixed; }
		.jd-batch-preview table td, .jd-batch-preview table th { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
		.jd-batch-preview table td:last-child, .jd-batch-preview table th:last-child { text-align:right; }
		.list-row.jd-selected-row, .list-item.jd-selected-row { background:#e8f4fd !important; box-shadow: inset 3px 0 0 #2490ef; }
		/* 上下分屏：整页锁定为视口高度，列表区和明细区各占一半（剩余），各自滚动 */
		.layout-main-section.jd-split-layout { display:flex; flex-direction:column; height:calc(100vh - 60px); overflow:hidden; padding-bottom:0 !important; }
		.layout-main-section.jd-split-layout .page-form { flex:0 0 auto; }
		.layout-main-section.jd-split-layout .frappe-list { flex:1 1 50%; min-height:0; display:flex; flex-direction:column; overflow:hidden; }
		.layout-main-section.jd-split-layout .frappe-list .result-container { flex:1 1 auto; min-height:0; display:flex; flex-direction:column; }
		.layout-main-section.jd-split-layout .frappe-list .result { flex:1 1 auto; min-height:0 !important; height:auto !important; max-height:none !important; overflow-y:auto !important; }
		.layout-main-section.jd-split-layout .frappe-list .list-pagination-wrapper { flex:0 0 auto; margin:4px 0; }
		/* 明细区：外层不滚（头部/Tabs 常驻），只有内层表格区滚动 */
		/* 与上方 .frappe-list 共用左右间距，标题、操作按钮、标签和表格整体对齐。 */
		.layout-main-section.jd-split-layout > .jd-batch-preview { flex:1 1 50%; min-height:0; overflow:hidden; display:flex; flex-direction:column; margin:10px var(--margin-md) 0; }
		.jd-batch-preview .jd-preview-head, .jd-batch-preview .jd-preview-tabs { flex:0 0 auto; }
		.jd-batch-preview .jd-preview-content { flex:1 1 auto; min-height:0; max-height:none !important; overflow-y:auto !important; }
		/* 细滚动条：不占表格列宽，视觉干扰最小（Chrome/Edge + Firefox） */
		.jd-preview-content::-webkit-scrollbar, .layout-main-section .result::-webkit-scrollbar { width:8px; height:8px; }
		.jd-preview-content::-webkit-scrollbar-track, .layout-main-section .result::-webkit-scrollbar-track { background:transparent; }
		.jd-preview-content::-webkit-scrollbar-thumb, .layout-main-section .result::-webkit-scrollbar-thumb { background:#c8cdd3; border-radius:4px; }
		.jd-preview-content::-webkit-scrollbar-thumb:hover, .layout-main-section .result::-webkit-scrollbar-thumb:hover { background:#a8aeb5; }
		.jd-preview-content, .layout-main-section .result { scrollbar-width:thin; scrollbar-color:#c8cdd3 transparent; }
		/* 列宽收紧：编号列定宽可省略，数字列允许收缩，按钮列始终钉在视口内 */
		.list-row .list-subject, .list-item .list-subject { flex: 0 0 220px !important; min-width: 0 !important; max-width: 220px !important; }
		.list-row .list-subject a, .list-item .list-subject a { display: inline-block; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; }
		.list-row .list-row-col, .list-item .list-row-col,
		.list-row-head .list-row-col, .list-head .list-row-col { min-width: 0 !important; max-width: 60px !important; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
		.list-row-head .list-subject, .list-head .list-subject { min-width: 0 !important; max-width: 220px !important; overflow: hidden; }
		.list-row .level-right, .list-item .level-right { flex: 0 0 auto !important; margin-left: auto !important; }
	`);
}

function jd_preview_is_active() {
	window.__jdPreviewDebug.activeChecks++;
	// frappe.get_route() 返回 router.current_route，切页过渡期可能是 undefined，
	// 直接读 route[0] 会抛 "Cannot read properties of undefined (reading '0')"
	const route = (frappe.get_route && frappe.get_route()) || [];
	// 兼容两种路由形态：标准 ["List", doctype] 与 /desk/{slug} 直达路由，
	// 后者在切页/前进后退后可能出现，不识别会导致点击行时明细区静默不渲染
	const active =
		(route[0] === "List" && route[1] === "JD Purchase Import Batch") ||
		route[0] === "jd-purchase-import-batch";
	if (!active && window.__jdPreviewDebug.earlyReturns.length < 5) {
		window.__jdPreviewDebug.earlyReturns.push(`路由不匹配: ${JSON.stringify(route)}`);
	}
	return active;
}

function jd_preview_render(batch_name, row) {
	const section = jd_preview_state.section;
	if (!section || !jd_preview_is_active()) return;
	jd_preview_state.batch = batch_name;
	// 样式独立注入（frappe.dom.set_style 幂等）：容器是单例，若把样式写死在容器里，
	// 旧容器重建前永远带着旧样式，后续 CSS 改进无法生效。
	jd_preview_inject_styles();
	// 容器单例：始终紧跟当前列表容器之后
	let container = section.querySelector(".jd-batch-preview");
	if (container) {
		// 清掉旧版本脚本遗留在容器里的内联 <style>（样式已改为集中注入）
		container.querySelectorAll("style").forEach((s) => s.remove());
	}
	if (!container) {
		container = document.createElement("div");
		container.className = "jd-batch-preview";
		container.innerHTML = `
			<div class="jd-preview-head" style="display:flex;align-items:center;gap:10px;margin:16px 0 8px;">
				<h5 style="margin:0;">${__("批次明细预览")}</h5>
				<span class="jd-preview-batch text-muted" style="font-size:12px;"></span>
				<span style="flex:1;"></span>
				<a class="btn btn-xs btn-default jd-preview-open-workbench" href="javascript:void(0)">${__("在工作台打开")}</a>
				<a class="btn btn-xs btn-default jd-preview-open-form" href="javascript:void(0)">${__("编辑批次")}</a>
			</div>
			<div class="jd-preview-tabs" style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap;">
				<button class="btn btn-sm jd-preview-tab" data-tab="orders">${__("采购订单列表")}</button>
				<button class="btn btn-sm jd-preview-tab" data-tab="summary">${__("领料汇总清单")}</button>
				<button class="btn btn-sm jd-preview-tab" data-tab="bundles">${__("组装清单")}</button>
				<button class="btn btn-sm jd-preview-tab" data-tab="sorting">${__("分拣清单")}</button>
			</div>
			<div class="jd-preview-content" style="max-height:480px;overflow:auto;border:1px solid #d1d8dd;border-radius:4px;padding:8px;background:#fff;"></div>`;
		container.addEventListener("click", (event) => {
			const tab = event.target.closest(".jd-preview-tab");
			if (tab) {
				jd_preview_state.tab = tab.dataset.tab;
				jd_preview_render(jd_preview_state.batch);
				return;
			}
			if (event.target.closest(".jd-preview-open-workbench")) {
				open_jd_purchase_workbench({ name: jd_preview_state.batch, status: "导入成功" });
				return;
			}
			if (event.target.closest(".jd-preview-open-form")) {
				frappe.set_route("Form", "JD Purchase Import Batch", jd_preview_state.batch);
			}
		});
	}
	jd_preview_pin_container(container);
	// 外层 flex 列布局（内联优先级最高，确保旧容器也被纠正）：头部/Tabs 常驻，内层表格区独占剩余高度
	container.style.display = "flex";
	container.style.flexDirection = "column";
	container.style.overflow = "hidden";
	container.querySelector(".jd-preview-batch").textContent = batch_name;
	container.querySelectorAll(".jd-preview-tab").forEach((btn) => {
		btn.classList.toggle("btn-primary", btn.dataset.tab === jd_preview_state.tab);
		btn.classList.toggle("btn-default", btn.dataset.tab !== jd_preview_state.tab);
	});

	const content = container.querySelector(".jd-preview-content");
	const data = jd_preview_state.cache[batch_name];
	if (!data) {
		content.innerHTML = `<div class="text-muted" style="padding:8px;">${__("正在加载批次预览...")}</div>`;
		frappe
			.xcall("yimed_ecommerce.jd.workflow.get_batch_preview", { import_batch: batch_name })
			.then((result) => {
				// 只缓存有效结果，防止空结果导致的"加载中"死循环
				if (result && typeof result === "object") {
					jd_preview_state.cache[batch_name] = result;
					// 请求返回时可能已经切换批次或离开列表，只缓存旧结果。
					if (jd_preview_state.batch === batch_name && jd_preview_is_active()) {
						jd_preview_render(batch_name, row);
					}
				}
			})
			.catch((err) => {
				console.error("[JD_PREVIEW] xcall 失败:", err, JSON.stringify(err));
				if (jd_preview_state.batch !== batch_name || !jd_preview_is_active()) return;
				const msg = (err && err.message) || String(err || "") || __("加载失败");
				content.innerHTML = `<div class="text-danger" style="padding:8px;">${frappe.utils.escape_html(msg)}</div>`;
			});
		return;
	}

	const unmapped = data.unmapped || [];
	const unmapped_block = unmapped.length
		? `<div style="margin-bottom:10px;">
			<div class="text-warning" style="margin-bottom:4px;">${__("⚠ {0} 个京东SKU未映射，共 {1} 件。", [unmapped.length, unmapped.reduce((s, r) => s + r.total_qty, 0)])}</div>
			<table class="table table-bordered table-condensed" style="margin:0;">
				<thead><tr class="text-muted"><th>${__("京东SKU")}</th><th>${__("商品名称")}</th><th class="text-right">${__("数量")}</th></tr></thead>
				<tbody>${unmapped.map((r) => `<tr><td>${frappe.utils.escape_html(r.jd_sku)}</td><td>${frappe.utils.escape_html(r.jd_item_name || "—")}</td><td class="text-right">${r.total_qty}</td></tr>`).join("")}</tbody>
			</table></div>`
		: "";

	if (jd_preview_state.tab === "orders") {
		const report_cell = (o) => {
			const purchase = Number(o.total_purchase_qty || 0);
			const report = Number(o.report_qty ?? purchase);
			if (Math.abs(report - purchase) > 1e-9) {
				return `<td class="text-right text-danger" title="${frappe.utils.escape_html(
					__("回告数量 {0} 与采购数量 {1} 不一致", [report, purchase])
				)}">${report}</td>`;
			}
			return `<td class="text-right">${report}</td>`;
		};
		const order_rows = data.orders.length
			? data.orders
					.map(
						(o) => `<tr>
							<td><a href="/app/jd-purchase-order/${encodeURIComponent(o.name)}">${frappe.utils.escape_html(o.name)}</a></td>
							<td>${frappe.utils.escape_html(o.mapping_status || "—")}</td>
							<td>${frappe.utils.escape_html(o.status || "—")}</td>
							<td>${frappe.utils.escape_html(o.destination_city || "—")}</td>
							<td>${frappe.utils.escape_html(o.jd_warehouse || "—")}</td>
							<td class="text-right">${o.total_purchase_qty}</td>
							${report_cell(o)}
						</tr>`
					)
					.join("")
			: `<tr><td colspan="7" class="text-muted text-center">${__("本批次没有采购单")}</td></tr>`;
		content.innerHTML = `<table class="table table-bordered" style="margin:0;">
			<thead><tr><th>${__("采购单")}</th><th>${__("映射状态")}</th><th>${__("状态")}</th><th>${__("目的城市")}</th><th>${__("京东仓")}</th><th class="text-right">${__("采购数量")}</th><th class="text-right">${__("回告数量")}</th></tr></thead>
			<tbody>${order_rows}</tbody></table>`;
		return;
	}

	// ---- 组装清单 / 分拣清单 / 领料汇总 ----
	const bundle_list = data.bundle_list || [];
	const sorting_list = data.sorting_list || [];
	const product_summary = data.product_summary || [];

	if (jd_preview_state.tab === "bundles") {
		const bundle_block = bundle_list.length
			? bundle_list
					.map(
						(b) => `<div class="jd-preview-card">
							<div style="margin-bottom:6px;">
								<strong>${frappe.utils.escape_html(b.platform_item)}</strong>
								<span class="text-muted">· ${frappe.utils.escape_html(b.jd_item_name || "—")}</span>
								· ${__("共 {0} 套", [b.total_qty])} ${frappe.utils.escape_html(b.uom || "")}
							</div>
							<table class="table table-bordered table-condensed" style="margin:0;">
								<thead><tr class="text-muted"><th>${__("组件（每套配比）")}</th><th>${__("物料名称")}</th><th class="text-right">${__("每套")}</th><th class="text-right">${__("合计")}</th></tr></thead>
								<tbody>${(b.components || [])
									.map(
										(c) => `<tr>
											<td>${frappe.utils.escape_html(c.stock_item)}</td>
											<td>${frappe.utils.escape_html(c.stock_item_name || "—")}</td>
											<td class="text-right">${c.per_set}</td>
											<td class="text-right">${c.total_qty}</td>
										</tr>`
									)
									.join("")}</tbody>
							</table>
						</div>`
					)
					.join("")
			: `<div class="jd-preview-card text-muted" style="padding:8px;">${__("本批次没有组合装商品。")}</div>`;
		content.innerHTML = bundle_block;
		return;
	}

	if (jd_preview_state.tab === "sorting") {
		const sorting_rows = sorting_list.length
			? sorting_list
					.map(
						(r) => `<tr>
							<td><a href="/app/jd-purchase-order/${encodeURIComponent(r.purchase_order)}">${frappe.utils.escape_html(r.purchase_order)}</a></td>
							<td>${frappe.utils.escape_html(r.warehouse)}</td>
							<td>${frappe.utils.escape_html(r.platform_item)}${r.is_bundle ? ` <span class="text-muted" style="font-size:11px;">(${__("组合装")})</span>` : ""}</td>
							<td>${frappe.utils.escape_html(r.jd_item_name || "—")}</td>
							<td>${frappe.utils.escape_html(r.uom || "—")}</td>
							<td class="text-right">${r.total_qty}</td>
						</tr>`
					)
					.join("")
			: `<tr><td colspan="6" class="text-muted text-center">${__("无分拣数据。")}</td></tr>`;
		content.innerHTML = `<table class="table table-bordered" style="margin:0;">
			<thead><tr><th>${__("采购单")}</th><th>${__("京东仓")}</th><th>${__("商品编码")}</th><th>${__("商品名称")}</th><th>${__("单位")}</th><th class="text-right">${__("分拣数量")}</th></tr></thead>
			<tbody>${sorting_rows}</tbody></table>`;
		return;
	}

	const summary_rows = product_summary.length
		? product_summary
				.map(
					(r) => `<tr>
						<td>${frappe.utils.escape_html(r.stock_item)}${r.from_bundle ? ` <span class="text-muted" style="font-size:11px;">(${__("组合装组件")})</span>` : ""}</td>
						<td>${frappe.utils.escape_html(r.stock_item_name || "—")}</td>
						<td>${frappe.utils.escape_html(r.uom || "—")}</td>
						<td class="text-right">${r.total_qty}</td>
					</tr>`
				)
				.join("")
		: `<tr><td colspan="4" class="text-muted text-center">${__("无已映射明细")}</td></tr>`;
	content.innerHTML = `<table class="table table-bordered" style="margin:0;">
		<thead><tr><th>${__("ERP物料编码")}</th><th>${__("物料名称")}</th><th>${__("单位")}</th><th class="text-right">${__("领用数量")}</th></tr></thead>
		<tbody>${summary_rows}</tbody></table>`;
}

// document 级 capture 委托：在任何 DOM 重建、任何缓存状态下都稳定生效
// 守卫用"行归属"判断（[data-doctype] 容器），不再依赖 frappe.get_route() 的形态——
// 从其他页面切回时路由形态可能短暂异常，导致点击被静默忽略（明细不显示）
document.addEventListener(
	"click",
	(event) => {
		window.__jdPreviewDebug.handlerCalls++;
		try {
			if (event.target.closest(".list-row-checkbox, input[type=checkbox], .btn-action")) {
				if (window.__jdPreviewDebug.earlyReturns.length < 5)
					window.__jdPreviewDebug.earlyReturns.push("点击落在复选框/按钮上");
				return;
			}
			const row = event.target.closest(".list-row") || event.target.closest(".list-item");
			if (!row) {
				if (window.__jdPreviewDebug.earlyReturns.length < 5)
					window.__jdPreviewDebug.earlyReturns.push(`未找到列表行: target=${event.target.tagName}.${String(event.target.className).slice(0, 30)}`);
				return;
			}
			// 行归属本列表才处理（行内复选框带 data-doctype 标记；其他 doctype 列表的行不受影响）。
			// 不用 frappe.get_route() 判断——切页过渡期路由形态可能短暂异常，导致点击被静默忽略
			const row_dt = row.querySelector("[data-doctype]")?.dataset?.doctype;
			if (row_dt !== "JD Purchase Import Batch") {
				if (window.__jdPreviewDebug.earlyReturns.length < 5)
					window.__jdPreviewDebug.earlyReturns.push(`行不属于本列表: ${row_dt}`);
				return;
			}
			const named = row.querySelector("[data-name]") || event.target.closest("[data-name]");
			const name = named && named.dataset.name;
			if (!name) {
				if (window.__jdPreviewDebug.earlyReturns.length < 5)
					window.__jdPreviewDebug.earlyReturns.push("行内无 data-name");
				return;
			}
			event.preventDefault();
			event.stopPropagation();
			event.stopImmediatePropagation();
			// 选中高亮：只给点击的行加背景色，列表本身不移动
			jd_preview_state.section.querySelectorAll(".list-row.jd-selected-row, .list-item.jd-selected-row").forEach((r) => {
				r.classList.remove("jd-selected-row");
			});
			row.classList.add("jd-selected-row");
			jd_preview_state.tab = "orders";
			window.__jdPreviewDebug.renders++;
			jd_preview_render(name, row);
		} catch (err) {
			window.__jdPreviewDebug.lastError = String(err && err.stack || err).slice(0, 300);
			console.error("[JD_LIST] 点击处理异常:", err);
		}
	},
	true
);

// 加载即打版本日志：排查"浏览器还在跑旧脚本"类问题
console.info(`[JD_LIST] 批次列表脚本已加载 ${window.__jdListJsVersion}`);

// 列表刷新/翻页后 DOM 重建，恢复选中行的高亮和明细区位置
// 明细区挂在页面主区块（.layout-main-section，纵向布局）末尾，始终全宽显示在列表
// 下方。之前的实现挂在 .result 的直接父容器末尾——该容器是横向排列，明细会被
// 排到视口右侧（x>1200），看起来像"明细不显示"。
// 注意：只在容器脱离父容器时才移动一次——频繁"抢占末尾"会和 frappe 自身的
// DOM 更新互相触发 MutationObserver，造成页面卡死（点击无响应）。
function jd_preview_pin_container(container) {
	const section = jd_preview_state.section;
	if (!section || !jd_preview_is_active()) return;
	// 启用上下分屏布局（列表/明细各占一半）
	section.classList.add("jd-split-layout");
	if (container.parentElement !== section) {
		section.appendChild(container);
	}
}

function jd_preview_restore_selection() {
	const section = jd_preview_state.section;
	if (!section || !jd_preview_is_active()) return;
	// 隐藏页面由 Desk 保留；只恢复本列表，不修改其他页面。
	section?.classList.add("jd-split-layout");
	if (!jd_preview_state.batch) return;
	section
		.querySelectorAll(".list-row.jd-selected-row, .list-item.jd-selected-row")
		.forEach((r) => r.classList.remove("jd-selected-row"));
	const escaped = (window.CSS && CSS.escape) ? CSS.escape(jd_preview_state.batch) : jd_preview_state.batch;
	const el =
		section.querySelector(`.list-row [data-name="${escaped}"]`) ||
		section.querySelector(`.list-item [data-name="${escaped}"]`);
	const row = el && (el.closest(".list-row") || el.closest(".list-item"));
	if (row) row.classList.add("jd-selected-row");
	// frappe 列表自动刷新会清空重建列表区域，明细容器一并被移除——重建（走缓存，瞬时恢复）
	const container = section.querySelector(".jd-batch-preview");
	if (!container) {
		jd_preview_render(jd_preview_state.batch, row);
		return;
	}
	jd_preview_pin_container(container);
}
