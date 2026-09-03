frappe.pages["content-center"].on_page_load = function (wrapper) {
	wrapper.content_center = new ChannelContentCenter(wrapper);
};

frappe.pages["content-center"].on_page_show = function (wrapper) {
	wrapper.content_center?.enter_focus_mode();
	wrapper.content_center?.apply_route_options();
};

class ChannelTagInput {
	constructor(wrapper, initialValue = "", readOnly = false, onChange = null) {
		this.wrapper = $(wrapper);
		this.readOnly = readOnly;
		this.onChange = onChange;
		this.tags = [];
		this.add(initialValue, false);
		this.render();
	}

	changed() {
		if (this.onChange) this.onChange(this.tags.slice());
	}

	normalize(value) {
		return String(value || "").split(/[,，\n\r]+/).map((tag) => tag.trim()).filter(Boolean);
	}

	add(value, notify = true) {
		for (const tag of this.normalize(value)) {
			if (tag.length > 30) {
				if (notify) frappe.show_alert({message: __("单个标签最多 30 个字符"), indicator: "orange"});
				continue;
			}
			if (this.tags.some((current) => current.toLocaleLowerCase() === tag.toLocaleLowerCase())) continue;
			if (this.tags.length >= 20) {
				if (notify) frappe.show_alert({message: __("最多设置 20 个标签"), indicator: "orange"});
				break;
			}
			this.tags.push(tag);
		}
	}

	render() {
		const escape = frappe.utils.escape_html;
		this.wrapper.html(`<div class="cc-token-input ${this.readOnly ? "read-only" : ""}">
			${this.tags.map((tag, index) => `<span class="cc-token"><span>${escape(tag)}</span>${this.readOnly ? "" : `<button type="button" data-token-index="${index}" aria-label="${__("删除标签")}">×</button>`}</span>`).join("")}
			${this.readOnly ? "" : `<input type="text" maxlength="30" placeholder="${__("输入后按 Enter 或逗号")}">`}
		</div>`);
		if (this.readOnly) return;
		const input = this.wrapper.find("input");
		this.wrapper.find("[data-token-index]").on("click", (event) => {
			this.tags.splice(Number($(event.currentTarget).data("token-index")), 1);
			this.render();
			this.changed();
			this.wrapper.find("input").trigger("focus");
		});
		input.on("keydown", (event) => {
			if (event.isComposing) return;
			if (["Enter", ",", "，"].includes(event.key)) {
				event.preventDefault();
				this.commit_input(input);
			} else if (event.key === "Backspace" && !input.val() && this.tags.length) {
				this.tags.pop();
				this.render();
				this.changed();
				this.wrapper.find("input").trigger("focus");
			}
		});
		input.on("paste", (event) => {
			const text = event.originalEvent?.clipboardData?.getData("text") || "";
			if (!/[,，\n\r]/.test(text)) return;
			event.preventDefault();
			this.add(text);
			this.render();
			this.changed();
			this.wrapper.find("input").trigger("focus");
		});
		input.on("blur", () => this.commit_input(input));
	}

	commit_input(input) {
		const value = input.val();
		if (!value) return;
		this.add(value);
		this.render();
		this.changed();
	}

	value() {
		return this.tags.join(",");
	}
}

class ChannelContentCenter {
	constructor(wrapper) {
		this.wrapper = $(wrapper);
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("内容中心"),
			single_column: true,
		});
		this.page.hide_sidebar = true;
		this.current_folder = null;
		this.show_trashed = false;
		this.search = "";
		this.view = "folder";
		this.filters = {};
		this.selected = new Set();
		this.search_results = [];
		this.loadSequence = 0;
		this.preferenceKey = `channel-content-center:${frappe.session.user}:view-preferences`;
		this.folderCollapseKey = `channel-content-center:${frappe.session.user}:folder-collapse`;
		this.collapsedFolders = new Set();
		this.collapsedScope = null;
		this.expandedPathKey = null;
		this.featureFlags = {approval_workflow: false, borrowing: false, responsible_governance: false};
		const preferences = this.load_preferences();
		this.displayMode = ["list", "compact", "standard", "large"].includes(preferences.view_mode) ? preferences.view_mode : "standard";
		this.sortBy = ["title", "rating", "file_size", "modified"].includes(preferences.sort_by) ? preferences.sort_by : "modified";
		this.sortOrder = preferences.sort_order === "asc" ? "asc" : "desc";
		this.setup_page();
		this.bind_events();
		this.wrapper.on("hide.content-center", () => this.leave_focus_mode());
		this.load();
	}

	load_preferences() {
		try { return JSON.parse(localStorage.getItem(this.preferenceKey) || "{}"); } catch (error) { return {}; }
	}

	save_preferences() {
		try {
			localStorage.setItem(this.preferenceKey, JSON.stringify({view_mode: this.displayMode, sort_by: this.sortBy, sort_order: this.sortOrder}));
		} catch (error) {
			// Browser storage may be disabled; the current session still keeps the selection.
		}
	}

	load_folder_collapse_state() {
		const scope = "global";
		if (this.collapsedScope === scope) return;
		this.collapsedScope = scope;
		try {
			const stored = JSON.parse(localStorage.getItem(this.folderCollapseKey) || "{}");
			this.collapsedFolders = new Set(Array.isArray(stored[scope]) ? stored[scope] : []);
		} catch (error) {
			this.collapsedFolders = new Set();
		}
	}

	save_folder_collapse_state() {
		if (!this.collapsedScope) return;
		try {
			const stored = JSON.parse(localStorage.getItem(this.folderCollapseKey) || "{}");
			stored[this.collapsedScope] = [...this.collapsedFolders];
			localStorage.setItem(this.folderCollapseKey, JSON.stringify(stored));
		} catch (error) {
			// The current session still keeps folder state when storage is unavailable.
		}
	}

	enter_focus_mode() {
		if (this.focus_mode_active) return;
		this.focus_mode_active = true;
		this.erp_sidebar_was_visible = frappe.app?.sidebar?.wrapper?.is(":visible") !== false;
		frappe.app?.sidebar?.toggle(true);
		$("body").addClass("cc-content-center-focus");
		this.update_erp_sidebar_button(false);
	}

	leave_focus_mode() {
		if (!this.focus_mode_active) return;
		this.focus_mode_active = false;
		$("body").removeClass("cc-content-center-focus");
		if (this.erp_sidebar_was_visible) frappe.app?.sidebar?.toggle(false);
		this.close_mobile_sidebar();
	}

	toggle_erp_sidebar() {
		const sidebar = frappe.app?.sidebar?.wrapper;
		if (!sidebar) return;
		const should_show = !sidebar.is(":visible");
		frappe.app.sidebar.toggle(!should_show);
		$("body").toggleClass("cc-content-center-focus", !should_show);
		this.update_erp_sidebar_button(should_show);
	}

	update_erp_sidebar_button(visible) {
		this.wrapper.find(".cc-erp-nav-toggle")
			.toggleClass("active", visible)
			.attr("title", visible ? __("收起 ERP 菜单") : __("显示 ERP 菜单"));
	}

	open_mobile_sidebar() {
		this.wrapper.find(".cc-shell").addClass("cc-sidebar-open");
	}

	close_mobile_sidebar() {
		this.wrapper.find(".cc-shell").removeClass("cc-sidebar-open");
	}

	apply_route_options() {
		// Reserved for safe page route options; batch reports now live on Batch.
	}

	setup_page() {
		this.page.set_primary_action(__("上传文件"), () => this.upload_files(), "upload");
		this.page.add_inner_button(__("新建文件夹"), () => this.create_folder(), __("文件"));
		this.rename_folder_button = this.page.add_inner_button(__("重命名当前文件夹"), () => this.rename_current_folder(), __("文件"));
		this.page.add_inner_button(__("移动当前文件夹"), () => this.move_current_folder(), __("文件"));
		this.page.add_inner_button(__("回收站"), () => this.toggle_trash(), __("文件"));
		this.page.add_inner_button(__("文件夹权限"), () => this.open_permissions(), __("设置"));
		this.page.add_inner_button(__("系统巡检"), () => this.show_health(), __("设置"));
		this.rebuild_index_button = this.page.add_inner_button(__("批量重建索引"), () => this.rebuild_content_index(), __("设置"));
		this.rebuild_index_button?.hide();
		this.page.add_inner_button(__("内容中心设置"), () => frappe.set_route("Form", "Channel Content Settings"), __("设置"));

		this.wrapper.find(".layout-main-section").html(`
			<div class="cc-shell">
				<div class="cc-sidebar-overlay"></div>
				<aside class="cc-sidebar">
					<div class="cc-sidebar-brand">
						<div class="cc-brand-copy">
							<strong>${__("内容中心")}</strong>
							<span class="text-muted">${__("企业文件管理")}</span>
						</div>
						<button class="btn btn-default btn-xs cc-erp-nav-toggle" title="${__("显示 ERP 菜单")}">${frappe.utils.icon("menu", "sm")}</button>
						<button class="btn btn-default btn-xs cc-mobile-sidebar-close" title="${__("关闭")}">${frappe.utils.icon("close", "sm")}</button>
					</div>
					<div class="cc-sidebar-heading"><strong>${__("快捷访问")}</strong></div>
					<div class="cc-personal-views">
						<button class="cc-view" data-view="all">${frappe.utils.icon("apps", "sm")}<span>${__("全部文件")}</span></button>
						<button class="cc-view" data-view="favorites">${frappe.utils.icon("star", "sm")}<span>${__("我的收藏")}</span></button>
						<button class="cc-view" data-view="recent">${frappe.utils.icon("history", "sm")}<span>${__("最近使用")}</span></button>
						<button class="cc-view" data-view="mine">${frappe.utils.icon("user", "sm")}<span>${__("我上传的")}</span></button>
						<button class="cc-view" data-view="expiring">${frappe.utils.icon("calendar", "sm")}<span>${__("即将到期")}</span></button>
						<button class="cc-view" data-view="expired">${frappe.utils.icon("warning", "sm")}<span>${__("已过期")}</span></button>
						<button class="cc-view hide" data-view="unassigned" data-optional-feature="responsible_governance">${frappe.utils.icon("user", "sm")}<span>${__("无人负责")}</span></button>
						<button class="cc-view hide" data-view="pending_review" data-optional-feature="approval_workflow">${frappe.utils.icon("check", "sm")}<span>${__("待我审批")}</span></button>
						<button class="cc-view hide" data-view="granted_to_me" data-optional-feature="borrowing">${frappe.utils.icon("share", "sm")}<span>${__("授权给我")}</span></button>
						<button class="cc-view hide" data-view="borrow_review" data-optional-feature="borrowing">${frappe.utils.icon("time", "sm")}<span>${__("借阅审批")}</span></button>
					</div>
					<div class="cc-sidebar-heading cc-folder-heading"><strong>${__("目录")}</strong><span class="cc-folder-heading-actions"><button type="button" data-folder-tree-action="expand" title="${__("全部展开")}">${frappe.utils.icon("unfold", "xs")}</button><button type="button" data-folder-tree-action="collapse" title="${__("全部折叠")}">${frappe.utils.icon("fold", "xs")}</button></span></div>
					<div class="cc-folders"></div>
						<div class="cc-sidebar-footer">
							<button class="cc-sidebar-action hide" data-sidebar-action="knowledge">${frappe.utils.icon("chart", "sm")}<span>${__("知识运营")}</span></button>
						<button class="cc-sidebar-action" data-sidebar-action="trash">${frappe.utils.icon("delete", "sm")}<span>${__("回收站")}</span></button>
						<button class="cc-sidebar-action" data-sidebar-action="settings">${frappe.utils.icon("setting-gear", "sm")}<span>${__("内容中心设置")}</span></button>
					</div>
				</aside>
				<section class="cc-main">
					<div class="cc-main-heading"><button class="btn btn-default btn-sm cc-mobile-sidebar-toggle">${frappe.utils.icon("menu", "sm")} ${__("文件夹")}</button><nav class="cc-breadcrumbs"></nav></div>
					<div class="cc-dashboard"></div>
					<div class="cc-toolbar">
						<div class="cc-search-wrap">
							<input class="form-control cc-search" placeholder="${__("搜索标题、文件名、标签或备注")}">
							<label class="cc-content-search-toggle"><input type="checkbox" class="cc-search-content"> ${__("搜索文件内容")}</label>
						</div>
						<button class="btn btn-default btn-sm cc-toggle-filters">${frappe.utils.icon("filter", "sm")} ${__("筛选")}</button>
						<div class="cc-view-modes" role="group" aria-label="${__("显示方式")}">${[["list", __("列表")], ["compact", __("紧凑")], ["standard", __("标准")], ["large", __("大图")]].map(([mode, label]) => `<button class="btn btn-default btn-sm cc-view-mode" data-display-mode="${mode}" title="${label}">${label}</button>`).join("")}</div>
					</div>
					<div class="cc-active-filters"></div>
					<div class="cc-filter-panel hide">
						<div class="cc-filter-grid">
							<div><label>${__("文件类型")}</label><select class="form-control cc-filter-type"><option value="">${__("全部类型")}</option></select></div>
							<div><label>${__("上传人")}</label><div class="cc-filter-uploader"></div></div>
							<div class="cc-filter-rating-wrap"><label>${__("星级")}</label><select class="form-control cc-filter-rating-mode"><option value="all">${__("不限")}</option><option value="unrated">${__("未评分")}</option><option value="exact">${__("精确星级")}</option><option value="at_least">${__("至少星级")}</option></select><div class="cc-rating-filter" data-rating="0">${[1,2,3,4,5].map((value) => `<button type="button" data-filter-rating="${value}">★</button>`).join("")}</div></div>
							<div class="cc-filter-tags-wrap"><label>${__("标签")}</label><div class="cc-filter-tags"></div><select class="form-control cc-filter-tag-match"><option value="any">${__("匹配任一标签")}</option><option value="all">${__("匹配全部标签")}</option></select><div class="cc-tag-facets"></div></div>
							<div><label>${__("更新开始")}</label><input type="date" class="form-control cc-filter-from"></div>
							<div><label>${__("更新结束")}</label><input type="date" class="form-control cc-filter-to"></div>
							<div><label>${__("负责人")}</label><div class="cc-filter-responsible"></div></div>
							<div><label>${__("所属部门")}</label><div class="cc-filter-department"></div></div>
							<div><label>${__("到期状态")}</label><select class="form-control cc-filter-expiry"><option value="any">${__("全部")}</option><option value="expiring">${__("即将到期")}</option><option value="expired">${__("已过期")}</option><option value="no_expiry">${__("未设置")}</option></select></div>
							<div><label>${__("锁定状态")}</label><select class="form-control cc-filter-lock"><option value="">${__("全部")}</option><option value="locked">${__("已锁定")}</option><option value="unlocked">${__("未锁定")}</option></select></div>
							<div class="cc-filter-publication-wrap hide"><label>${__("审批状态")}</label><select class="form-control cc-filter-publication"><option value="">${__("全部")}</option><option value="Draft">${__("草稿")}</option><option value="Pending">${__("待审批")}</option><option value="Published">${__("已发布")}</option><option value="Rejected">${__("已驳回")}</option></select></div>
							<label class="cc-all-folders"><input type="checkbox" class="cc-filter-all"> ${__("搜索所有可访问文件夹")}</label>
						</div>
						<div class="cc-filter-actions"><button class="btn btn-primary btn-sm cc-apply-filters">${__("应用筛选")}</button><button class="btn btn-default btn-sm cc-clear-filters">${__("清除")}</button></div>
					</div>
					<div class="cc-summary-row">
						<div class="cc-summary text-muted"></div>
						<div class="cc-sort-controls"><select class="form-control cc-sort-by"><option value="modified">${__("更新时间")}</option><option value="title">${__("名称")}</option><option value="rating">${__("星级")}</option><option value="file_size">${__("大小")}</option></select><button class="btn btn-default btn-xs cc-sort-order" data-sort-order="desc" title="${__("切换排序方向")}">↓</button></div>
						<label class="cc-select-wrap"><input type="checkbox" class="cc-select-all"> ${__("全选本页")}</label>
					</div>
					<div class="cc-bulk-bar hide">
						<span class="cc-selected-count"></span>
						<button class="btn btn-default btn-xs" data-cc-bulk="move">${__("移动")}</button>
						<button class="btn btn-default btn-xs" data-cc-bulk="copy">${__("复制")}</button>
						<button class="btn btn-default btn-xs" data-cc-bulk="download">${__("批量下载")}</button>
						<button class="btn btn-default btn-xs" data-cc-governance="responsible_user">${__("设置负责人")}</button>
						<button class="btn btn-default btn-xs" data-cc-governance="department">${__("设置部门")}</button>
						<button class="btn btn-default btn-xs" data-cc-governance="expires_on">${__("设置到期日")}</button>
						<button class="btn btn-default btn-xs" data-cc-governance="tags">${__("设置标签")}</button>
						<button class="btn btn-default btn-xs" data-cc-governance="lock">${__("批量锁定")}</button>
						<button class="btn btn-default btn-xs" data-cc-governance="unlock">${__("批量解锁")}</button>
						<button class="btn btn-default btn-xs" data-cc-bulk="trash">${__("移入回收站")}</button>
						<button class="btn btn-default btn-xs" data-cc-bulk="restore">${__("批量恢复")}</button>
						<button class="btn btn-danger btn-xs" data-cc-bulk="permanent-delete">${__("彻底删除")}</button>
						<button class="btn btn-link btn-xs cc-clear-selection">${__("取消选择")}</button>
					</div>
					<div class="cc-assets"></div>
				</section>
			</div>
		`);
		this.uploader_filter = frappe.ui.form.make_control({
			parent: this.wrapper.find(".cc-filter-uploader"),
			df: {fieldtype: "Link", options: "User", fieldname: "uploaded_by", placeholder: __("全部用户")},
			render_input: true,
		});
		this.responsible_filter = frappe.ui.form.make_control({
			parent: this.wrapper.find(".cc-filter-responsible"),
			df: {fieldtype: "Link", options: "User", fieldname: "responsible_user", placeholder: __("全部负责人")},
			render_input: true,
		});
		this.department_filter = frappe.ui.form.make_control({
			parent: this.wrapper.find(".cc-filter-department"),
			df: {fieldtype: "Link", options: "Department", fieldname: "department", placeholder: __("全部部门")},
			render_input: true,
		});
		this.tagFilter = new ChannelTagInput(this.wrapper.find(".cc-filter-tags"), "", false, () => this.render_tag_facets());
		this.wrapper.find(".cc-view-mode").removeClass("active").filter(`[data-display-mode='${this.displayMode}']`).addClass("active");
		this.wrapper.find(".cc-sort-by").val(this.sortBy);
		this.wrapper.find(".cc-sort-order").attr("data-sort-order", this.sortOrder).text(this.sortOrder === "asc" ? "↑" : "↓");
		this.add_styles();
	}

	add_styles() {
		if (document.getElementById("channel-content-center-style")) return;
		$("head").append(`<style id="channel-content-center-style">
			body.cc-content-center-focus .page-container[data-page-route="content-center"]>.page-body>.container{max-width:none;padding-left:12px;padding-right:12px}
			body.cc-content-center-focus .page-container[data-page-route="content-center"] .layout-main-section{padding-left:0;padding-right:0}
			.cc-shell{position:relative;display:grid;grid-template-columns:270px 1fr;min-height:calc(100vh - 105px);border:1px solid var(--border-color);border-radius:var(--border-radius-md);overflow:hidden;background:var(--card-bg)}
			.cc-sidebar{display:flex;flex-direction:column;padding:12px 10px 10px;border-right:1px solid var(--border-color);background:var(--subtle-fg);overflow:auto}.cc-sidebar-brand{display:flex;align-items:flex-start;gap:8px;padding:2px 6px 14px;margin-bottom:10px;border-bottom:1px solid var(--border-color)}.cc-brand-copy{display:flex;flex:1;min-width:0;flex-direction:column}.cc-brand-copy strong{font-size:14px}.cc-brand-copy>span{font-size:11px}.cc-company-label{margin:9px 0 3px;font-size:10px;color:var(--text-muted)}.cc-company-scope-wrap{margin:7px 0 0}.cc-company-scope-wrap>span{display:block;margin-bottom:3px;font-size:10px;color:var(--text-muted)}.cc-company-scope{height:30px}.cc-sidebar-company-field .frappe-control,.cc-mobile-company-field .frappe-control{margin:0}.cc-sidebar-company-field .control-label,.cc-mobile-company-field .control-label{display:none}.cc-mobile-company{display:none;min-width:190px}.cc-mobile-company>label{display:block;margin:0 0 2px;font-size:10px;color:var(--text-muted)}.cc-group-scope-badge{flex:0 0 auto;margin-top:6px}.cc-sidebar-heading{display:flex;flex-direction:column;padding:0 8px 6px;font-size:11px;color:var(--text-muted)}.cc-folder-heading{padding-top:14px;padding-bottom:5px;border-top:1px solid var(--border-color);margin-top:8px}.cc-erp-nav-toggle.active{background:var(--control-bg)}.cc-mobile-sidebar-close,.cc-mobile-sidebar-toggle{display:none}
			.cc-folder-row{display:flex;align-items:center;width:100%;border-radius:6px}.cc-folder-toggle{flex:0 0 22px;width:22px;height:28px;border:0;background:transparent;padding:0;color:var(--text-muted);font-size:16px;line-height:1}.cc-folder-toggle.placeholder{visibility:hidden}.cc-folder{display:flex;align-items:center;justify-content:flex-start;gap:8px;flex:1;min-width:0;border:0;background:transparent;padding:8px 6px;border-radius:6px;text-align:left;color:var(--text-color)}.cc-view{display:flex;align-items:center;justify-content:flex-start;gap:8px;width:100%;border:0;background:transparent;padding:8px;border-radius:6px;text-align:left;color:var(--text-color)}.cc-folder>.icon,.cc-view>.icon,.cc-sidebar-action>.icon{flex:0 0 16px;width:16px;height:16px;margin:0!important}.cc-folder span,.cc-view span,.cc-sidebar-action span{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.cc-folder-row:hover,.cc-folder.active,.cc-view:hover,.cc-view.active{background:var(--control-bg)}.cc-folder-menu-trigger{flex:0 0 24px;width:24px;height:26px;border:0;background:transparent;color:var(--text-muted);border-radius:5px;padding:0;opacity:0}.cc-folder-row:hover>.cc-folder-menu-trigger,.cc-folder-row:focus-within>.cc-folder-menu-trigger,.cc-directory-card:hover .cc-folder-menu-trigger,.cc-directory-card:focus-within .cc-folder-menu-trigger,.cc-directory-row:hover .cc-folder-menu-trigger,.cc-directory-row:focus-within .cc-folder-menu-trigger{opacity:1}.cc-folder-menu-trigger:hover{background:var(--control-bg);color:var(--text-color)}.cc-folder-context-menu{position:fixed;z-index:1060;min-width:170px;padding:5px;border:1px solid var(--border-color);border-radius:8px;background:var(--card-bg);box-shadow:var(--shadow-lg)}.cc-folder-context-menu button{display:flex;width:100%;border:0;background:transparent;padding:7px 9px;border-radius:5px;text-align:left;color:var(--text-color)}.cc-folder-context-menu button:hover,.cc-folder-context-menu button:focus{background:var(--control-bg)}.cc-folder-context-menu button.danger{color:var(--red-600)}
			.cc-folder-heading{flex-direction:row;align-items:center;justify-content:space-between}.cc-folder-heading-actions{display:flex;gap:2px}.cc-folder-heading-actions button{border:0;background:transparent;padding:2px 4px;color:var(--text-muted);border-radius:4px}.cc-folder-heading-actions button:hover{background:var(--control-bg);color:var(--text-color)}.cc-folders{flex:1}.cc-sidebar-footer{display:grid;gap:3px;padding-top:10px;margin-top:10px;border-top:1px solid var(--border-color)}.cc-sidebar-action{display:flex;align-items:center;gap:7px;width:100%;border:0;background:transparent;padding:8px;border-radius:6px;text-align:left;color:var(--text-color)}.cc-sidebar-action:hover{background:var(--control-bg)}
			.cc-main{padding:18px;min-width:0}.cc-main-heading{display:flex;align-items:center;gap:8px}.cc-breadcrumbs{display:flex;align-items:center;gap:6px;min-height:28px;margin-bottom:10px;flex-wrap:wrap}.cc-main-heading .cc-breadcrumbs{flex:1}.cc-crumb{border:0;background:transparent;color:var(--text-muted);padding:2px}.cc-crumb:last-of-type{font-weight:600;color:var(--text-color)}
			.cc-dashboard{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:9px;margin-bottom:14px}.cc-stat{border:1px solid var(--border-color);border-radius:8px;background:var(--card-bg);padding:10px 12px;text-align:left}.cc-stat:hover{background:var(--subtle-fg)}.cc-stat-value{font-size:18px;font-weight:600}.cc-stat-label{font-size:11px;color:var(--text-muted);margin-top:2px}
			.cc-toolbar{display:flex;gap:8px;align-items:flex-start;margin-bottom:10px;flex-wrap:wrap}.cc-search-wrap{flex:1;min-width:220px;max-width:620px}.cc-content-search-toggle{display:flex;align-items:center;gap:6px;margin:6px 2px 0;font-size:11px;font-weight:400;color:var(--text-muted)}.cc-view-modes{display:flex;gap:3px}.cc-view-mode.active{background:var(--primary);color:var(--fg-color)}.cc-filter-panel{border:1px solid var(--border-color);border-radius:8px;background:var(--subtle-fg);padding:12px;margin-bottom:12px}.cc-filter-grid{display:grid;grid-template-columns:repeat(5,minmax(130px,1fr));gap:10px;align-items:end}.cc-filter-grid label{display:block;font-size:11px;color:var(--text-muted);margin-bottom:4px}.cc-filter-grid .cc-all-folders{display:flex;align-items:center;gap:6px;min-height:34px;color:var(--text-color);margin:0}.cc-filter-actions{display:flex;gap:6px;margin-top:10px}.cc-rating-filter{display:flex;gap:2px;margin-top:5px}.cc-rating-filter button{border:0;background:transparent;color:var(--gray-400);padding:1px;font-size:18px}.cc-rating-filter button.active{color:#f0a800}.cc-filter-tag-match{margin-top:5px}.cc-tag-facets{display:flex;flex-wrap:wrap;gap:4px;max-height:78px;overflow:auto;margin-top:6px}.cc-tag-facet{border:1px solid var(--border-color);border-radius:12px;background:var(--card-bg);font-size:10px;padding:2px 7px}.cc-active-filters{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}.cc-filter-chip{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--border-color);border-radius:13px;background:var(--control-bg);padding:3px 8px;font-size:11px}.cc-filter-chip button{border:0;background:transparent;padding:0;color:var(--text-muted)}
			.cc-summary-row{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:10px 0}.cc-sort-controls{display:flex;align-items:center;gap:4px;margin-left:auto}.cc-sort-by{height:30px;min-width:105px}.cc-select-wrap{font-size:12px;font-weight:400}.cc-bulk-bar{display:flex;align-items:center;gap:7px;padding:8px 10px;background:var(--control-bg);border-radius:7px;margin-bottom:12px}.cc-selected-count{font-weight:600;margin-right:4px}
			.cc-assets{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}.cc-assets.view-compact{grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px}.cc-assets.view-large{grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}.cc-assets.view-list{display:block;overflow-x:auto}.cc-card{position:relative;border:1px solid var(--border-color);border-radius:10px;padding:14px;background:var(--card-bg);min-width:0}.cc-card:hover{box-shadow:var(--shadow-sm)}.cc-card.selected{border-color:var(--primary);box-shadow:0 0 0 1px var(--primary)}.cc-card-select{position:absolute;right:12px;top:12px}.cc-directory-card{display:block;width:100%;color:var(--text-color);text-align:left;cursor:pointer}.cc-directory-card>.cc-folder-menu-trigger{position:absolute;right:9px;top:9px;z-index:1;background:var(--card-bg)}.cc-directory-open{display:block;width:100%;border:0;background:transparent;padding:0;color:inherit;text-align:left}.cc-directory-icon{height:160px;display:flex;align-items:center;justify-content:center;border-radius:8px;background:var(--subtle-fg);color:var(--text-muted)}.view-compact .cc-directory-icon{height:96px}.view-large .cc-directory-icon{height:240px}.cc-directory-row{cursor:pointer}.cc-directory-row:hover{background:var(--subtle-fg)}.view-standard .cc-file-icon{height:160px}.view-compact .cc-file-icon{height:96px}.view-compact .cc-filename,.view-compact .cc-tags,.view-compact .cc-statuses,.view-compact .cc-meta{display:none}.view-compact .cc-actions{margin-top:7px}.view-compact .cc-actions .btn:nth-child(n+3){display:none}.view-large .cc-file-icon{height:240px}.view-large .cc-card .cc-actions{opacity:0;transition:opacity .15s}.view-large .cc-card:hover .cc-actions,.view-large .cc-card:focus-within .cc-actions{opacity:1}.cc-list-table{width:100%;min-width:1050px;border-collapse:separate;border-spacing:0}.cc-list-table th,.cc-list-table td{padding:8px;border-bottom:1px solid var(--border-color);vertical-align:middle;text-align:left}.cc-list-table tr.selected{background:var(--subtle-fg)}.cc-list-table th{font-size:11px;color:var(--text-muted);background:var(--subtle-fg)}.cc-list-sort{border:0;background:transparent;color:inherit;padding:0}.cc-list-name{max-width:260px}.cc-list-name strong,.cc-list-name span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
			.cc-file-icon{width:100%;height:112px;border-radius:9px;display:flex;align-items:center;justify-content:center;background:var(--control-bg);font-size:20px;margin-bottom:10px;cursor:pointer;overflow:hidden}.cc-thumbnail{width:100%;height:100%;object-fit:cover;display:block}.cc-thumbnail-fallback{display:flex;align-items:center;justify-content:center;width:100%;height:100%}.cc-title{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.cc-filename{font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:3px}.cc-statuses{display:flex;flex-wrap:wrap;gap:5px;min-height:20px;margin-top:7px}.cc-status{display:inline-flex;align-items:center;gap:3px;border-radius:10px;padding:2px 7px;font-size:10px;background:var(--control-bg)}.cc-status.locked{color:var(--orange-700);background:var(--orange-100)}.cc-status.expired,.cc-status.rejected,.cc-status.grant-expired{color:var(--red-700);background:var(--red-100)}.cc-status.expiring,.cc-status.pending,.cc-status.grant-expiring{color:var(--yellow-700);background:var(--yellow-100)}.cc-status.published{color:var(--green-700);background:var(--green-100)}.cc-status.draft{color:var(--gray-700);background:var(--gray-100)}.cc-tags{display:flex;align-items:center;flex-wrap:wrap;gap:4px;min-height:25px;margin-top:8px}.cc-tag{display:inline-flex;align-items:center;gap:3px;background:var(--control-bg);border-radius:12px;padding:2px 8px;font-size:11px}.cc-inline-tag-remove,.cc-inline-tag-add{border:0;background:transparent;color:var(--text-muted);padding:0;line-height:1}.cc-inline-tag-remove:hover,.cc-inline-tag-add:hover{color:var(--text-color)}.cc-inline-tag-input{width:88px;height:24px;border:1px solid var(--border-color);border-radius:12px;background:var(--control-bg);padding:2px 8px;font-size:11px;outline:0}.cc-inline-tag-input:focus{border-color:var(--primary)}.cc-inline-tags.saving{pointer-events:none;opacity:.55}.cc-meta{display:flex;justify-content:space-between;font-size:11px;margin-top:10px}.cc-rating{display:inline-flex;gap:1px;margin-top:7px}.cc-rating-star{border:0;background:transparent;color:var(--gray-400);padding:0 1px;font-size:17px;line-height:1;cursor:pointer}.cc-rating-star.active{color:#f0a800}.cc-rating-star:disabled{cursor:default;opacity:.75}.cc-rating.saving{pointer-events:none;opacity:.55}.cc-token-input{display:flex;align-items:center;flex-wrap:wrap;gap:5px;min-height:38px;padding:5px 7px;border:1px solid var(--border-color);border-radius:var(--border-radius);background:var(--control-bg)}.cc-token-input:focus-within{border-color:var(--primary)}.cc-token-input.read-only{background:var(--disabled-control-bg);min-height:34px}.cc-token{display:inline-flex;align-items:center;gap:4px;max-width:100%;border-radius:12px;padding:3px 7px;background:var(--subtle-fg);font-size:11px}.cc-token span{overflow:hidden;text-overflow:ellipsis}.cc-token button{border:0;background:transparent;padding:0;color:var(--text-muted);line-height:1}.cc-token-input input{flex:1;min-width:120px;border:0!important;background:transparent!important;outline:0!important;box-shadow:none!important}.cc-detail-dialog [data-fieldname="notes"] textarea{height:38px!important;min-height:38px!important;resize:vertical}.cc-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}.cc-actions .btn{padding:3px 8px;font-size:11px}.cc-empty{grid-column:1/-1;text-align:center;padding:90px 20px;color:var(--text-muted)}
			.cc-preview{min-height:340px;display:flex;align-items:center;justify-content:center;background:var(--subtle-fg);border-radius:8px;overflow:hidden}.cc-preview img{max-width:100%;max-height:68vh}.cc-preview iframe,.cc-preview video{width:100%;height:68vh;border:0}.cc-preview audio{width:min(600px,90%)}.cc-preview-fallback{text-align:center;padding:60px 20px}.cc-activity{list-style:none;padding:0}.cc-activity li{padding:7px 0;border-bottom:1px solid var(--border-color)}
			.cc-search-result{grid-column:1/-1;border:1px solid var(--border-color);border-radius:9px;padding:13px;background:var(--card-bg)}.cc-search-result:hover{box-shadow:var(--shadow-sm)}.cc-search-result-heading{display:flex;align-items:center;justify-content:space-between;gap:10px}.cc-search-snippet{margin:8px 0;line-height:1.6;color:var(--text-color)}.cc-search-snippet mark{background:var(--yellow-100);color:inherit;border-radius:2px;padding:0 2px}.cc-index-status{font-size:10px;border-radius:10px;padding:2px 7px;background:var(--control-bg)}
			.cc-knowledge-metrics{display:grid;grid-template-columns:repeat(3,minmax(150px,1fr));gap:10px}.cc-knowledge-metric{border:1px solid var(--border-color);border-radius:9px;padding:11px;background:var(--card-bg);text-align:left}.cc-knowledge-metric[data-issue-type]{cursor:pointer}.cc-knowledge-value{font-size:20px;font-weight:600}.cc-progress{height:7px;border-radius:6px;background:var(--control-bg);overflow:hidden;margin-top:7px}.cc-progress>span{display:block;height:100%;background:var(--primary);border-radius:inherit}.cc-quality-list{margin-top:14px}.cc-quality-pagination{display:flex;align-items:center;justify-content:flex-end;gap:8px;margin-top:10px}.cc-popular-query{display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border-color)}
			.cc-favorite{position:absolute;right:38px;top:8px;z-index:1;border:0;background:var(--card-bg);color:var(--text-muted);padding:4px;border-radius:6px}.cc-favorite.active{color:var(--primary)}.cc-relation-row{display:flex;justify-content:space-between;align-items:center;gap:8px}
			.cc-sidebar-overlay{display:none}
			@media(hover:none){.cc-folder-menu-trigger{opacity:1}}
			@media(max-width:1050px){.cc-filter-grid{grid-template-columns:repeat(2,minmax(150px,1fr))}.cc-dashboard{grid-template-columns:repeat(3,1fr)}.cc-knowledge-metrics{grid-template-columns:repeat(2,minmax(140px,1fr))}}@media(max-width:900px){.cc-shell{display:block;min-height:calc(100vh - 95px);overflow:hidden}.cc-sidebar{position:absolute;inset:0 auto 0 0;z-index:3;width:min(310px,86vw);max-height:none;border-right:1px solid var(--border-color);transform:translateX(-102%);transition:transform .2s ease;box-shadow:var(--shadow-lg)}.cc-shell.cc-sidebar-open .cc-sidebar{transform:translateX(0)}.cc-shell.cc-sidebar-open .cc-sidebar-overlay{display:block;position:absolute;inset:0;z-index:2;background:rgba(0,0,0,.28)}.cc-mobile-sidebar-close,.cc-mobile-sidebar-toggle{display:inline-flex}.cc-erp-nav-toggle{display:none}.cc-mobile-company{display:block}.cc-main-heading{align-items:flex-start;flex-wrap:wrap}.cc-main-heading .cc-breadcrumbs{flex-basis:100%;order:3}.cc-main{padding:12px}.cc-toolbar{align-items:stretch}.cc-filter-grid{grid-template-columns:1fr}.cc-summary-row{align-items:flex-start;gap:8px;flex-wrap:wrap}.cc-dashboard{grid-template-columns:repeat(2,1fr)}}@media(max-width:600px){.cc-mobile-company{flex:1;min-width:150px}.cc-knowledge-metrics{grid-template-columns:1fr}.cc-view-modes{width:100%;overflow-x:auto}.cc-assets.view-standard,.cc-assets.view-large{grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px}.view-standard .cc-file-icon,.view-large .cc-file-icon,.view-standard .cc-directory-icon,.view-large .cc-directory-icon{height:96px}.view-large .cc-card .cc-actions{opacity:1}.cc-sort-controls{order:3;width:100%;margin-left:0}}
		</style>`);
	}

	bind_events() {
		let timer;
		this.wrapper.on("input", ".cc-search", (event) => {
			clearTimeout(timer);
			timer = setTimeout(() => {
				this.search = event.target.value || "";
				this.selected.clear();
				this.load();
			}, 300);
		});
		this.wrapper.on("click", ".cc-folder", (event) => {
			const folder = $(event.currentTarget).data("folder");
			const hasChildren = (this.data?.folders || []).some((row) => row.parent_channel_content_folder === folder);
			if (hasChildren) {
				if (this.collapsedFolders.has(folder)) this.collapsedFolders.delete(folder);
				else this.collapsedFolders.add(folder);
				this.save_folder_collapse_state();
			}
			this.current_folder = folder;
			this.view = "folder";
			this.selected.clear();
			this.close_mobile_sidebar();
			this.load();
		});
		this.wrapper.on("click", ".cc-folder-menu-trigger", (event) => {
			event.preventDefault();
			event.stopPropagation();
			const button = $(event.currentTarget);
			const rect = event.currentTarget.getBoundingClientRect();
			this.show_folder_menu(button.data("folder-menu"), rect.left, rect.bottom + 4);
		});
		this.wrapper.on("contextmenu", "[data-folder-menu-scope]", (event) => {
			event.preventDefault();
			event.stopPropagation();
			const folder = $(event.currentTarget).data("folder-menu-scope");
			this.show_folder_menu(folder, event.clientX, event.clientY);
		});
		this.wrapper.on("click", ".cc-folder-context-menu [data-folder-action]", (event) => {
			event.preventDefault();
			event.stopPropagation();
			const button = $(event.currentTarget);
			const folder = button.closest(".cc-folder-context-menu").data("folder");
			this.close_folder_menu();
			this.run_folder_action(button.data("folder-action"), folder);
		});
		this.wrapper.on("click", ".cc-crumb,[data-content-folder]", (event) => {
			this.current_folder = $(event.currentTarget).data("folder") || $(event.currentTarget).data("content-folder");
			this.view = "folder";
			this.selected.clear();
			this.close_mobile_sidebar();
			this.load();
		});
		this.wrapper.on("click", ".cc-folder-toggle", (event) => {
			event.preventDefault();
			event.stopPropagation();
			const folder = $(event.currentTarget).data("folder-toggle");
			if (this.collapsedFolders.has(folder)) this.collapsedFolders.delete(folder);
			else this.collapsedFolders.add(folder);
			this.save_folder_collapse_state();
			this.render_folders();
		});
		this.wrapper.on("click", "[data-folder-tree-action]", (event) => {
			const action = $(event.currentTarget).data("folder-tree-action");
			const parentNames = new Set((this.data?.folders || []).map((folder) => folder.parent_channel_content_folder).filter(Boolean));
			this.collapsedFolders = action === "collapse" ? parentNames : new Set();
			this.save_folder_collapse_state();
			this.render_folders();
		});
		this.wrapper.on("click", ".cc-view,.cc-stat[data-view]", (event) => {
			this.view = $(event.currentTarget).data("view");
			this.show_trashed = false;
			this.selected.clear();
			this.close_mobile_sidebar();
			this.load();
		});
		this.wrapper.on("click", ".cc-erp-nav-toggle", () => this.toggle_erp_sidebar());
		this.wrapper.on("click", ".cc-mobile-sidebar-toggle", () => this.open_mobile_sidebar());
		this.wrapper.on("click", ".cc-mobile-sidebar-close,.cc-sidebar-overlay", () => this.close_mobile_sidebar());
		this.wrapper.on("click", "[data-sidebar-action]", (event) => {
			const action = $(event.currentTarget).data("sidebar-action");
			this.close_mobile_sidebar();
			if (action === "trash") this.toggle_trash();
			if (action === "settings") frappe.set_route("Form", "Channel Content Settings");
			if (action === "knowledge") this.show_knowledge_dashboard();
		});
		this.wrapper.on("click", ".cc-toggle-filters", () => this.wrapper.find(".cc-filter-panel").toggleClass("hide"));
		this.wrapper.on("click", ".cc-view-mode", (event) => {
			this.displayMode = $(event.currentTarget).data("display-mode");
			this.wrapper.find(".cc-view-mode").removeClass("active");
			$(event.currentTarget).addClass("active");
			this.save_preferences();
			this.render_assets();
		});
		this.wrapper.on("change", ".cc-sort-by", (event) => {
			this.sortBy = event.currentTarget.value;
			this.save_preferences();
			this.load();
		});
		this.wrapper.on("click", ".cc-sort-order", (event) => {
			this.sortOrder = this.sortOrder === "asc" ? "desc" : "asc";
			$(event.currentTarget).attr("data-sort-order", this.sortOrder).text(this.sortOrder === "asc" ? "↑" : "↓");
			this.save_preferences();
			this.load();
		});
		this.wrapper.on("click", ".cc-list-sort", (event) => {
			const sortBy = $(event.currentTarget).data("sort-by");
			this.sortOrder = this.sortBy === sortBy && this.sortOrder === "asc" ? "desc" : "asc";
			this.sortBy = sortBy;
			this.wrapper.find(".cc-sort-by").val(sortBy);
			this.wrapper.find(".cc-sort-order").attr("data-sort-order", this.sortOrder).text(this.sortOrder === "asc" ? "↑" : "↓");
			this.save_preferences();
			this.load();
		});
		this.wrapper.on("click", "[data-filter-rating]", (event) => {
			const value = Number($(event.currentTarget).data("filter-rating"));
			this.wrapper.find(".cc-rating-filter").attr("data-rating", value).find("button").each((_, button) => $(button).toggleClass("active", Number($(button).data("filter-rating")) <= value));
			if (this.wrapper.find(".cc-filter-rating-mode").val() === "all") this.wrapper.find(".cc-filter-rating-mode").val("exact");
		});
		this.wrapper.on("change", ".cc-filter-rating-mode", (event) => {
			if (["all", "unrated"].includes(event.currentTarget.value)) this.wrapper.find(".cc-rating-filter").attr("data-rating", "0").find("button").removeClass("active");
		});
		this.wrapper.on("click", ".cc-tag-facet", (event) => {
			this.tagFilter.add($(event.currentTarget).data("tag"));
			this.tagFilter.render();
			this.tagFilter.changed();
		});
		this.wrapper.on("click", ".cc-filter-chip button", (event) => this.remove_filter_chip($(event.currentTarget).closest(".cc-filter-chip")));
		this.wrapper.on("click", ".cc-clear-filter-chips", () => this.clear_filters());
		this.wrapper.on("change", ".cc-search-content", () => {
			this.selected.clear();
			this.load();
		});
		this.wrapper.on("click", ".cc-apply-filters", () => this.apply_filters());
		this.wrapper.on("click", ".cc-clear-filters", () => this.clear_filters());
		this.wrapper.on("change", ".cc-asset-select", (event) => this.toggle_asset_selection(event));
		this.wrapper.on("change", ".cc-select-all", (event) => this.select_all(event.target.checked));
		this.wrapper.on("click", ".cc-clear-selection", () => {
			this.selected.clear();
			this.render_selection();
		});
		this.wrapper.on("click", "[data-cc-bulk]", (event) => this.bulk_action($(event.currentTarget).data("cc-bulk")));
		this.wrapper.on("click", "[data-cc-governance]", (event) => this.bulk_governance($(event.currentTarget).data("cc-governance")));
		this.wrapper.on("click", "[data-cc-rating]", (event) => {
			event.preventDefault();
			event.stopPropagation();
			const button = $(event.currentTarget);
			this.rate_asset(button.data("asset"), Number(button.data("cc-rating")), button.closest(".cc-rating"));
		});
		this.wrapper.on("click", ".cc-inline-tag-remove", (event) => {
			event.preventDefault();
			event.stopPropagation();
			const button = $(event.currentTarget);
			this.remove_inline_tag(button.data("asset"), Number(button.data("tag-index")), button.closest(".cc-inline-tags"));
		});
		this.wrapper.on("click", ".cc-inline-tag-add", (event) => {
			event.preventDefault();
			event.stopPropagation();
			const control = $(event.currentTarget).closest(".cc-inline-tags");
			control.find(".cc-inline-tag-add").addClass("hide");
			control.find(".cc-inline-tag-input").removeClass("hide").trigger("focus");
		});
		this.wrapper.on("keydown", ".cc-inline-tag-input", (event) => {
			if (event.isComposing) return;
			if (["Enter", ",", "，"].includes(event.key)) {
				event.preventDefault();
				this.commit_inline_tag($(event.currentTarget));
			} else if (event.key === "Escape") {
				$(event.currentTarget).val("").addClass("hide").siblings(".cc-inline-tag-add").removeClass("hide");
			}
		});
		this.wrapper.on("blur", ".cc-inline-tag-input", (event) => this.commit_inline_tag($(event.currentTarget)));
		this.wrapper.on("click", "[data-cc-action]", (event) => {
			const button = $(event.currentTarget);
			this.handle_action(button.data("cc-action"), button.data("asset"), button.data("url"));
		});
	}

	async load() {
		const sequence = ++this.loadSequence;
		this.wrapper.find(".cc-assets").html(`<div class="cc-empty">${__("正在加载…")}</div>`);
		try {
			if (this.wrapper.find(".cc-search-content").prop("checked") && this.search.trim() && this.data) {
				try {
					await this.load_content_search(sequence);
					return;
				} catch (error) {
					this.wrapper.find(".cc-search-content").prop("checked", false);
					if (!this.search_fallback_notified) {
						this.search_fallback_notified = true;
						frappe.show_alert({message: __("全文搜索暂不可用，已自动切换为标题和标签搜索"), indicator: "orange"});
					}
				}
			}
			const workspaceArgs = {
					folder: this.current_folder,
					search: this.search,
					show_trashed: this.show_trashed ? 1 : 0,
					view: this.view,
					sort_by: this.sortBy,
					sort_order: this.sortOrder,
					...this.filters,
				};
			const legacyArgs = {...workspaceArgs};
			["tags", "tag_match", "rating_mode", "rating", "sort_by", "sort_order"].forEach((key) => delete legacyArgs[key]);
			let response;
			if (this.workspaceContractFallback) {
				response = await frappe.call({method: "channel_erp.content_center.get_workspace", args: legacyArgs});
			} else {
				try {
					response = await frappe.call({method: "channel_erp.content_center.get_workspace", args: workspaceArgs});
				} catch (contractError) {
					this.workspaceContractFallback = true;
					delete this.filters.tags; delete this.filters.tag_match; delete this.filters.rating_mode; delete this.filters.rating; delete this.filters.min_rating;
					delete legacyArgs.min_rating;
					this.tagFilter.tags = [];
					this.tagFilter.render();
					this.wrapper.find(".cc-filter-tags-wrap,.cc-filter-rating-wrap,.cc-sort-controls").addClass("hide");
					response = await frappe.call({method: "channel_erp.content_center.get_workspace", args: legacyArgs});
					frappe.show_alert({message: __("高级筛选或排序暂不可用，已回退基础文件视图"), indicator: "orange"});
				}
			}
			if (sequence !== this.loadSequence) return;
			this.data = response.message;
			this.search_results = [];
			this.view = this.data.view || this.view;
			this.current_folder = this.data.current_folder;
			this.selected = new Set([...this.selected].filter((name) => this.data.assets.some((asset) => asset.name === name)));
			this.render();
		} catch (error) {
			if (sequence !== this.loadSequence) return;
			this.wrapper.find(".cc-assets").html(`<div class="cc-empty">${__("内容中心加载失败")}</div>`);
		}
	}

	async load_content_search(sequence = this.loadSequence) {
		const response = await frappe.call({
			method: "channel_erp.content_center.search_content",
			args: {
				query: this.search.trim(),
				folder: this.view === "folder" ? this.current_folder : null,
				view: this.view,
				file_type: this.filters.file_type || null,
				department: this.filters.department || null,
				responsible_user: this.filters.responsible_user || null,
				modified_from: this.filters.modified_from || null,
				modified_to: this.filters.modified_to || null,
			},
		});
		if (sequence !== this.loadSequence) return;
		const message = response.message;
		if (message?.available === false || message?.enabled === false) throw new Error("content search unavailable");
		const results = Array.isArray(message) ? message : (message?.results || message?.items);
		if (!Array.isArray(results)) throw new Error("invalid content search response");
		this.search_results = results;
		this.search_total = message?.total ?? results.length;
		this.selected.clear();
		this.render_content_search();
	}

	render_content_search() {
		this.wrapper.find(".cc-view").removeClass("active").filter(`[data-view='${this.view}']`).addClass("active");
		this.wrapper.find(".cc-folder").removeClass("active").filter(`[data-folder='${this.current_folder}']`).addClass("active");
		if (this.view !== "folder") {
			const label = this.wrapper.find(`.cc-view[data-view='${this.view}'] span`).text() || __("全文搜索");
			this.wrapper.find(".cc-breadcrumbs").html(`<strong>${frappe.utils.escape_html(label)}</strong>`);
		}
		this.wrapper.find(".cc-summary").text(__("全文搜索找到 {0} 个结果", [this.search_total ?? this.search_results.length]));
		this.wrapper.find(".cc-select-wrap,.cc-bulk-bar").addClass("hide");
		this.wrapper.find(".cc-assets").html(this.search_results.length
			? this.search_results.map((result) => this.search_result_card(result)).join("")
			: `<div class="cc-empty">${__("没有找到匹配的文件内容")}</div>`);
	}

	search_result_card(result) {
		const escape = frappe.utils.escape_html;
		const title = result.title || result.file_name || result.name || __("未命名文件");
		const snippetData = result.snippet || result.content_snippet || result.match_excerpt || "";
		const snippet = typeof snippetData === "object" ? (snippetData.text || "") : snippetData;
		let highlighted = escape(snippet);
		if (typeof snippetData === "object" && Number.isInteger(snippetData.highlight_start) && Number.isInteger(snippetData.highlight_end)) {
			const start = Math.max(0, snippetData.highlight_start);
			const end = Math.max(start, snippetData.highlight_end);
			highlighted = `${escape(snippet.slice(0, start))}<mark>${escape(snippet.slice(start, end))}</mark>${escape(snippet.slice(end))}`;
		} else {
			const terms = this.search.trim().split(/\s+/).filter((term) => term.length > 1).slice(0, 8);
			terms.forEach((term) => {
				const safeTerm = escape(term);
				const pattern = safeTerm.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
				highlighted = highlighted.replace(new RegExp(`(${pattern})`, "gi"), "<mark>$1</mark>");
			});
		}
		const indexStatus = result.index_status || result.content_index_status || (result.indexed_at ? "Ready" : __("未知"));
		const indexLabel = {Ready: __("已索引"), Queued: __("等待索引"), Pending: __("等待索引"), Failed: __("索引失败"), Unsupported: __("不支持")}[indexStatus] || indexStatus;
		const folderRecord = this.data?.folders?.find((folder) => folder.name === result.folder);
		const folder = result.folder_path || result.folder_name || result.folder_label || folderRecord?.folder_name || result.folder;
		const assetName = result.asset || result.asset_name || result.name;
		return `<article class="cc-search-result">
			<div class="cc-search-result-heading"><strong>${escape(title)}</strong><span class="cc-index-status">${escape(indexLabel)}</span></div>
			<div class="text-muted small">${escape(result.file_type || result.mime_type || __("未知类型"))}${folder ? ` · ${__("目录")}：${escape(folder)}` : ""}${result.modified || result.indexed_at ? ` · ${frappe.datetime.str_to_user(result.modified || result.indexed_at)}` : ""}</div>
			<div class="cc-search-snippet">${highlighted || `<span class="text-muted">${__("命中文本不可用")}</span>`}</div>
			<div class="cc-actions"><button class="btn btn-primary btn-xs" data-cc-action="detail" data-asset="${escape(assetName)}">${__("查看详情")}</button> <button class="btn btn-default btn-xs" data-cc-action="preview" data-asset="${escape(assetName)}">${__("预览")}</button></div>
		</article>`;
	}

	render_folders() {
		if (!this.data?.folders) return;
		const escape = frappe.utils.escape_html;
		this.load_folder_collapse_state();
		const activePathKey = this.current_folder || "";
		if (this.expandedPathKey !== activePathKey) {
			(this.data.breadcrumbs || []).slice(0, -1).forEach((folder) => this.collapsedFolders.delete(folder.name));
			this.expandedPathKey = activePathKey;
		}
		const byName = Object.fromEntries(this.data.folders.map((folder) => [folder.name, folder]));
		const parentNames = new Set(this.data.folders.map((folder) => folder.parent_channel_content_folder).filter(Boolean));
		const depths = {};
		const depthOf = (folder) => {
			if (depths[folder.name] !== undefined) return depths[folder.name];
			const parent = byName[folder.parent_channel_content_folder];
			depths[folder.name] = parent ? depthOf(parent) + 1 : 0;
			return depths[folder.name];
		};
		const hiddenByAncestor = (folder) => {
			let parent = byName[folder.parent_channel_content_folder];
			while (parent) {
				if (this.collapsedFolders.has(parent.name)) return true;
				parent = byName[parent.parent_channel_content_folder];
			}
			return false;
		};
		this.wrapper.find(".cc-folders").html(this.data.folders.filter((folder) => !hiddenByAncestor(folder)).map((folder) => {
			const hasChildren = parentNames.has(folder.name);
			const collapsed = this.collapsedFolders.has(folder.name);
			return `<div class="cc-folder-row" data-folder-menu-scope="${escape(folder.name)}" style="padding-left:${depthOf(folder) * 18}px">
				<button type="button" class="cc-folder-toggle ${hasChildren ? "" : "placeholder"}" data-folder-toggle="${escape(folder.name)}" aria-expanded="${hasChildren ? String(!collapsed) : "false"}" title="${collapsed ? __("展开") : __("折叠")}">${collapsed ? "›" : "⌄"}</button>
				<button class="cc-folder ${this.view === "folder" && folder.name === this.current_folder ? "active" : ""}" data-folder="${escape(folder.name)}">
					${frappe.utils.icon("folder-normal", "sm")}<span title="${escape(folder.folder_name)}">${escape(folder.folder_name)}</span>
				</button>
				<button type="button" class="cc-folder-menu-trigger" data-folder-menu="${escape(folder.name)}" aria-label="${__("文件夹操作")}">⋯</button>
			</div>`;
		}).join(""));
		this.save_folder_collapse_state();
	}

	folder_action_permissions(folder) {
		const explicit = folder?.permissions || folder?.actions || {};
		const actions = Array.isArray(explicit) ? Object.fromEntries(explicit.map((action) => [action, true])) : explicit;
		const fallback = folder?.name === this.current_folder ? (this.data?.permissions || {}) : {};
		const enabled = (value) => value === true || Number(value) === 1;
		const allowed = (...keys) => {
			for (const key of keys) if (Object.prototype.hasOwnProperty.call(actions, key)) return enabled(actions[key]);
			return keys.some((key) => enabled(fallback[key]));
		};
		const protectedFolder = Boolean(
			folder?.is_protected === true || Number(folder?.is_protected) === 1 ||
			folder?.protected === true || Number(folder?.protected) === 1 ||
			folder?.name === this.data?.root_folder
		);
		return {
			open: true,
			create: allowed("create", "create_child", "can_create_folder"),
			rename: !protectedFolder && allowed("rename", "can_rename", "can_move"),
			move: !protectedFolder && allowed("move", "can_move"),
			permissions: allowed("permissions", "manage_permissions", "can_manage"),
			delete: !protectedFolder && allowed("delete", "can_delete"),
		};
	}

	show_folder_menu(folderName, left, top) {
		this.close_folder_menu();
		const folder = (this.data?.folders || []).find((row) => row.name === folderName);
		if (!folder) return;
		const permissions = this.folder_action_permissions(folder);
		const items = [
			["open", __("打开"), true, ""],
			["create", __("新建子文件夹"), permissions.create, ""],
			["rename", __("重命名"), permissions.rename, ""],
			["move", __("移动到"), permissions.move, ""],
			["permissions", __("权限设置"), permissions.permissions, ""],
			["delete", __("删除"), permissions.delete, "danger"],
		].filter((item) => item[2]);
		const escape = frappe.utils.escape_html;
		const menu = $(`<div class="cc-folder-context-menu" data-folder="${escape(folderName)}" role="menu">${items.map(([action, label, , className]) => `<button type="button" class="${className}" data-folder-action="${action}" role="menuitem">${escape(label)}</button>`).join("")}</div>`).appendTo(this.wrapper);
		const width = menu.outerWidth() || 180;
		const height = menu.outerHeight() || 220;
		menu.css({left: Math.max(6, Math.min(left, window.innerWidth - width - 8)), top: Math.max(6, Math.min(top, window.innerHeight - height - 8))});
		setTimeout(() => {
			$(document).off("pointerdown.channel-content-folder-menu keydown.channel-content-folder-menu")
				.on("pointerdown.channel-content-folder-menu", (event) => {
					if (!$(event.target).closest(".cc-folder-context-menu,.cc-folder-menu-trigger").length) this.close_folder_menu();
				})
				.on("keydown.channel-content-folder-menu", (event) => { if (event.key === "Escape") this.close_folder_menu(); });
		}, 0);
	}

	close_folder_menu() {
		this.wrapper.find(".cc-folder-context-menu").remove();
		$(document).off("pointerdown.channel-content-folder-menu keydown.channel-content-folder-menu");
	}

	run_folder_action(action, folder) {
		if (action === "open") return this.open_folder(folder);
		if (action === "create") return this.create_folder(folder);
		if (action === "rename") return this.rename_folder(folder);
		if (action === "move") return this.move_folder(folder);
		if (action === "permissions") return this.open_permissions(folder);
		if (action === "delete") return this.delete_folder(folder);
	}

	open_folder(folder) {
		if (!folder) return;
		this.current_folder = folder;
		this.view = "folder";
		this.selected.clear();
		this.close_mobile_sidebar();
		this.load();
	}

	render() {
		const escape = frappe.utils.escape_html;
		this.close_folder_menu();
		this.wrapper.find(".cc-dashboard,.cc-toolbar,.cc-active-filters,.cc-summary-row").removeClass("hide");
		this.featureFlags = {...this.featureFlags, ...(this.data.feature_flags || {})};
		this.render_folders();
		this.wrapper.find("[data-optional-feature]").each((_, element) => {
			$(element).toggleClass("hide", !this.featureFlags[$(element).data("optional-feature")]);
		});
		this.wrapper.find(".cc-filter-publication-wrap").toggleClass("hide", !this.featureFlags.approval_workflow);
		this.wrapper.find(".cc-view").removeClass("active").filter(`[data-view='${this.view}']`).addClass("active");

		const viewLabels = {
			all: __("全部文件"),
			favorites: __("我的收藏"),
			recent: __("最近使用"),
			mine: __("我上传的"),
			expiring: __("即将到期"),
			expired: __("已过期"),
			unassigned: __("无人负责"),
			pending_review: __("待我审批"),
			granted_to_me: __("授权给我"),
			borrow_review: __("借阅审批"),
		};
		this.wrapper.find(".cc-breadcrumbs").html(this.view === "folder" ? this.data.breadcrumbs.map((folder, index) => `
			${index ? `<span class="text-muted">/</span>` : ""}<button class="cc-crumb" data-folder="${escape(folder.name)}">${escape(folder.folder_name)}</button>
		`).join("") : `<strong>${viewLabels[this.view] || __("内容中心")}</strong>`);
		this.render_dashboard();
		const size = frappe.utils.format_size ? frappe.utils.format_size(this.data.summary.total_size || 0) : `${this.data.summary.total_size || 0} B`;
		const folderCount = this.current_child_folders().length;
		this.wrapper.find(".cc-summary").text(folderCount
			? __("{0} 个文件夹 · {1} 个文件 · {2}", [folderCount, this.data.summary.file_count, size])
			: __("{0} 个文件 · {1}", [this.data.summary.file_count, size]));
		this.render_type_options();
		this.wrapper.find(".cc-select-wrap").removeClass("hide");
		const canBulkRebuildIndex = (frappe.session.user === "Administrator" || (frappe.user_roles || []).some((role) => ["System Manager", "Content Center Manager"].includes(role))) &&
			Boolean(this.data.permissions?.can_rebuild_index || this.data.assets?.some((asset) => Object.prototype.hasOwnProperty.call(asset.permissions || {}, "can_rebuild_index")) || this.data.indexing_enabled);
		this.rebuild_index_button?.toggle(canBulkRebuildIndex);

		this.page.set_primary_action(__("上传文件"), () => this.upload_files(), "upload");
		if (!this.data.permissions.can_upload || this.show_trashed) this.page.clear_primary_action();
		this.rename_folder_button?.toggle(
			this.view === "folder" &&
			!this.show_trashed &&
			this.current_folder !== this.data.root_folder &&
			Boolean(this.data.permissions.can_move)
		);
		this.wrapper.find("[data-cc-bulk='move']").toggle(!this.show_trashed);
		this.wrapper.find("[data-cc-bulk='copy']").toggle(!this.show_trashed);
		this.wrapper.find("[data-cc-bulk='download']").toggle(!this.show_trashed);
		this.wrapper.find("[data-cc-bulk='trash']").toggle(!this.show_trashed);
		this.wrapper.find("[data-cc-bulk='restore']").toggle(this.show_trashed);
		this.wrapper.find("[data-cc-bulk='permanent-delete']").toggle(this.show_trashed);
		this.wrapper.find("[data-cc-governance]").toggle(!this.show_trashed);
		this.wrapper.find("[data-sidebar-action='trash'] span").text(this.show_trashed ? __("返回文件") : __("回收站"));
		this.wrapper.find("[data-sidebar-action='knowledge']").toggleClass("hide", !Boolean(
			this.data.permissions?.can_view_knowledge_dashboard ||
			this.data.permissions?.can_view_quality_metrics ||
			this.data.permissions?.can_manage_knowledge
		));

		this.render_assets();
		this.render_filter_chips();
		this.render_tag_facets();
		this.render_selection();
	}

	render_assets() {
		const container = this.wrapper.find(".cc-assets");
		container.removeClass("view-list view-compact view-standard view-large").addClass(`view-${this.displayMode}`);
		const folders = this.current_child_folders();
		const assets = this.data?.assets || [];
		if (!assets.length && !folders.length) {
			container.html(`<div class="cc-empty">${this.show_trashed ? __("回收站为空") : __("没有符合条件的文件")}</div>`);
			return;
		}
		container.html(this.displayMode === "list"
			? this.asset_list(assets, folders)
			: `${folders.map((folder) => this.folder_card(folder)).join("")}${assets.map((asset) => this.asset_card(asset)).join("")}`);
		container.find(".cc-thumbnail").each(function () {
			const image = $(this);
			image.one("error", () => image.addClass("hide").siblings(".cc-thumbnail-fallback").removeClass("hide"));
			image.attr("src", image.data("thumbnail-url"));
		});
	}

	current_child_folders() {
		if (this.view !== "folder" || this.show_trashed || !this.current_folder) return [];
		return (this.data?.folders || [])
			.filter((folder) => folder.parent_channel_content_folder === this.current_folder)
			.sort((left, right) => String(left.folder_name || "").localeCompare(String(right.folder_name || "")));
	}

	folder_card(folder) {
		const escape = frappe.utils.escape_html;
		return `<article class="cc-card cc-directory-card" data-folder-menu-scope="${escape(folder.name)}">
			<button type="button" class="cc-folder-menu-trigger" data-folder-menu="${escape(folder.name)}" aria-label="${__("文件夹操作")}">⋯</button>
			<button type="button" class="cc-directory-open" data-content-folder="${escape(folder.name)}">
				<div class="cc-directory-icon">${frappe.utils.icon("folder-normal", "md")}</div>
				<div class="cc-title" title="${escape(folder.folder_name)}">${escape(folder.folder_name)}</div>
				<div class="cc-filename text-muted">${__("文件夹")}</div>
			</button>
		</article>`;
	}

	asset_list(assets, folders = []) {
		const header = (label, field) => `<button class="cc-list-sort" data-sort-by="${field}">${label}${this.sortBy === field ? (this.sortOrder === "asc" ? " ↑" : " ↓") : ""}</button>`;
		const folderRows = folders.map((folder) => `<tr class="cc-directory-row" data-folder-menu-scope="${frappe.utils.escape_html(folder.name)}"><td>${frappe.utils.icon("folder-normal", "sm")}</td><td class="cc-list-name" data-content-folder="${frappe.utils.escape_html(folder.name)}"><strong>${frappe.utils.escape_html(folder.folder_name)}</strong><span class="text-muted small">${__("文件夹")}</span></td><td></td><td></td><td>${__("文件夹")}</td><td>-</td><td>-</td><td>-</td><td>${__("正常")}</td><td><button type="button" class="btn btn-default btn-xs" data-content-folder="${frappe.utils.escape_html(folder.name)}">${__("打开")}</button> <button type="button" class="cc-folder-menu-trigger" data-folder-menu="${frappe.utils.escape_html(folder.name)}" aria-label="${__("文件夹操作")}">⋯</button></td></tr>`).join("");
		return `<table class="cc-list-table"><thead><tr><th></th><th>${header(__("名称"), "title")}</th><th>${__("标签")}</th><th>${header(__("星级"), "rating")}</th><th>${__("类型")}</th><th>${header(__("大小"), "file_size")}</th><th>${__("负责人")}</th><th>${header(__("修改时间"), "modified")}</th><th>${__("状态")}</th><th>${__("操作")}</th></tr></thead><tbody>${folderRows}${assets.map((asset) => this.asset_list_row(asset)).join("")}</tbody></table>`;
	}

	asset_tags(asset) {
		return String(asset?.tags || "").split(/[,，\n\r]+/).map((tag) => tag.trim()).filter(Boolean);
	}

	can_edit_inline_tags(asset) {
		const pending = this.featureFlags.approval_workflow && (asset.publication_status || "Draft") === "Pending";
		return Boolean(asset.permissions?.can_upload) && !asset.is_locked && !pending && !this.show_trashed;
	}

	inline_tags(asset) {
		const escape = frappe.utils.escape_html;
		const tags = this.asset_tags(asset);
		const canEdit = this.can_edit_inline_tags(asset);
		return `<div class="cc-tags cc-inline-tags" data-tag-asset="${escape(asset.name)}">
			${tags.map((tag, index) => `<span class="cc-tag"><span>${escape(tag)}</span>${canEdit ? `<button type="button" class="cc-inline-tag-remove" data-asset="${escape(asset.name)}" data-tag-index="${index}" aria-label="${__("删除标签 {0}", [tag])}">×</button>` : ""}</span>`).join("")}
			${canEdit && tags.length < 20 ? `<button type="button" class="cc-inline-tag-add" data-asset="${escape(asset.name)}">＋${__("标签")}</button><input type="text" class="cc-inline-tag-input hide" data-asset="${escape(asset.name)}" maxlength="30" placeholder="${__("输入后回车")}">` : ""}
		</div>`;
	}

	asset_list_row(asset) {
		const escape = frappe.utils.escape_html;
		const rating = Math.max(0, Math.min(5, Number(asset.rating || 0)));
		const publicationStatus = asset.publication_status || "Draft";
		const publicationLabels = {Draft: __("草稿"), Pending: __("待审批"), Published: __("已发布"), Rejected: __("已驳回")};
		const ratePermission = Object.prototype.hasOwnProperty.call(asset.permissions || {}, "can_rate") ? asset.permissions.can_rate : asset.permissions?.can_upload;
		const canRate = Boolean(ratePermission) && !asset.is_locked && (!this.featureFlags.approval_workflow || publicationStatus !== "Pending") && !this.show_trashed;
		const stars = `<div class="cc-rating" data-rating-asset="${escape(asset.name)}" data-current-rating="${rating}">${[1,2,3,4,5].map((value) => `<button type="button" class="cc-rating-star ${value <= rating ? "active" : ""}" data-cc-rating="${value}" data-asset="${escape(asset.name)}" ${canRate ? "" : "disabled"}>★</button>`).join("")}</div>`;
		const size = frappe.utils.format_size ? frappe.utils.format_size(asset.file_size || 0) : `${asset.file_size || 0} B`;
		const workflowStatus = this.featureFlags.approval_workflow ? `<span class="cc-status ${escape(publicationStatus.toLowerCase())}">${escape(publicationLabels[publicationStatus] || publicationStatus)}</span>` : `<span class="cc-status">${__("正常")}</span>`;
		return `<tr data-asset-card="${escape(asset.name)}"><td><input type="checkbox" class="cc-asset-select" data-asset="${escape(asset.name)}" ${this.selected.has(asset.name) ? "checked" : ""}></td><td class="cc-list-name"><strong>${escape(asset.title)}</strong><span class="text-muted small">${escape(asset.file_name || "")}</span></td><td>${this.inline_tags(asset)}</td><td>${stars}</td><td>${escape(asset.file_type || asset.mime_type || "-")}</td><td>${escape(size)}</td><td>${escape(asset.responsible_user || "-")}</td><td>${asset.modified ? frappe.datetime.str_to_user(asset.modified) : "-"}</td><td>${workflowStatus}${asset.is_locked ? ` <span class="cc-status locked">${__("已锁定")}</span>` : ""}</td><td><button class="btn btn-default btn-xs" data-cc-action="preview" data-asset="${escape(asset.name)}">${__("预览")}</button> <button class="btn btn-default btn-xs" data-cc-action="detail" data-asset="${escape(asset.name)}">${__("详情")}</button>${asset.permissions?.can_download && !this.show_trashed ? ` <button class="btn btn-default btn-xs" data-cc-action="download" data-asset="${escape(asset.name)}">${__("下载")}</button>` : ""}</td></tr>`;
	}

	render_dashboard() {
		const dashboard = this.data.dashboard || {};
		const size = frappe.utils.format_size ? frappe.utils.format_size(dashboard.total_size || 0) : `${dashboard.total_size || 0} B`;
		const cards = [
			{label: __("全部文件"), value: dashboard.total_files || 0, view: "all"},
			{label: __("空间占用"), value: size},
			{label: __("我的收藏"), value: dashboard.favorite_count || 0, view: "favorites"},
			{label: __("最近使用"), value: dashboard.recent_count || 0, view: "recent"},
			{label: __("我上传的"), value: dashboard.my_upload_count || 0, view: "mine"},
		];
		this.wrapper.find(".cc-dashboard").html(cards.map((card) => `<button class="cc-stat" ${card.view ? `data-view="${card.view}"` : ""}><div class="cc-stat-value">${frappe.utils.escape_html(String(card.value))}</div><div class="cc-stat-label">${card.label}</div></button>`).join(""));
	}

	render_type_options() {
		const current = this.filters.file_type || "";
		const options = [`<option value="">${__("全部类型")}</option>`, ...this.data.available_types.map((type) => `<option value="${frappe.utils.escape_html(type)}">${frappe.utils.escape_html(type)}</option>`)].join("");
		this.wrapper.find(".cc-filter-type").html(options).val(current);
	}

	asset_card(asset) {
		const escape = frappe.utils.escape_html;
		const mime = asset.mime_type || "";
		const icon = mime.startsWith("image/") ? "image" : mime === "application/pdf" ? "file-pdf" : mime.startsWith("video/") ? "video" : "file";
		const thumbnailStatus = asset.thumbnail_status || asset.preview_status || "";
		const thumbnailUrl = asset.thumbnail_url || asset.safe_thumbnail_url || "";
		const thumbnailSupported = thumbnailUrl && !["Failed", "Unsupported"].includes(thumbnailStatus);
		const locked = Boolean(asset.is_locked);
		const hasPublicationStatus = this.featureFlags.approval_workflow && Object.prototype.hasOwnProperty.call(asset, "publication_status");
		const publicationStatus = asset.publication_status || "Draft";
		const publicationLabels = {Draft: __("草稿"), Pending: __("待审批"), Published: __("已发布"), Rejected: __("已驳回")};
		const expired = Boolean(asset.expired) || Boolean(asset.expires_on && frappe.datetime.get_diff(asset.expires_on, frappe.datetime.get_today()) < 0);
		const expiresSoon = !expired && asset.expires_on && frappe.datetime.get_diff(asset.expires_on, frappe.datetime.get_today()) <= 30;
		const currentGrant = this.featureFlags.borrowing ? (asset.borrow_grant || asset.current_grant || asset.access_grant || {}) : {};
		const grantExpiresAt = this.featureFlags.borrowing ? (asset.borrow_valid_until || asset.grant_expires_at || asset.access_expires_at || asset.access_grant_expires_at || currentGrant.valid_until) : null;
		const grantExpired = currentGrant.effective_status === "Expired" || Boolean(asset.grant_expired || asset.access_grant_expired) || Boolean(grantExpiresAt && frappe.datetime.get_diff(grantExpiresAt, frappe.datetime.now_datetime()) < 0);
		const grantExpiring = !grantExpired && Boolean(asset.grant_expiring || asset.access_grant_expiring || (grantExpiresAt && frappe.datetime.get_diff(grantExpiresAt, frappe.datetime.now_datetime()) <= 3));
		const canMove = asset.permissions?.can_move && !this.show_trashed && !locked;
		const canDownload = asset.permissions?.can_download && !this.show_trashed;
		const canDelete = asset.permissions?.can_delete && !locked;
		const canCopy = canDownload && !locked;
		const currentRating = Math.max(0, Math.min(5, Number(asset.my_rating ?? asset.user_rating ?? asset.rating ?? 0) || 0));
		const ratePermission = Object.prototype.hasOwnProperty.call(asset.permissions || {}, "can_rate")
			? asset.permissions.can_rate : asset.permissions?.can_upload;
		const canRate = Boolean(ratePermission) && !locked && (!this.featureFlags.approval_workflow || publicationStatus !== "Pending") && !this.show_trashed;
		const rating = `<div class="cc-rating ${canRate ? "" : "read-only"}" data-rating-asset="${escape(asset.name)}" data-current-rating="${currentRating}" aria-label="${__("文件评分")}">${[1, 2, 3, 4, 5].map((value) => `<button type="button" class="cc-rating-star ${value <= currentRating ? "active" : ""}" data-cc-rating="${value}" data-asset="${escape(asset.name)}" title="${value === currentRating && canRate ? __("清除评分") : __("设置为 {0} 星", [value])}" ${canRate ? "" : "disabled"}>★</button>`).join("")}</div>`;
		const statuses = [
			"",
			hasPublicationStatus ? `<span class="cc-status ${escape(publicationStatus.toLowerCase())}">${escape(publicationLabels[publicationStatus] || publicationStatus)}</span>` : "",
			grantExpired ? `<span class="cc-status grant-expired">${__("授权已过期")}</span>` : "",
			grantExpiring ? `<span class="cc-status grant-expiring" title="${grantExpiresAt ? escape(frappe.datetime.str_to_user(grantExpiresAt)) : ""}">${__("授权即将到期")}</span>` : "",
			locked ? `<span class="cc-status locked" title="${escape(this.lock_description(asset))}">${frappe.utils.icon("lock", "xs")} ${__("已锁定")}</span>` : "",
			expired ? `<span class="cc-status expired">${__("已到期")}</span>` : "",
			expiresSoon ? `<span class="cc-status expiring">${__("即将到期")} · ${escape(frappe.datetime.str_to_user(asset.expires_on))}</span>` : "",
		].filter(Boolean).join("");
		const primary = this.show_trashed
			? (canDelete ? `<button class="btn btn-xs btn-default" data-cc-action="restore" data-asset="${escape(asset.name)}">${__("恢复")}</button>` : "")
			: `<button class="btn btn-xs btn-default" data-cc-action="detail" data-asset="${escape(asset.name)}">${__("详情")}</button>
			   ${canMove ? `<button class="btn btn-xs btn-default" data-cc-action="rename" data-asset="${escape(asset.name)}">${__("重命名")}</button>` : ""}
			   ${canMove ? `<button class="btn btn-xs btn-default" data-cc-action="move" data-asset="${escape(asset.name)}">${__("移动")}</button>` : ""}
			   ${canCopy ? `<button class="btn btn-xs btn-default" data-cc-action="copy" data-asset="${escape(asset.name)}">${__("复制")}</button>` : ""}
			   ${canDownload ? `<button class="btn btn-xs btn-default" data-cc-action="download" data-asset="${escape(asset.name)}">${__("下载")}</button>` : ""}
			   ${canDelete ? `<button class="btn btn-xs btn-default" data-cc-action="trash" data-asset="${escape(asset.name)}">${__("回收")}</button>` : ""}`;
		return `<article class="cc-card ${this.selected.has(asset.name) ? "selected" : ""}" data-asset-card="${escape(asset.name)}">
			<input type="checkbox" class="cc-card-select cc-asset-select" data-asset="${escape(asset.name)}" ${this.selected.has(asset.name) ? "checked" : ""}>
			<button class="cc-favorite ${asset.is_favorite ? "active" : ""}" data-cc-action="favorite" data-asset="${escape(asset.name)}" title="${asset.is_favorite ? __("取消收藏") : __("收藏")}">${frappe.utils.icon("bookmark", "sm")}</button>
			<div class="cc-file-icon" data-cc-action="preview" data-asset="${escape(asset.name)}">${thumbnailSupported ? `<img class="cc-thumbnail" data-thumbnail-url="${escape(thumbnailUrl)}" loading="lazy" decoding="async" alt="${escape(asset.title || asset.file_name || "")}"><span class="cc-thumbnail-fallback hide">${frappe.utils.icon(icon, "md")}</span>` : `<span class="cc-thumbnail-fallback">${frappe.utils.icon(icon, "md")}</span>`}</div>
			<div class="cc-title" title="${escape(asset.title)}">${escape(asset.title)}</div>
			<div class="cc-filename text-muted" title="${escape(asset.file_name || "")}">${escape(asset.file_name || "")}</div>
			<div class="cc-statuses">${statuses}</div>
			${this.inline_tags(asset)}
			${rating}
			<div class="cc-meta text-muted"><span>v${asset.version_number || 1}</span></div>
			<div class="cc-actions">
				${asset.file_url ? `<button class="btn btn-xs btn-primary" data-cc-action="preview" data-asset="${escape(asset.name)}">${__("预览")}</button>` : ""}
				${primary}
			</div>
		</article>`;
	}

	lock_description(asset) {
		if (!asset?.is_locked) return "";
		const parts = [__("文件已锁定")];
		if (asset.locked_by) parts.push(__("锁定人：{0}", [asset.locked_by]));
		if (asset.locked_at) parts.push(__("锁定时间：{0}", [frappe.datetime.str_to_user(asset.locked_at)]));
		return parts.join(" · ");
	}

	async rate_asset(assetName, selectedRating, control) {
		if (!assetName || control.hasClass("saving")) return;
		const asset = this.data?.assets?.find((row) => row.name === assetName);
		if (!asset) return;
		const publicationStatus = asset.publication_status || "Draft";
		const ratePermission = Object.prototype.hasOwnProperty.call(asset.permissions || {}, "can_rate")
			? asset.permissions.can_rate : asset.permissions?.can_upload;
		if (!ratePermission || asset.is_locked || publicationStatus === "Pending" || this.show_trashed) return;
		const oldRating = Math.max(0, Math.min(5, Number(asset.my_rating ?? asset.user_rating ?? asset.rating ?? 0) || 0));
		const rating = selectedRating === oldRating ? 0 : selectedRating;
		const paint = (value) => control.find(".cc-rating-star").each((_, star) => {
			$(star).toggleClass("active", Number($(star).data("cc-rating")) <= value);
		});
		control.addClass("saving").find("button").prop("disabled", true);
		paint(rating);
		try {
			const response = await frappe.call({
				method: "channel_erp.content_center.rate_asset",
				args: {asset: assetName, rating},
			});
			const saved = Number(response.message?.rating ?? response.message?.my_rating ?? rating) || 0;
			asset.my_rating = saved;
			asset.user_rating = saved;
			asset.rating = response.message?.average_rating ?? saved;
			control.attr("data-current-rating", saved);
			paint(saved);
		} catch (error) {
			paint(oldRating);
			frappe.show_alert({message: __("评分保存失败，已恢复原评分"), indicator: "red"});
		} finally {
			control.removeClass("saving").find("button").prop("disabled", false);
		}
	}

	remove_inline_tag(assetName, index, control) {
		const asset = this.data?.assets?.find((row) => row.name === assetName);
		if (!asset || !this.can_edit_inline_tags(asset) || control.hasClass("saving")) return;
		const tags = this.asset_tags(asset);
		if (!Number.isInteger(index) || index < 0 || index >= tags.length) return;
		tags.splice(index, 1);
		this.save_inline_tags(asset, tags, control);
	}

	commit_inline_tag(input) {
		const control = input.closest(".cc-inline-tags");
		if (control.hasClass("saving")) return;
		const asset = this.data?.assets?.find((row) => row.name === input.data("asset"));
		if (!asset || !this.can_edit_inline_tags(asset)) return;
		const additions = String(input.val() || "").split(/[,，\n\r]+/).map((tag) => tag.trim()).filter(Boolean);
		if (!additions.length) {
			input.addClass("hide").siblings(".cc-inline-tag-add").removeClass("hide");
			return;
		}
		if (additions.some((tag) => tag.length > 30)) return frappe.show_alert({message: __("单个标签最多 30 个字符"), indicator: "orange"});
		const tags = this.asset_tags(asset);
		for (const tag of additions) {
			if (!tags.some((current) => current.toLocaleLowerCase() === tag.toLocaleLowerCase())) tags.push(tag);
		}
		if (tags.length > 20) return frappe.show_alert({message: __("最多设置 20 个标签"), indicator: "orange"});
		input.val("").addClass("hide");
		this.save_inline_tags(asset, tags, control);
	}

	async save_inline_tags(asset, tags, control) {
		control.addClass("saving");
		try {
			const response = await frappe.call({
				method: "channel_erp.content_center.update_asset",
				args: {asset: asset.name, tags: tags.join(",")},
			});
			asset.tags = response.message?.tags ?? tags.join(",");
			frappe.show_alert({message: __("标签已更新"), indicator: "green"});
			await this.load();
		} catch (error) {
			control.removeClass("saving").find(".cc-inline-tag-input").addClass("hide").siblings(".cc-inline-tag-add").removeClass("hide");
			frappe.show_alert({message: __("标签保存失败"), indicator: "red"});
		}
	}

	apply_filters() {
		const ratingMode = this.wrapper.find(".cc-filter-rating-mode").val() || "all";
		const ratingValue = Number(this.wrapper.find(".cc-rating-filter").attr("data-rating")) || 0;
		if (["exact", "at_least"].includes(ratingMode) && !ratingValue) return frappe.msgprint(__("请选择 1 到 5 星。"));
		const selectedTags = this.tagFilter.tags.slice();
		this.filters = {
			file_type: this.wrapper.find(".cc-filter-type").val() || null,
			uploaded_by: this.uploader_filter.get_value() || null,
			responsible_user: this.responsible_filter.get_value() || null,
			department: this.department_filter.get_value() || null,
			expiry_state: this.wrapper.find(".cc-filter-expiry").val() || "any",
			lock_state: this.wrapper.find(".cc-filter-lock").val() || "all",
			...(this.wrapper.find(".cc-filter-publication").val() ? {publication_status: this.wrapper.find(".cc-filter-publication").val()} : {}),
			rating_mode: ratingMode,
			rating: ["exact", "at_least"].includes(ratingMode) ? ratingValue : null,
			...(ratingMode === "at_least" && ratingValue ? {min_rating: ratingValue} : {}),
			tags: selectedTags.length ? selectedTags : null,
			tag_match: this.wrapper.find(".cc-filter-tag-match").val() || "any",
			modified_from: this.wrapper.find(".cc-filter-from").val() || null,
			modified_to: this.wrapper.find(".cc-filter-to").val() || null,
			search_all_folders: this.wrapper.find(".cc-filter-all").prop("checked") ? 1 : 0,
		};
		this.selected.clear();
		this.load();
	}

	clear_filters() {
		this.filters = {};
		this.uploader_filter.set_value("");
		this.responsible_filter.set_value("");
		this.department_filter.set_value("");
		this.wrapper.find(".cc-filter-type,.cc-filter-from,.cc-filter-to,.cc-filter-lock,.cc-filter-publication").val("");
		this.wrapper.find(".cc-filter-expiry").val("any");
		this.wrapper.find(".cc-filter-rating-mode").val("all");
		this.wrapper.find(".cc-rating-filter").attr("data-rating", "0").find("button").removeClass("active");
		this.wrapper.find(".cc-filter-tag-match").val("any");
		this.tagFilter.tags = [];
		this.tagFilter.render();
		this.wrapper.find(".cc-filter-all").prop("checked", false);
		this.selected.clear();
		this.load();
	}

	render_tag_facets(query = "") {
		const raw = this.data?.tag_facets || [];
		const facets = Array.isArray(raw) ? raw : Object.entries(raw).map(([tag, count]) => ({tag, count}));
		const selected = new Set((this.tagFilter?.tags || []).map((tag) => tag.toLocaleLowerCase()));
		const needle = String(query || "").trim().toLocaleLowerCase();
		this.wrapper.find(".cc-tag-facets").html(facets.filter((facet) => {
			const tag = String(facet.tag || facet.value || facet.label || "");
			return tag && !selected.has(tag.toLocaleLowerCase()) && (!needle || tag.toLocaleLowerCase().includes(needle));
		}).slice(0, 30).map((facet) => {
			const tag = facet.tag || facet.value || facet.label;
			return `<button type="button" class="cc-tag-facet" data-tag="${frappe.utils.escape_html(tag)}">${frappe.utils.escape_html(facet.display_tag || tag)} <span class="text-muted">${facet.count ?? facet.value_count ?? 0}</span></button>`;
		}).join(""));
		this.wrapper.find(".cc-filter-tags input").off("input.tag-facets").on("input.tag-facets", (event) => this.render_tag_facets(event.target.value));
	}

	render_filter_chips() {
		const escape = frappe.utils.escape_html;
		const chips = [];
		const add = (key, label, value = "") => chips.push(`<span class="cc-filter-chip" data-filter-key="${escape(key)}" data-filter-value="${escape(value)}"><span>${escape(label)}</span><button type="button" aria-label="${__("移除筛选")}">×</button></span>`);
		const labels = {file_type: __("类型"), uploaded_by: __("上传人"), responsible_user: __("负责人"), department: __("部门"), expiry_state: __("到期"), lock_state: __("锁定"), publication_status: __("审批"), modified_from: __("开始"), modified_to: __("结束")};
		Object.entries(labels).forEach(([key, label]) => {
			const value = this.filters[key];
			if (value && !["any", "all"].includes(value)) add(key, `${label}：${value}`);
		});
		if (this.filters.rating_mode && this.filters.rating_mode !== "all") {
			const label = this.filters.rating_mode === "unrated" ? __("未评分") : `${this.filters.rating_mode === "exact" ? __("精确") : __("至少")} ${this.filters.rating || 0} ★`;
			add("rating", label);
		}
		(this.filters.tags || []).forEach((tag) => add("tags", `${__("标签")}（${this.filters.tag_match === "all" ? __("全部") : __("任一")}）：${tag}`, tag));
		if (this.filters.search_all_folders) add("search_all_folders", __("所有文件夹"));
		this.wrapper.find(".cc-active-filters").html(chips.length ? `${chips.join("")}<button class="btn btn-link btn-xs cc-clear-filter-chips">${__("清除全部")}</button>` : "");
	}

	remove_filter_chip(chip) {
		const key = chip.data("filter-key");
		const value = String(chip.data("filter-value") || "");
		if (key === "tags") {
			this.tagFilter.tags = this.tagFilter.tags.filter((tag) => tag.toLocaleLowerCase() !== value.toLocaleLowerCase());
			this.tagFilter.render();
			this.filters.tags = this.tagFilter.tags.length ? this.tagFilter.tags.slice() : null;
		} else if (key === "rating") {
			delete this.filters.rating_mode; delete this.filters.rating; delete this.filters.min_rating;
			this.wrapper.find(".cc-filter-rating-mode").val("all");
			this.wrapper.find(".cc-rating-filter").attr("data-rating", "0").find("button").removeClass("active");
		} else {
			delete this.filters[key];
			const selectors = {file_type: ".cc-filter-type", expiry_state: ".cc-filter-expiry", lock_state: ".cc-filter-lock", publication_status: ".cc-filter-publication", modified_from: ".cc-filter-from", modified_to: ".cc-filter-to"};
			if (selectors[key]) this.wrapper.find(selectors[key]).val(key === "expiry_state" ? "any" : "");
			if (key === "uploaded_by") this.uploader_filter.set_value("");
			if (key === "responsible_user") this.responsible_filter.set_value("");
			if (key === "department") this.department_filter.set_value("");
			if (key === "search_all_folders") this.wrapper.find(".cc-filter-all").prop("checked", false);
			if (key === "tag_match") this.wrapper.find(".cc-filter-tag-match").val("any");
		}
		this.selected.clear();
		this.load();
	}

	toggle_asset_selection(event) {
		const name = $(event.currentTarget).data("asset");
		event.currentTarget.checked ? this.selected.add(name) : this.selected.delete(name);
		this.render_selection();
	}

	select_all(checked) {
		this.selected.clear();
		if (checked) this.data.assets.forEach((asset) => this.selected.add(asset.name));
		this.render_selection();
	}

	render_selection() {
		this.wrapper.find(".cc-asset-select").each((_, input) => {
			const checked = this.selected.has($(input).data("asset"));
			input.checked = checked;
			$(input).closest(".cc-card,[data-asset-card]").toggleClass("selected", checked);
		});
		const allSelected = this.data?.assets?.length && this.data.assets.every((asset) => this.selected.has(asset.name));
		this.wrapper.find(".cc-select-all").prop("checked", Boolean(allSelected));
		this.wrapper.find(".cc-bulk-bar").toggleClass("hide", !this.selected.size);
		this.wrapper.find(".cc-selected-count").text(__("已选择 {0} 项", [this.selected.size]));
		const hasLocked = this.selected_assets([...this.selected]).some((asset) => asset.is_locked);
		this.wrapper.find("[data-cc-bulk='move'],[data-cc-bulk='copy'],[data-cc-bulk='trash'],[data-cc-bulk='restore'],[data-cc-bulk='permanent-delete']")
			.prop("disabled", hasLocked)
			.attr("title", hasLocked ? __("所选文件包含已锁定文件，仅可预览或下载") : "");
		this.wrapper.find("[data-cc-governance='responsible_user'],[data-cc-governance='department'],[data-cc-governance='expires_on'],[data-cc-governance='tags']")
			.prop("disabled", hasLocked)
			.attr("title", hasLocked ? __("请先解锁所选文件，再修改治理信息") : "");
	}

	async create_folder(parent = this.current_folder) {
		const target = (this.data?.folders || []).find((row) => row.name === parent);
		if (!target || !this.folder_action_permissions(target).create) return frappe.msgprint(__("没有在此目录新建子文件夹的权限"));
		frappe.prompt({fieldname: "folder_name", fieldtype: "Data", label: __("文件夹名称"), reqd: 1}, async (values) => {
			await frappe.call({method: "channel_erp.content_center.create_folder", args: {folder_name: values.folder_name, parent}});
			this.load();
		}, __("在“{0}”中新建文件夹", [target.folder_name]), __("创建"));
	}

	async rename_current_folder() {
		return this.rename_folder(this.current_folder);
	}

	async rename_folder(folderName = this.current_folder) {
		const folder = this.data?.folders?.find((row) => row.name === folderName);
		if (!folder || !this.folder_action_permissions(folder).rename) return frappe.msgprint(__("该目录不能重命名，或您没有权限"));
		frappe.prompt(
			{fieldname: "folder_name", fieldtype: "Data", label: __("文件夹名称"), default: folder.folder_name, reqd: 1},
			async (values) => {
				await frappe.call({
					method: "channel_erp.content_center.rename_folder",
					args: {folder: folderName, folder_name: values.folder_name},
				});
				frappe.show_alert({message: __("文件夹已重命名"), indicator: "green"});
				this.load();
			},
			__("重命名当前文件夹"),
			__("保存")
		);
	}

	folder_options(excluded = [], requiredAction = null) {
		const blocked = new Set(excluded);
		return this.data.folders.filter((folder) => !blocked.has(folder.name) && (!requiredAction || this.folder_action_permissions(folder)[requiredAction])).map((folder) => ({label: folder.folder_name, value: folder.name}));
	}

	choose_folder(title, excluded = [], primary_action_label = __("确认"), requiredAction = null) {
		return new Promise((resolve) => {
			const options = this.folder_options(excluded, requiredAction);
			if (!options.length) {
				frappe.msgprint(__("没有可用的目标文件夹"));
				resolve(null);
				return;
			}
			const dialog = new frappe.ui.Dialog({
				title,
				fields: [{fieldname: "folder", fieldtype: "Select", label: __("目标文件夹"), options, reqd: 1}],
				primary_action_label,
				primary_action: (values) => { dialog.hide(); resolve(values.folder); },
			});
			dialog.show();
		});
	}

	async move_current_folder() {
		return this.move_folder(this.current_folder);
	}

	async move_folder(folderName = this.current_folder) {
		const current = this.data.folders.find((folder) => folder.name === folderName);
		if (!current || !this.folder_action_permissions(current).move) return frappe.msgprint(__("该文件夹不能移动或没有权限"));
		const excluded = this.data.folders.filter((folder) => folder.lft >= current.lft && folder.rgt <= current.rgt).map((folder) => folder.name);
		const target = await this.choose_folder(__("移动文件夹“{0}”", [current.folder_name]), excluded, __("确认移动"), "create");
		if (!target) return;
		await frappe.call({method: "channel_erp.content_center.move_folder", args: {folder: folderName, target_parent: target}});
		if (this.current_folder === folderName) this.current_folder = target;
		this.load();
	}

	delete_folder(folderName) {
		const folder = (this.data?.folders || []).find((row) => row.name === folderName);
		if (!folder || !this.folder_action_permissions(folder).delete) return frappe.msgprint(__("该文件夹不能删除或没有权限"));
		frappe.confirm(
			__("确认删除文件夹“{0}”？只有不包含子文件夹和文件的空目录才能删除。", [folder.folder_name]),
			async () => {
				await frappe.call({method: "channel_erp.content_center.delete_folder", args: {folder: folderName}});
				if (this.current_folder === folderName) this.current_folder = folder.parent_channel_content_folder || this.data.root_folder;
				this.collapsedFolders.delete(folderName);
				frappe.show_alert({message: __("空文件夹已删除"), indicator: "green"});
				this.load();
			}
		);
	}

	upload_files(asset = null) {
		if (!asset && !this.data?.permissions?.can_upload) return;
		const current = asset && this.data?.assets?.find((row) => row.name === asset);
		if (current?.is_locked) return frappe.msgprint(__("文件已锁定，不能上传新版本"));
		let uploaded = 0;
		new frappe.ui.FileUploader({
			allow_multiple: !asset,
			make_attachments_public: false,
			dialog_title: asset ? __("上传新版本") : __("上传文件（可多选或拖拽）"),
			on_success: async (file) => {
				const method = asset ? "channel_erp.content_center.add_version" : "channel_erp.content_center.register_uploaded_file";
				const args = asset ? {asset, file: file.name, change_note: __("从内容中心上传新版本")} : {file: file.name, folder: this.current_folder};
				const response = await frappe.call({method, args});
				if (!asset && response.message?.duplicate) {
					const existing = response.message.existing;
					frappe.confirm(
						__("检测到相同文件已存在：{0}。是否仍创建一个新的逻辑文件？", [existing.title]),
						async () => {
							await frappe.call({method, args: {...args, allow_duplicate: 1}});
							uploaded += 1;
							frappe.show_alert({message: __("已创建文件副本"), indicator: "green"});
							this.load();
						},
						async () => {
							await frappe.call({method: "channel_erp.content_center.discard_uploaded_file", args: {file: file.name}});
							frappe.show_alert({message: __("已取消重复文件"), indicator: "blue"});
						}
					);
					return;
				}
				uploaded += 1;
				frappe.show_alert({message: asset ? __("新版本已保存") : __("已加入内容中心：{0} 个文件", [uploaded]), indicator: "green"});
				this.load();
			},
		});
	}

	toggle_trash() {
		this.show_trashed = !this.show_trashed;
		this.selected.clear();
		this.load();
	}

	async bulk_action(action, single_asset = null) {
		const assets = single_asset ? [single_asset] : [...this.selected];
		if (!assets.length) return;
		if (action === "download") return this.download_assets(assets);
		if (this.selected_assets(assets).some((asset) => asset.is_locked)) {
			return frappe.msgprint(__("所选文件包含已锁定文件。锁定文件仅可预览或下载。"));
		}
		if (action === "copy") return this.copy_assets(assets);
		if (action === "permanent-delete") {
			return frappe.confirm(__("彻底删除后无法恢复，确定继续？"), async () => {
				await frappe.call({method: "channel_erp.content_center.permanently_delete_assets", args: {assets}});
				this.selected.clear();
				frappe.show_alert({message: __("文件已彻底删除"), indicator: "green"});
				this.load();
			});
		}
		let target_folder = null;
		if (action === "move") {
			target_folder = await this.choose_folder(__("移动所选文件"), [], __("确认移动"));
			if (!target_folder) return;
		}
		const run = async () => {
			await frappe.call({method: "channel_erp.content_center.bulk_asset_action", args: {assets, action, target_folder}});
			this.selected.clear();
			frappe.show_alert({message: __("已完成 {0} 个文件", [assets.length]), indicator: "green"});
			this.load();
		};
		if (action === "trash") return frappe.confirm(__("将所选文件移入回收站？"), run);
		return run();
	}

	async bulk_governance(action) {
		if (this.show_trashed) return frappe.msgprint(__("回收站中的文件不能执行批量治理"));
		const assets = [...this.selected];
		if (!assets.length) return;
		const rows = this.selected_assets(assets);
		const labels = {
			responsible_user: __("设置负责人"),
			department: __("设置所属部门"),
			expires_on: __("设置到期日"),
			tags: __("设置标签"),
			lock: __("锁定文件"),
			unlock: __("解锁文件"),
		};
		if (rows.some((asset) => !asset.permissions?.can_upload)) {
			return frappe.msgprint(__("所选文件中包含没有编辑权限的文件"));
		}
		if (!["lock", "unlock"].includes(action) && rows.some((asset) => asset.is_locked)) {
			return frappe.msgprint(__("所选文件包含已锁定文件，请先解锁后再修改治理信息"));
		}

		const execute = (updates = {}) => {
			frappe.confirm(
				__("此操作将影响 {0} 个文件，确认执行“{1}”吗？", [assets.length, labels[action]]),
				async () => {
					const response = await frappe.call({
						method: "channel_erp.content_center.bulk_update_assets",
						args: {assets, ...updates},
					});
					const count = response.message?.count ?? response.message?.updated ?? assets.length;
					this.selected.clear();
					frappe.show_alert({message: __("已更新 {0} 个文件", [count]), indicator: "green"});
					this.load();
				}
			);
		};

		if (["lock", "unlock"].includes(action)) return execute({lock_action: action});
		if (action === "tags") {
			let tagInput;
			const dialog = new frappe.ui.Dialog({
				title: labels.tags,
				fields: [
					{fieldname: "tags", fieldtype: "HTML", label: __("标签")},
					{fieldname: "hint", fieldtype: "HTML", options: `<p class="text-muted small">${__("将替换所选文件的标签；留空可清除。支持 Enter、逗号或粘贴多行。")}</p>`},
				],
				primary_action_label: __("下一步"),
				primary_action() {
					dialog.hide();
					execute({tags: tagInput.value()});
				},
			});
			tagInput = new ChannelTagInput(dialog.fields_dict.tags.$wrapper);
			dialog.show();
			return;
		}
		const fields = {
			responsible_user: {fieldtype: "Link", options: "User", label: __("负责人"), description: __("留空可清除负责人")},
			department: {fieldtype: "Link", options: "Department", label: __("所属部门"), description: __("留空可清除部门")},
			expires_on: {fieldtype: "Date", label: __("到期日"), description: __("留空可清除到期日")},
		};
		const field = {fieldname: action, ...fields[action]};
		frappe.prompt(
			field,
			(values) => execute({[action]: values[action] || ""}),
			labels[action],
			__("下一步")
		);
	}

	selected_assets(asset_names) {
		const names = new Set(asset_names);
		return (this.data?.assets || []).filter((asset) => names.has(asset.name));
	}

	can_download_assets(asset_names) {
		const assets = this.selected_assets(asset_names);
		return assets.length === asset_names.length && assets.every((asset) => asset.permissions?.can_download);
	}

	async rename_asset(asset_name) {
		const asset = this.data?.assets?.find((row) => row.name === asset_name);
		if (asset?.is_locked) return frappe.msgprint(__("文件已锁定，仅可预览或下载"));
		if (!asset?.permissions?.can_move || this.show_trashed) return frappe.msgprint(__("没有重命名此文件的权限"));
		frappe.prompt(
			{fieldname: "title", fieldtype: "Data", label: __("文件名称"), default: asset.title, reqd: 1},
			async (values) => {
				await frappe.call({method: "channel_erp.content_center.rename_asset", args: {asset: asset_name, title: values.title}});
				frappe.show_alert({message: __("文件已重命名"), indicator: "green"});
				this.load();
			},
			__("重命名文件"),
			__("保存")
		);
	}

	async copy_assets(asset_names) {
		if (this.show_trashed) return;
		if (!this.can_download_assets(asset_names)) return frappe.msgprint(__("所选文件中包含没有下载权限的文件，无法复制"));
		const target_folder = await this.choose_folder(__("复制到文件夹"), [], __("确认复制"));
		if (!target_folder) return;
		const response = await frappe.call({
			method: "channel_erp.content_center.copy_assets",
			args: {asset_names, target_folder},
		});
		this.selected.clear();
		const count = response.message?.count ?? asset_names.length;
		frappe.show_alert({message: __("已复制 {0} 个文件", [count]), indicator: "green"});
		this.load();
	}

	download_assets(asset_names) {
		if (this.show_trashed) return;
		if (!this.can_download_assets(asset_names)) return frappe.msgprint(__("所选文件中包含没有下载权限的文件，无法下载"));
		const query = encodeURIComponent(JSON.stringify(asset_names));
		window.open(`/api/method/channel_erp.content_center.download_assets?assets=${query}`, "_blank", "noopener");
	}

	async handle_action(action, asset, url) {
		if (action === "open") return window.open(url, "_blank", "noopener");
		if (action === "favorite") {
			await frappe.call({method: "channel_erp.content_center.toggle_favorite", args: {asset}});
			return this.load();
		}
		if (action === "preview") return this.show_preview(asset);
		if (action === "detail") return this.show_detail(asset);
		if (action === "rename") return this.rename_asset(asset);
		if (action === "copy") return this.bulk_action("copy", asset);
		if (action === "download") return this.bulk_action("download", asset);
		if (action === "move") return this.bulk_action("move", asset);
		if (action === "trash") return this.bulk_action("trash", asset);
		if (action === "restore") return this.bulk_action("restore", asset);
	}

	async show_preview(asset_name) {
		let asset = this.data.assets.find((row) => row.name === asset_name) || this.search_results.find((row) => (row.asset || row.asset_name || row.name) === asset_name);
		if (asset && !asset.file_url) {
			const response = await frappe.call({method: "channel_erp.content_center.get_asset_detail", args: {asset: asset_name}});
			asset = response.message?.asset || asset;
		}
		if (!asset?.file_url) return;
		await frappe.call({method: "channel_erp.content_center.record_asset_access", args: {asset: asset_name}});
		const escape = frappe.utils.escape_html;
		const url = escape(asset.file_url);
		const mime = asset.mime_type || "";
		let preview;
		if (mime.startsWith("image/")) preview = `<img src="${url}" alt="${escape(asset.title)}">`;
		else if (mime === "application/pdf") preview = `<iframe src="${url}" title="${escape(asset.title)}"></iframe>`;
		else if (mime.startsWith("video/")) preview = `<video controls><source src="${url}" type="${escape(mime)}"></video>`;
		else if (mime.startsWith("audio/")) preview = `<audio controls><source src="${url}" type="${escape(mime)}"></audio>`;
		else if (mime.startsWith("text/")) preview = `<iframe src="${url}" title="${escape(asset.title)}"></iframe>`;
		else preview = `<div class="cc-preview-fallback">${frappe.utils.icon("file", "xl")}<p>${__("该格式暂不支持浏览器内预览，可打开或下载查看。")}</p></div>`;
		const dialog = new frappe.ui.Dialog({title: asset.title, size: "extra-large", fields: [{fieldname: "preview", fieldtype: "HTML"}]});
		dialog.fields_dict.preview.$wrapper.html(`<div class="cc-preview">${preview}</div><div class="mt-3"><a class="btn btn-primary btn-sm" href="${url}" target="_blank" rel="noopener">${__("在新窗口打开")}</a></div>`);
		dialog.show();
	}

	show_knowledge_dashboard() {
		const permissions = this.data?.permissions || {};
		if (!permissions.can_view_knowledge_dashboard && !permissions.can_view_quality_metrics && !permissions.can_manage_knowledge) {
			return frappe.msgprint(__("没有查看知识运营看板的权限"));
		}
		let dialog;
		dialog = new frappe.ui.Dialog({
			title: __("知识运营"),
			size: "extra-large",
			fields: [
				{fieldname: "days", fieldtype: "Select", label: __("统计周期"), options: [{label: __("最近30天"), value: "30"}, {label: __("最近90天"), value: "90"}], default: "30", onchange: () => this.load_knowledge_dashboard(dialog, 1)},
				{fieldname: "issue_type", fieldtype: "Select", label: __("问题类型"), options: [
					{label: __("点击指标或选择问题"), value: ""},
					{label: __("未建立索引"), value: "unindexed"},
					{label: __("索引异常"), value: "index_health"},
					{label: __("治理信息缺失"), value: "governance"},
					{label: __("已过期"), value: "expired"},
					{label: __("长期未使用"), value: "stale"},
					{label: __("重复文件"), value: "duplicate"},
				], onchange: () => this.load_quality_issues(dialog, 1)},
				{fieldname: "dashboard", fieldtype: "HTML"},
			],
		});
		dialog.show();
		this.load_knowledge_dashboard(dialog, 1);
	}

	async load_knowledge_dashboard(dialog, issuePage = 1) {
		if (!dialog?.fields_dict?.dashboard) return;
		const wrapper = dialog.fields_dict.dashboard.$wrapper;
		wrapper.html(`<div class="cc-empty">${__("正在加载知识运营指标…")}</div>`);
		try {
			const days = Number(dialog.get_value("days") || 30);
			const response = await frappe.call({
				method: "channel_erp.content_center.get_knowledge_dashboard",
				args: {period_days: days},
			});
			const dashboard = response.message || {};
			dialog.knowledgeDashboard = dashboard;
			this.render_knowledge_dashboard(dialog);
			if (dialog.get_value("issue_type")) await this.load_quality_issues(dialog, issuePage, false);
		} catch (error) {
			wrapper.html(`<div class="alert alert-warning">${__("知识运营看板暂不可用，内容中心其他功能不受影响。")}</div>`);
		}
	}

	render_knowledge_dashboard(dialog) {
		const escape = frappe.utils.escape_html;
		const wrapper = dialog.fields_dict.dashboard.$wrapper;
		const dashboard = dialog.knowledgeDashboard || {};
		const metrics = dashboard.metrics || dashboard;
		const percent = (value) => Math.max(0, Math.min(100, Number(value) || 0));
		const metricCards = [
			{label: __("文件覆盖率"), value: metrics.file_coverage_rate ?? metrics.coverage_rate ?? 0, percent: true, issue: "unindexed"},
			{label: __("索引健康"), value: metrics.index_health_rate ?? metrics.index_health ?? 0, percent: true, issue: "index_health"},
			{label: __("治理完整度"), value: metrics.governance_completeness ?? metrics.governance_rate ?? 0, percent: true, issue: "governance"},
			{label: __("过期文件"), value: metrics.expired_count ?? metrics.expired_files ?? 0, issue: "expired"},
			{label: __("长期未使用"), value: metrics.stale_count ?? metrics.unused_count ?? 0, issue: "stale"},
			{label: __("重复文件"), value: metrics.duplicate_count ?? metrics.duplicate_files ?? 0, issue: "duplicate"},
			{label: __("搜索次数"), value: metrics.search_count ?? metrics.total_searches ?? 0},
			{label: __("零结果率"), value: metrics.zero_result_rate ?? 0, percent: true},
		];
		const cards = metricCards.map((metric) => {
			const value = metric.percent ? percent(metric.value) : Number(metric.value) || 0;
			return `<button class="cc-knowledge-metric" ${metric.issue ? `data-issue-type="${metric.issue}"` : ""}>
				<div class="cc-knowledge-value">${escape(metric.percent ? `${value.toFixed(1)}%` : String(value))}</div>
				<div class="text-muted small">${metric.label}</div>
				${metric.percent ? `<div class="cc-progress"><span style="width:${value}%"></span></div>` : ""}
			</button>`;
		}).join("");
		const popularQueries = dashboard.search_terms_enabled === false ? [] : (Array.isArray(dashboard.popular_queries) ? dashboard.popular_queries : []);
		const popular = popularQueries.length ? `<h6 class="mt-4">${__("热门查询")}</h6>${popularQueries.map((query) => `<div class="cc-popular-query"><span>${escape(query.query || query.term || query.text || "")}</span><strong>${escape(String(query.count ?? query.search_count ?? 0))}</strong></div>`).join("")}` : "";
		const canRebuild = Boolean(dashboard.permissions?.can_rebuild || dashboard.permissions?.can_rebuild_quality_metrics || this.data.permissions?.can_rebuild_quality_metrics);
		wrapper.html(`<div class="cc-knowledge-metrics">${cards}</div>${popular}<div class="cc-quality-list"><p class="text-muted">${__("点击上方质量指标查看对应问题文件。")}</p></div>${canRebuild ? `<button class="btn btn-default btn-xs mt-3 cc-rebuild-quality">${__("重新计算指标")}</button>` : ""}`);
		wrapper.off("click.knowledge");
		wrapper.on("click.knowledge", ".cc-knowledge-metric[data-issue-type]", (event) => {
			dialog.set_value("issue_type", $(event.currentTarget).data("issue-type"));
		});
		wrapper.on("click.knowledge", ".cc-rebuild-quality", () => this.rebuild_quality_metrics(dialog));
		wrapper.on("click.knowledge", ".cc-quality-open", (event) => {
			const asset = $(event.currentTarget).data("asset");
			if (!asset) return;
			dialog.hide();
			this.show_detail(asset);
		});
		wrapper.on("click.knowledge", ".cc-quality-page", (event) => this.load_quality_issues(dialog, Number($(event.currentTarget).data("page")), false));
	}

	async load_quality_issues(dialog, page = 1, rerenderDashboard = true) {
		const issueType = dialog.get_value("issue_type");
		if (!issueType) return;
		const wrapper = dialog.fields_dict.dashboard.$wrapper;
		if (rerenderDashboard && dialog.knowledgeDashboard) this.render_knowledge_dashboard(dialog);
		const list = wrapper.find(".cc-quality-list").html(`<div class="text-muted">${__("正在加载问题清单…")}</div>`);
		try {
			const response = await frappe.call({
				method: "channel_erp.content_center.get_quality_issues",
				args: {issue_type: issueType, page, page_length: 20},
			});
			const message = response.message || {};
			const issues = Array.isArray(message) ? message : (message.issues || message.results || []);
			const total = Array.isArray(message) ? issues.length : (message.total ?? issues.length);
			const pageLength = message.page_length || 20;
			const currentPage = message.page || page;
			const pages = Math.max(1, Math.ceil(total / pageLength));
			const rows = issues.map((issue) => {
				const asset = issue.asset || issue.asset_name || issue.name;
				return `<div class="cc-search-result mb-2"><div class="cc-search-result-heading"><strong>${frappe.utils.escape_html(issue.title || issue.file_name || asset || __("未知文件"))}</strong><span class="cc-index-status">${frappe.utils.escape_html(issue.issue_label || issue.issue_type || issueType)}</span></div><div class="text-muted small">${frappe.utils.escape_html(issue.reason || issue.detail || "")}${issue.modified ? ` · ${frappe.datetime.str_to_user(issue.modified)}` : ""}</div>${asset ? `<div class="cc-actions"><button class="btn btn-default btn-xs cc-quality-open" data-asset="${frappe.utils.escape_html(asset)}">${__("打开文件")}</button></div>` : ""}</div>`;
			}).join("");
			list.html(`${rows || `<div class="cc-empty">${__("当前没有此类质量问题")}</div>`}<div class="cc-quality-pagination"><button class="btn btn-default btn-xs cc-quality-page" data-page="${Math.max(1, currentPage - 1)}" ${currentPage <= 1 ? "disabled" : ""}>${__("上一页")}</button><span>${__("第 {0}/{1} 页", [currentPage, pages])}</span><button class="btn btn-default btn-xs cc-quality-page" data-page="${Math.min(pages, currentPage + 1)}" ${currentPage >= pages ? "disabled" : ""}>${__("下一页")}</button></div>`);
		} catch (error) {
			list.html(`<div class="alert alert-warning">${__("问题清单暂时无法加载")}</div>`);
		}
	}

	rebuild_quality_metrics(dialog) {
		frappe.confirm(__("确认重新计算知识质量指标？"), async () => {
			await frappe.call({method: "channel_erp.content_center.rebuild_quality_metrics"});
			frappe.show_alert({message: __("知识质量指标已重新计算"), indicator: "green"});
			this.load_knowledge_dashboard(dialog, 1);
		});
	}

	async open_permissions(folderName = this.current_folder) {
		const folder = (this.data?.folders || []).find((row) => row.name === folderName);
		if (!folder || !this.folder_action_permissions(folder).permissions) return frappe.msgprint(__("没有管理此文件夹权限的权限"));
		const response = await frappe.call({method: "channel_erp.content_center.get_folder_permissions", args: {folder: folderName}});
		const data = response.message;
		const escape = frappe.utils.escape_html;
		const source = data.permission_source || data.effective_source || data.inherited_from_folder;
		const ancestors = data.inherited_from || [];
		const sourceFolder = source?.folder_name || source?.label || source?.name || (typeof source === "string" ? source : null);
		const fallbackAncestor = ancestors.length ? ancestors[ancestors.length - 1] : null;
		const inheritedFolder = sourceFolder || fallbackAncestor?.folder_name || fallbackAncestor?.name;
		const isDirect = source?.type === "direct" || source?.is_direct || (!source && (data.permissions || []).length > 0);
		const sourceLabel = isDirect
			? `<span class="indicator-pill green">${__("本目录直接设置")}</span>`
			: inheritedFolder
				? `<span class="indicator-pill blue">${__("继承自：{0}", [escape(inheritedFolder)])}</span>`
				: `<span class="indicator-pill gray">${__("尚无直接权限，使用系统默认权限")}</span>`;
		const inheritancePath = ancestors.map((folder) => escape(folder.folder_name || folder.name)).join(" / ");
		const permissionFields = [
			{fieldname: "subject_type", fieldtype: "Select", label: __("对象类型"), options: "Role\nUser", in_list_view: 1, reqd: 1},
			{fieldname: "role", fieldtype: "Link", label: __("角色"), options: "Role", in_list_view: 1},
			{fieldname: "user", fieldtype: "Link", label: __("用户"), options: "User", in_list_view: 1},
			...[["can_view", "查看"], ["can_download", "下载"], ["can_upload", "上传"], ["can_create_folder", "建目录"], ["can_move", "移动"], ["can_delete", "删除"], ["can_manage", "授权"]].map(([fieldname, label]) => ({fieldname, fieldtype: "Check", label: __(label), in_list_view: 1})),
		];
		const dialog = new frappe.ui.Dialog({
			title: __("文件夹权限：{0}", [data.folder_name]),
			size: "extra-large",
			fields: [
				{fieldname: "help", fieldtype: "HTML", options: `<div class="mb-3"><div class="mb-2">${sourceLabel}</div><p class="text-muted mb-1">${__("当前文件夹未配置的用户将继承最近上级目录权限。直接配置会覆盖继承结果。")}</p>${inheritancePath ? `<p class="text-muted mb-0">${__("继承路径：{0}", [inheritancePath])}</p>` : ""}</div>`},
				{fieldname: "permissions", fieldtype: "Table", label: __("直接权限"), cannot_add_rows: false, in_place_edit: true, data: data.permissions, fields: permissionFields},
			],
			primary_action_label: __("保存权限"),
			primary_action: async (values) => {
				await frappe.call({method: "channel_erp.content_center.save_folder_permissions", args: {folder: folderName, permissions: values.permissions || []}});
				dialog.hide();
				frappe.show_alert({message: __("权限已保存"), indicator: "green"});
				this.load();
			},
		});
		dialog.show();
	}

	async show_health() {
		const response = await frappe.call({method: "channel_erp.content_center.get_content_health"});
		const health = response.message;
		const escape = frappe.utils.escape_html;
		const formatSize = (value) => frappe.utils.format_size ? frappe.utils.format_size(value || 0) : `${value || 0} B`;
		const largest = health.largest_files.map((row) => `<li>${escape(row.title)} · ${formatSize(row.file_size)}</li>`).join("");
		const types = health.type_counts.map((row) => `<span class="cc-tag">${escape(row.file_type)} ${row.count}</span>`).join("");
		const dialog = new frappe.ui.Dialog({title: __("内容中心系统巡检"), size: "large", fields: [{fieldname: "health", fieldtype: "HTML"}]});
		dialog.fields_dict.health.$wrapper.html(`
			<div class="cc-dashboard">
				<div class="cc-stat"><div class="cc-stat-value">${health.total_files}</div><div class="cc-stat-label">${__("有效文件")}</div></div>
				<div class="cc-stat"><div class="cc-stat-value">${formatSize(health.total_size)}</div><div class="cc-stat-label">${__("空间占用")}</div></div>
				<div class="cc-stat"><div class="cc-stat-value">${health.trashed_files}</div><div class="cc-stat-label">${__("回收站")}</div></div>
				<div class="cc-stat"><div class="cc-stat-value">${health.broken_versions.length}</div><div class="cc-stat-label">${__("版本异常")}</div></div>
			</div>
			<p>${__("重复文件组")}：${health.duplicate_groups.length} · ${__("可清理过期文件")}：${health.eligible_trash.length} · ${__("保留天数")}：${health.settings.recycle_retention_days}</p>
			<div>${types || `<span class="text-muted">${__("暂无文件类型统计")}</span>`}</div>
			<hr><h6>${__("大文件排行")}</h6><ol>${largest || `<li>${__("暂无")}</li>`}</ol>
			<div class="mt-3"><button class="btn btn-danger btn-sm cc-health-purge">${__("清理过期回收站")}</button> <button class="btn btn-default btn-sm cc-health-settings">${__("打开设置")}</button></div>
		`);
		dialog.fields_dict.health.$wrapper.on("click", ".cc-health-purge", () => {
			frappe.confirm(__("确定彻底删除超过保留期限的回收站文件？"), async () => {
				const result = await frappe.call({method: "channel_erp.content_center.purge_expired_trash"});
				frappe.show_alert({message: __("已清理 {0} 个文件", [result.message.count]), indicator: "green"});
				dialog.hide();
				this.load();
			});
		});
		dialog.fields_dict.health.$wrapper.on("click", ".cc-health-settings", () => frappe.set_route("Form", "Channel Content Settings"));
		dialog.show();
	}

	async show_detail(asset) {
		const response = await frappe.call({method: "channel_erp.content_center.get_asset_detail", args: {asset}});
		const detail = response.message;
		let auditRows = Array.isArray(detail.audit) ? detail.audit : [];
		if (!Array.isArray(detail.audit)) {
			try {
				const auditResponse = await frappe.call({method: "channel_erp.content_center.get_content_audit", args: {asset, limit: 50}});
				auditRows = auditResponse.message || [];
			} catch (error) {
				auditRows = [];
			}
		}
		const escape = frappe.utils.escape_html;
		const locked = Boolean(detail.asset.is_locked);
		const workspaceAsset = this.data?.assets?.find((row) => row.name === asset);
		const assetPermissions = detail.asset.permissions || workspaceAsset?.permissions || {};
		const detailFeatures = {...this.featureFlags, ...(detail.feature_flags || {})};
		const accessPermissionKeys = ["can_request_borrow", "can_grant_access", "can_review_borrow", "can_revoke_access", "can_view_grants"];
		const hasAccessFeature = detailFeatures.borrowing && (Array.isArray(detail.access_grants) || accessPermissionKeys.some((key) => Object.prototype.hasOwnProperty.call(assetPermissions, key)));
		const grantsResponse = hasAccessFeature && !Array.isArray(detail.access_grants)
			? await frappe.call({method: "channel_erp.content_center.get_access_grants", args: {asset}})
			: null;
		const accessGrants = Array.isArray(detail.access_grants) ? detail.access_grants : (grantsResponse?.message || []);
		const hasPublicationStatus = detailFeatures.approval_workflow && Object.prototype.hasOwnProperty.call(detail.asset, "publication_status");
		const publicationStatus = detail.asset.publication_status || "Draft";
		let contentIndex = detail.index || detail.content_index || {};
		const hasInlineIndex = Object.prototype.hasOwnProperty.call(detail.asset, "index_status") ||
			Object.prototype.hasOwnProperty.call(detail.asset, "content_index_status") ||
			Boolean(detail.index || detail.content_index);
		if (Object.prototype.hasOwnProperty.call(assetPermissions, "can_rebuild_index")) {
			try {
				const indexResponse = await frappe.call({method: "channel_erp.content_center.get_index_status", args: {asset}});
				contentIndex = indexResponse.message || {};
			} catch (error) {
				contentIndex = detail.index || detail.content_index || {};
			}
		}
		const hasIndexFeature = hasInlineIndex || Boolean(Object.keys(contentIndex).length);
		const indexStatus = detail.asset.index_status || detail.asset.content_index_status || contentIndex.status || "Not Indexed";
		const indexReason = detail.asset.index_error || detail.asset.index_message || detail.asset.index_unsupported_reason || contentIndex.error_summary || contentIndex.error || contentIndex.message || contentIndex.reason;
		const canReindex = Boolean(assetPermissions.can_reindex || assetPermissions.can_rebuild_index || assetPermissions.can_manage);
		const insightPermissionKeys = ["can_view_insights", "can_generate_insights", "can_manage_insights", "can_accept_insights"];
		const hasInsightFeature = Boolean(detail.insights || detail.content_insights) || insightPermissionKeys.some((key) => Object.prototype.hasOwnProperty.call(assetPermissions, key));
		let insightData = detail.insights || detail.content_insights || null;
		let insightUnavailable = "";
		if (hasInsightFeature && !insightData && ["Ready", "Indexed"].includes(indexStatus)) {
			try {
				const insightResponse = await frappe.call({method: "channel_erp.content_center.get_content_insights", args: {asset}});
				insightData = insightResponse.message || null;
				if (insightData?.permissions) Object.assign(assetPermissions, insightData.permissions);
			} catch (error) {
				insightUnavailable = __("本地洞察服务暂不可用，文件详情仍可正常使用");
			}
		} else if (hasInsightFeature && ["Queued", "Pending"].includes(indexStatus)) {
			insightUnavailable = __("全文索引仍在处理中，完成后才能生成本地洞察");
		} else if (hasInsightFeature && indexStatus === "Unsupported") {
			insightUnavailable = indexReason || __("当前文件类型不支持文本提取，无法生成本地洞察");
		} else if (hasInsightFeature && indexStatus === "Failed") {
			insightUnavailable = indexReason || __("全文索引失败，请先修复或重新建立索引");
		} else if (hasInsightFeature && ["Missing", "Not Indexed"].includes(indexStatus)) {
			insightUnavailable = __("文件尚未建立全文索引，请先建立索引");
		}
		const sharePermissionKeys = ["can_share_external", "can_manage_external_shares", "can_create_share", "can_view_shares", "can_manage_shares", "can_revoke_share"];
		const hasShareFeature = Array.isArray(detail.share_links) || sharePermissionKeys.some((key) => Object.prototype.hasOwnProperty.call(assetPermissions, key));
		const canManageExternalShares = Boolean(assetPermissions.can_manage_external_shares || assetPermissions.can_view_shares || assetPermissions.can_manage_shares);
		const sharesResponse = hasShareFeature && canManageExternalShares && !Array.isArray(detail.share_links)
			? await frappe.call({method: "channel_erp.content_center.list_share_links", args: {asset}})
			: null;
		const shareLinks = Array.isArray(detail.share_links) ? detail.share_links : (sharesResponse?.message || []);
		const canEdit = Boolean(assetPermissions.can_upload) && !locked;
		const canLock = Boolean(assetPermissions.can_upload) && !locked;
		const canUnlock = locked && (
			detail.asset.locked_by === frappe.session.user ||
			frappe.session.user === "Administrator" ||
			(frappe.user_roles || []).some((role) => ["System Manager", "Content Center Manager"].includes(role))
		);
		let detailTagInput;
		const dialog = new frappe.ui.Dialog({
			title: detail.asset.title,
			size: "large",
			fields: [
				{fieldname: "title", fieldtype: "Data", label: __("标题"), default: detail.asset.title, reqd: 1, read_only: !canEdit},
				{fieldname: "tags", fieldtype: "HTML", label: __("标签")},
				{fieldname: "notes", fieldtype: "Small Text", label: __("备注"), default: detail.asset.notes, read_only: !canEdit},
				{fieldname: "governance_section", fieldtype: "Section Break", label: __("文件治理")},
				{fieldname: "responsible_user", fieldtype: "Link", options: "User", label: __("负责人"), default: detail.asset.responsible_user, read_only: !canEdit},
				{fieldname: "department", fieldtype: "Link", options: "Department", label: __("所属部门"), default: detail.asset.department, read_only: !canEdit},
				{fieldname: "governance_column", fieldtype: "Column Break"},
				{fieldname: "expires_on", fieldtype: "Date", label: __("到期日"), default: detail.asset.expires_on, read_only: !canEdit},
				{fieldname: "lock_status", fieldtype: "HTML"},
				...(detailFeatures.approval_workflow ? [
					{fieldname: "publication_section", fieldtype: "Section Break", label: __("审批与发布")},
					{fieldname: "publication_info", fieldtype: "HTML"},
				] : []),
				...(detailFeatures.borrowing ? [
					{fieldname: "access_section", fieldtype: "Section Break", label: __("限时授权与借阅")},
					{fieldname: "access_info", fieldtype: "HTML"},
				] : []),
				{fieldname: "share_section", fieldtype: "Section Break", label: __("外部安全分享")},
				{fieldname: "share_info", fieldtype: "HTML"},
				{fieldname: "index_section", fieldtype: "Section Break", label: __("全文索引")},
				{fieldname: "index_info", fieldtype: "HTML"},
				...(hasInsightFeature ? [
					{fieldname: "insight_section", fieldtype: "Section Break", label: __("本地智能洞察")},
					{fieldname: "insight_info", fieldtype: "HTML"},
				] : []),
				{fieldname: "history", fieldtype: "HTML"},
			],
			primary_action_label: __("保存"),
			primary_action: async (values) => {
				await frappe.call({method: "channel_erp.content_center.update_asset", args: {asset, ...values, tags: detailTagInput.value()}});
				dialog.hide();
				this.load();
			},
		});
		dialog.$wrapper.addClass("cc-detail-dialog");
		detailTagInput = new ChannelTagInput(dialog.fields_dict.tags.$wrapper, detail.asset.tags, !canEdit);
		const lockInfo = locked
			? `<div class="alert alert-warning mb-2"><strong>${__("文件已锁定")}</strong><br><span>${escape(this.lock_description(detail.asset))}</span></div>${canUnlock ? `<button class="btn btn-default btn-xs cc-unlock-asset">${frappe.utils.icon("unlock", "xs")} ${__("解锁文件")}</button>` : `<span class="text-muted">${__("只有锁定人或内容中心管理员可以解锁")}</span>`}`
			: `${canLock ? `<div class="text-muted mb-2">${__("锁定后仅允许预览和下载，不能修改、移动、复制、更新版本或回收。")}</div><button class="btn btn-default btn-xs cc-lock-asset">${frappe.utils.icon("lock", "xs")} ${__("锁定文件")}</button>` : ""}`;
		dialog.fields_dict.lock_status.$wrapper.html(lockInfo);
		const publicationLabels = {Draft: __("草稿"), Pending: __("待审批"), Published: __("已发布"), Rejected: __("已驳回")};
		const publicationActions = hasPublicationStatus && !locked ? [
			assetPermissions.can_submit && ["Draft", "Rejected"].includes(publicationStatus)
				? `<button class="btn btn-primary btn-xs cc-publication-action" data-publication-action="submit">${__("提交审核")}</button>` : "",
			assetPermissions.can_publish && publicationStatus === "Pending"
				? `<button class="btn btn-primary btn-xs cc-publication-action" data-publication-action="approve">${__("批准发布")}</button>` : "",
			assetPermissions.can_review && publicationStatus === "Pending"
				? `<button class="btn btn-default btn-xs cc-publication-action" data-publication-action="reject">${__("驳回")}</button>` : "",
			assetPermissions.can_publish && publicationStatus === "Published"
				? `<button class="btn btn-default btn-xs cc-publication-action" data-publication-action="unpublish">${__("取消发布")}</button>` : "",
		].filter(Boolean).join(" ") : "";
		const publicationInfo = hasPublicationStatus ? `
			<div class="mb-2"><span class="cc-status ${escape(publicationStatus.toLowerCase())}">${escape(publicationLabels[publicationStatus] || publicationStatus)}</span></div>
			<div class="text-muted small">
				${detail.asset.submitted_by ? `<div>${__("提交人")}：${escape(detail.asset.submitted_by)}${detail.asset.submitted_at ? ` · ${frappe.datetime.str_to_user(detail.asset.submitted_at)}` : ""}</div>` : ""}
				${detail.asset.reviewed_by ? `<div>${__("审核人")}：${escape(detail.asset.reviewed_by)}${detail.asset.reviewed_at ? ` · ${frappe.datetime.str_to_user(detail.asset.reviewed_at)}` : ""}</div>` : ""}
				${detail.asset.review_note ? `<div>${__("审核意见")}：${escape(detail.asset.review_note)}</div>` : ""}
			</div>
			<div class="cc-actions cc-publication-actions">${publicationActions}</div>
		` : `<span class="text-muted">${__("当前数据尚未启用审批发布信息")}</span>`;
		if (dialog.fields_dict.publication_info) dialog.fields_dict.publication_info.$wrapper.html(publicationInfo);
		const grantStatusLabels = {Pending: __("待审批"), Active: __("生效中"), Approved: __("已批准"), Rejected: __("已驳回"), Revoked: __("已撤销"), Expired: __("已过期")};
		const grantRows = accessGrants.map((grant) => {
			const grantPermissions = grant.permissions || {};
			const status = grant.effective_status || grant.status || "";
			const startsAt = grant.valid_from || grant.starts_at || grant.start_at;
			const endsAt = grant.valid_until || grant.expires_at || grant.end_at;
			const canApprove = status === "Pending" && Boolean(grantPermissions.can_approve || assetPermissions.can_review_borrow);
			const canReject = status === "Pending" && Boolean(grantPermissions.can_reject || assetPermissions.can_review_borrow);
			const canRevoke = ["Active", "Approved"].includes(status) && Boolean(grantPermissions.can_revoke || assetPermissions.can_revoke_access);
			return `<li class="cc-relation-row">
				<span><strong>${escape(grant.grantee || grant.user || grant.requested_by || __("未知用户"))}</strong> · ${escape(grantStatusLabels[status] || status || __("未知状态"))}<br>
				<span class="text-muted">${startsAt ? frappe.datetime.str_to_user(startsAt) : "-"} → ${endsAt ? frappe.datetime.str_to_user(endsAt) : "-"} · ${grant.can_download ? __("允许下载") : __("仅预览")}${grant.request_reason ? ` · ${escape(grant.request_reason)}` : ""}${grant.review_note ? ` · ${escape(grant.review_note)}` : ""}</span></span>
				<span>${canApprove ? `<button class="btn btn-primary btn-xs cc-grant-action" data-grant-action="approve" data-grant="${escape(grant.name)}">${__("批准")}</button>` : ""} ${canReject ? `<button class="btn btn-default btn-xs cc-grant-action" data-grant-action="reject" data-grant="${escape(grant.name)}">${__("驳回")}</button>` : ""} ${canRevoke ? `<button class="btn btn-default btn-xs cc-grant-action" data-grant-action="revoke" data-grant="${escape(grant.name)}">${__("撤销")}</button>` : ""}</span>
			</li>`;
		}).join("");
		const accessActions = hasAccessFeature && !locked ? [
			assetPermissions.can_request_borrow ? `<button class="btn btn-default btn-xs cc-access-action" data-access-action="request">${__("申请借阅")}</button>` : "",
			assetPermissions.can_grant_access ? `<button class="btn btn-default btn-xs cc-access-action" data-access-action="grant">${__("直接授权")}</button>` : "",
		].filter(Boolean).join(" ") : "";
		if (dialog.fields_dict.access_info) dialog.fields_dict.access_info.$wrapper.html(hasAccessFeature ? `<div class="cc-actions">${accessActions}</div><h6 class="mt-3">${__("授权记录")}</h6><ul class="cc-activity">${grantRows || `<li>${__("暂无授权记录")}</li>`}</ul>` : `<span class="text-muted">${__("当前数据尚未启用限时授权功能")}</span>`);
		const canCreateShare = hasShareFeature &&
			Boolean(assetPermissions.can_share_external || assetPermissions.can_create_share) &&
			detail.asset.status === "Active" &&
			(!detailFeatures.approval_workflow || publicationStatus === "Published") &&
			!locked;
		const shareStatusLabels = {Active: __("有效"), Revoked: __("已撤销"), Expired: __("已过期"), Exhausted: __("次数已用完")};
		const shareRows = shareLinks.map((share) => {
			const status = share.effective_status || share.status || "";
			const maskedToken = share.masked_token || share.token_mask || share.token_preview || "••••••";
			const downloads = share.download_count ?? share.downloads ?? 0;
			const maxDownloads = share.max_downloads ?? share.max_download_count;
			const canRevoke = status === "Active" && Boolean(share.permissions?.can_revoke || assetPermissions.can_revoke_share || assetPermissions.can_manage_shares || assetPermissions.can_manage_external_shares);
			return `<li class="cc-relation-row">
				<span><strong>${escape(maskedToken)}</strong> · ${escape(shareStatusLabels[status] || status || __("未知状态"))}<br>
				<span class="text-muted">${__("有效期至")}：${share.expires_at ? frappe.datetime.str_to_user(share.expires_at) : __("未设置")} · ${__("下载")} ${downloads}/${maxDownloads || __("不限")} · ${share.allow_preview ? __("可预览") : __("不可预览")} · ${share.allow_download ? __("可下载") : __("不可下载")}${share.watermark_text ? ` · ${__("水印")}：${escape(share.watermark_text)}` : ""}</span></span>
				${canRevoke ? `<button class="btn btn-default btn-xs cc-share-revoke" data-share="${escape(share.name)}">${__("撤销")}</button>` : ""}
			</li>`;
		}).join("");
		const shareCreateAction = canCreateShare ? `<button class="btn btn-default btn-xs cc-share-create">${__("创建外部分享")}</button>` : "";
		dialog.fields_dict.share_info.$wrapper.html(hasShareFeature ? `<div class="cc-actions">${shareCreateAction}</div><h6 class="mt-3">${__("分享记录")}</h6><ul class="cc-activity">${shareRows || `<li>${__("暂无外部分享")}</li>`}</ul><p class="text-muted small">${__("出于安全考虑，完整分享地址仅在创建成功时显示一次，访问密码不会回显。")}</p>` : `<span class="text-muted">${__("当前数据尚未启用外部安全分享")}</span>`);
		const indexLabels = {Ready: __("已索引"), Indexed: __("已索引"), Queued: __("等待索引"), Pending: __("等待索引"), Failed: __("索引失败"), Unsupported: __("不支持"), Missing: __("未索引"), "Not Indexed": __("未索引")};
		const indexedCharacters = detail.asset.indexed_characters ?? detail.asset.index_character_count ?? contentIndex.char_count ?? contentIndex.character_count ?? contentIndex.characters;
		const indexedAt = detail.asset.indexed_at || detail.asset.indexed_on || contentIndex.indexed_at;
		dialog.fields_dict.index_info.$wrapper.html(hasIndexFeature ? `
			<div><span class="cc-index-status">${escape(indexLabels[indexStatus] || indexStatus)}</span></div>
			<div class="text-muted small mt-2">${__("索引字符数")}：${indexedCharacters ?? 0}${indexedAt ? ` · ${__("索引时间")}：${frappe.datetime.str_to_user(indexedAt)}` : ""}</div>
			${indexReason ? `<div class="alert alert-warning mt-2 mb-2">${escape(indexReason)}</div>` : ""}
			${canReindex ? `<button class="btn btn-default btn-xs cc-reindex-asset">${__("重新建立索引")}</button>` : ""}
		` : `<span class="text-muted">${__("当前文件尚未提供全文索引信息")}</span>`);
		if (hasInsightFeature) {
			const insight = insightData?.insight || insightData || {};
			const suggestionSource = insightData?.suggestions || insight.suggestions || [];
			const suggestions = Array.isArray(suggestionSource) ? suggestionSource : [];
			const keywordsRaw = insight.keywords || insight.keyword_list || [];
			let keywords = Array.isArray(keywordsRaw) ? keywordsRaw : [];
			if (!keywords.length && typeof keywordsRaw === "string") {
				try {
					const parsed = JSON.parse(keywordsRaw);
					keywords = Array.isArray(parsed) ? parsed : [];
				} catch (error) {
					keywords = keywordsRaw.split(/[,，]/).map((keyword) => keyword.trim()).filter(Boolean);
				}
			}
			const canGenerateInsights = Boolean(assetPermissions.can_generate_insights || assetPermissions.can_manage_insights) &&
				["Ready", "Indexed"].includes(indexStatus) && !["Waiting", "Pending", "Processing"].includes(insight.status);
			const insightStateMessage = insightUnavailable || (
				["Waiting", "Pending", "Processing"].includes(insight.status) ? __("本地分析正在处理中，请稍后刷新") :
				insight.status === "Failed" ? (insight.error_summary || __("本地分析失败，可重新分析")) :
				insight.status === "Disabled" ? (insight.error_summary || __("当前文件不支持本地分析")) : ""
			);
			const suggestionCards = suggestions.map((suggestion) => {
				const status = suggestion.status || "Pending";
				const suggestionPermissions = suggestion.permissions || {};
				const pending = status === "Pending";
				const canAccept = pending && suggestion.name && Boolean(suggestionPermissions.can_accept || assetPermissions.can_accept_insights || assetPermissions.can_manage_insights);
				const canDismiss = pending && suggestion.name && Boolean(suggestionPermissions.can_dismiss || assetPermissions.can_accept_insights || assetPermissions.can_manage_insights);
				const type = suggestion.suggestion_type || suggestion.type || "";
				const value = suggestion.suggested_value || suggestion.value || suggestion.tag || [suggestion.reference_doctype, suggestion.reference_name].filter(Boolean).join(" · ");
				const confidenceValue = Number(suggestion.confidence);
				const confidence = Number.isFinite(confidenceValue) ? (confidenceValue <= 1 ? `${Math.round(confidenceValue * 100)}%` : `${Math.round(confidenceValue)}%`) : __("未评分");
				return `<div class="cc-search-result mb-2">
					<div class="cc-search-result-heading"><strong>${__("建议标签")}</strong><span class="cc-index-status">${escape(confidence)}</span></div>
					<div class="mt-1">${escape(value || __("未提供建议内容"))}</div>
					${suggestion.reason ? `<div class="text-muted small mt-1">${__("建议理由")}：${escape(suggestion.reason)}</div>` : ""}
					<div class="cc-actions">${canAccept ? `<button class="btn btn-primary btn-xs cc-insight-suggestion" data-insight-action="accept" data-suggestion="${escape(suggestion.name)}">${__("接受")}</button>` : ""}${canDismiss ? `<button class="btn btn-default btn-xs cc-insight-suggestion" data-insight-action="dismiss" data-suggestion="${escape(suggestion.name)}">${__("忽略")}</button>` : ""}${!pending ? `<span class="text-muted small">${escape(status)}</span>` : ""}</div>
				</div>`;
			}).join("");
			dialog.fields_dict.insight_info.$wrapper.html(`
				<div class="mb-2"><span class="cc-status published">${insight.status === "Ready" || insight.summary ? __("本地生成") : __("本地分析")}</span>${insight.generator_version || insight.version ? ` <span class="text-muted small">v${escape(insight.generator_version || insight.version)}</span>` : ""}${insight.generated_at ? ` <span class="text-muted small">· ${frappe.datetime.str_to_user(insight.generated_at)}</span>` : ""}</div>
				${insightStateMessage ? `<div class="alert alert-warning">${escape(insightStateMessage)}</div>` : ""}
				${insight.summary ? `<h6>${__("智能摘要")}</h6><p>${escape(insight.summary)}</p>` : (!insightStateMessage ? `<p class="text-muted">${__("尚未生成本地智能摘要")}</p>` : "")}
				${keywords.length ? `<h6>${__("关键词")}</h6><div class="cc-tags">${keywords.map((keyword) => {
					const label = typeof keyword === "object" ? keyword.value : keyword;
					const count = typeof keyword === "object" ? keyword.count : null;
					return `<span class="cc-tag">${escape(label || "")}${count ? ` · ${escape(String(count))}` : ""}</span>`;
				}).join("")}</div>` : ""}
				${suggestionCards ? `<h6 class="mt-3">${__("待确认建议")}</h6>${suggestionCards}` : ""}
				${canGenerateInsights ? `<button class="btn btn-default btn-xs cc-generate-insights">${insight.summary ? __("重新分析") : __("生成本地洞察")}</button>` : ""}
				<p class="text-muted small mt-2">${__("以上内容由本系统在本地根据文件索引生成，建议不会自动应用。")}</p>
			`);
		}
		const versions = detail.versions.map((version) => `<li>v${version.version_number} · ${escape(version.original_filename)} · ${frappe.datetime.str_to_user(version.creation)} ${version.name === detail.asset.current_version ? `· <strong>${__("当前版本")}</strong>` : (canEdit ? `· <button class="btn btn-link btn-xs cc-set-version" data-version="${escape(version.name)}">${__("设为当前")}</button>` : "")}</li>`).join("");
		const activity = (detail.activity || []).map((row) => `<li><strong>${escape(row.label)}</strong><br><span class="text-muted">${frappe.datetime.str_to_user(row.timestamp)}${row.user ? ` · ${escape(row.user)}` : ""}</span></li>`).join("");
		const auditActions = {
			"metadata-update": __("修改文件信息"),
			"bulk-governance": __("批量治理"),
			lock: __("锁定文件"),
			unlock: __("解锁文件"),
			rename: __("重命名"),
			move: __("移动文件"),
			trash: __("移入回收站"),
			restore: __("恢复文件"),
			"submit-review": __("提交审核"),
			"submit-for-review": __("提交审核"),
			approve: __("批准发布"),
			reject: __("驳回文件"),
			unpublish: __("取消发布"),
			"borrow-request": __("申请借阅"),
			"borrow-approve": __("批准借阅"),
			"borrow-reject": __("驳回借阅"),
			"access-grant": __("直接授权"),
			"access-revoke": __("撤销授权"),
			"share-create": __("创建外部分享"),
			"share-revoke": __("撤销外部分享"),
		};
		const audit = auditRows.map((row) => {
			const details = row.details && typeof row.details === "object"
				? Object.entries(row.details).map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(", ") : (value ?? "")}`).join(" · ")
				: (row.details || "");
			const reference = row.reference_doctype && row.reference_name
				? ` · ${frappe.utils.get_form_link(row.reference_doctype, row.reference_name, true)}`
				: "";
			return `<li><strong>${escape(auditActions[row.action] || row.action || __("文件操作"))}</strong>${reference}<br><span class="text-muted">${frappe.datetime.str_to_user(row.creation)}${row.user ? ` · ${escape(row.user)}` : ""}${details ? ` · ${escape(details)}` : ""}</span></li>`;
		}).join("");
		dialog.fields_dict.history.$wrapper.html(`<hr><h6>${__("版本历史")}</h6><ul>${versions || `<li>${__("暂无")}</li>`}</ul><h6>${__("操作记录")}</h6><ul class="cc-activity">${activity}</ul><h6 class="mt-4">${__("治理审计")}</h6><ul class="cc-activity">${audit || `<li>${__("暂无审计记录")}</li>`}</ul>${canEdit ? `<button class="btn btn-xs btn-default cc-add-version">${__("上传新版本")}</button>` : ""}`);
		dialog.fields_dict.lock_status.$wrapper.on("click", ".cc-lock-asset,.cc-unlock-asset", async (event) => {
			const method = $(event.currentTarget).hasClass("cc-lock-asset") ? "lock_asset" : "unlock_asset";
			await frappe.call({method: `channel_erp.content_center.${method}`, args: {asset}});
			dialog.hide();
			frappe.show_alert({message: method === "lock_asset" ? __("文件已锁定") : __("文件已解锁"), indicator: "green"});
			this.load();
		});
		if (dialog.fields_dict.publication_info) {
			dialog.fields_dict.publication_info.$wrapper.on("click", ".cc-publication-action", (event) => {
				this.run_publication_action(asset, $(event.currentTarget).data("publication-action"), dialog);
			});
		}
		if (dialog.fields_dict.access_info) {
			dialog.fields_dict.access_info.$wrapper.on("click", ".cc-access-action", (event) => {
				this.open_access_dialog(asset, $(event.currentTarget).data("access-action"), dialog);
			});
			dialog.fields_dict.access_info.$wrapper.on("click", ".cc-grant-action", (event) => {
				const button = $(event.currentTarget);
				this.review_access_grant(asset, button.data("grant"), button.data("grant-action"), dialog);
			});
		}
		dialog.fields_dict.share_info.$wrapper.on("click", ".cc-share-create", () => this.open_share_dialog(asset, dialog));
		dialog.fields_dict.share_info.$wrapper.on("click", ".cc-share-revoke", (event) => {
			this.revoke_share_link(asset, $(event.currentTarget).data("share"), dialog);
		});
		dialog.fields_dict.index_info.$wrapper.on("click", ".cc-reindex-asset", () => this.reindex_content_asset(asset, dialog));
		if (hasInsightFeature) {
			dialog.fields_dict.insight_info.$wrapper.on("click", ".cc-generate-insights", () => this.generate_content_insights(asset, dialog));
			dialog.fields_dict.insight_info.$wrapper.on("click", ".cc-insight-suggestion", (event) => {
				const button = $(event.currentTarget);
				this.handle_content_suggestion(asset, button.data("suggestion"), button.data("insight-action"), dialog);
			});
		}
		dialog.fields_dict.history.$wrapper.on("click", ".cc-add-version", () => this.upload_files(asset));
		dialog.fields_dict.history.$wrapper.on("click", ".cc-set-version", async (event) => {
			await frappe.call({method: "channel_erp.content_center.set_current_version", args: {asset, version: $(event.currentTarget).data("version")}});
			dialog.hide();
			frappe.show_alert({message: __("当前版本已切换"), indicator: "green"});
			this.load();
		});
		dialog.show();
		if (!canEdit) dialog.get_primary_btn().hide();
	}

	run_publication_action(asset, action, parentDialog) {
		const config = {
			submit: {method: "submit_for_review", title: __("提交审核"), label: __("提交说明"), success: __("文件已提交审核")},
			approve: {method: "approve_asset", title: __("批准发布"), label: __("审核意见"), success: __("文件已批准并发布"), confirm: __("确认批准并发布此文件？"), required: true},
			reject: {method: "reject_asset", title: __("驳回文件"), label: __("驳回意见"), success: __("文件已驳回"), confirm: __("确认驳回此文件？"), required: true},
			unpublish: {method: "unpublish_asset", title: __("取消发布"), label: __("取消发布说明"), success: __("文件已取消发布"), confirm: __("确认将此文件退回草稿状态？")},
		}[action];
		if (!config) return;
		frappe.prompt(
			{fieldname: "comment", fieldtype: "Small Text", label: config.label, reqd: Boolean(config.required)},
			(values) => {
				const execute = async () => {
					await frappe.call({method: `channel_erp.content_center.${config.method}`, args: {asset, comment: values.comment || ""}});
					parentDialog?.hide();
					frappe.show_alert({message: config.success, indicator: "green"});
					this.load();
				};
				if (config.confirm) return frappe.confirm(config.confirm, execute);
				return execute();
			},
			config.title,
			__("继续")
		);
	}

	open_access_dialog(asset, action, parentDialog) {
		if (!["request", "grant"].includes(action)) return;
		const directGrant = action === "grant";
		const now = frappe.datetime.now_datetime();
		const defaultEnd = frappe.datetime.add_days(now, 7);
		const fields = [
				directGrant ? {fieldname: "grantee", fieldtype: "Link", options: "User", label: __("授权用户"), reqd: 1} : null,
				{fieldname: "valid_from", fieldtype: "Datetime", label: __("开始时间"), default: now, reqd: 1},
				{fieldname: "valid_until", fieldtype: "Datetime", label: __("结束时间"), default: defaultEnd, reqd: 1},
				{fieldname: "can_download", fieldtype: "Check", label: __("允许下载"), default: 0},
				{fieldname: directGrant ? "review_note" : "request_reason", fieldtype: "Small Text", label: directGrant ? __("授权备注") : __("申请理由"), reqd: !directGrant},
		].filter(Boolean);
		const dialog = new frappe.ui.Dialog({
			title: directGrant ? __("直接授权文件访问") : __("申请借阅文件"),
			fields,
			primary_action_label: directGrant ? __("确认授权") : __("提交申请"),
			primary_action: async (values) => {
				if (new Date(values.valid_until) <= new Date(values.valid_from)) {
					return frappe.msgprint(__("结束时间必须晚于开始时间"));
				}
				const method = directGrant ? "grant_asset_access" : "request_asset_borrow";
				await frappe.call({method: `channel_erp.content_center.${method}`, args: {asset, ...values}});
				dialog.hide();
				parentDialog?.hide();
				frappe.show_alert({message: directGrant ? __("限时授权已创建") : __("借阅申请已提交"), indicator: "green"});
				this.load();
			},
		});
		dialog.show();
	}

	review_access_grant(asset, grant, action, parentDialog) {
		const config = {
			approve: {method: "approve_borrow_request", title: __("批准借阅"), label: __("审核备注"), confirm: __("确认批准此借阅申请？"), success: __("借阅申请已批准")},
			reject: {method: "reject_borrow_request", title: __("驳回借阅"), label: __("驳回原因"), confirm: __("确认驳回此借阅申请？"), success: __("借阅申请已驳回"), required: true},
			revoke: {method: "revoke_asset_access", title: __("撤销授权"), label: __("撤销备注"), confirm: __("确认立即撤销此用户的文件访问权限？"), success: __("文件授权已撤销")},
		}[action];
		if (!config) return;
		frappe.prompt(
			{fieldname: "review_note", fieldtype: "Small Text", label: config.label, reqd: Boolean(config.required)},
			(values) => frappe.confirm(config.confirm, async () => {
				await frappe.call({method: `channel_erp.content_center.${config.method}`, args: {grant, review_note: values.review_note || ""}});
				parentDialog?.hide();
				frappe.show_alert({message: config.success, indicator: "green"});
				this.load();
			}),
			config.title,
			__("继续")
		);
	}

	open_share_dialog(asset, parentDialog) {
		const now = frappe.datetime.now_datetime();
		const dialog = new frappe.ui.Dialog({
			title: __("创建外部安全分享"),
			fields: [
				{fieldname: "expires_at", fieldtype: "Datetime", label: __("有效期至"), default: frappe.datetime.add_days(now, 7), reqd: 1},
				{fieldname: "allow_preview", fieldtype: "Check", label: __("允许预览"), default: 1},
				{fieldname: "allow_download", fieldtype: "Check", label: __("允许下载"), default: 0},
				{fieldname: "max_downloads", fieldtype: "Int", label: __("最大下载次数"), default: 0, description: __("0 表示不限制下载次数")},
				{fieldname: "password", fieldtype: "Password", label: __("访问密码"), description: __("可选；创建后不会回显")},
				{fieldname: "watermark_text", fieldtype: "Small Text", label: __("水印文本"), description: __("预览或下载时由服务端按策略应用")},
			],
			primary_action_label: __("创建分享"),
			primary_action: async (values) => {
				if (!values.allow_preview && !values.allow_download) {
					return frappe.msgprint(__("预览和下载至少需要允许一项"));
				}
				if ((values.max_downloads || 0) < 0) return frappe.msgprint(__("最大下载次数不能小于 0"));
				const response = await frappe.call({method: "channel_erp.content_center.create_share_link", args: {asset, ...values}});
				const shareUrl = response.message?.share_url || response.message?.url;
				dialog.hide();
				parentDialog?.hide();
				if (shareUrl) this.show_created_share_url(shareUrl);
				else frappe.msgprint(__("分享已创建，但服务端未返回完整分享地址"));
				this.load();
			},
		});
		dialog.show();
	}

	show_created_share_url(shareUrl) {
		const dialog = new frappe.ui.Dialog({
			title: __("外部分享已创建"),
			fields: [{fieldname: "share_url", fieldtype: "HTML"}],
			primary_action_label: __("关闭"),
			primary_action: () => dialog.hide(),
		});
		dialog.fields_dict.share_url.$wrapper.html(`
			<div class="alert alert-warning">${__("请立即保存此地址。关闭窗口后，系统将不再显示完整分享地址。访问密码也不会被保存到页面中。")}</div>
			<div class="input-group"><input class="form-control cc-created-share-url" type="text" readonly><div class="input-group-append"><button class="btn btn-default cc-copy-share-url">${__("复制")}</button></div></div>
		`);
		dialog.fields_dict.share_url.$wrapper.find(".cc-created-share-url").val(shareUrl);
		dialog.fields_dict.share_url.$wrapper.on("click", ".cc-copy-share-url", async () => {
			const input = dialog.fields_dict.share_url.$wrapper.find(".cc-created-share-url").get(0);
			try {
				await navigator.clipboard.writeText(shareUrl);
			} catch (error) {
				if (frappe.utils.copy_to_clipboard) frappe.utils.copy_to_clipboard(shareUrl);
				else {
					input.select();
					document.execCommand("copy");
				}
			}
			frappe.show_alert({message: __("分享地址已复制"), indicator: "green"});
		});
		dialog.show();
	}

	revoke_share_link(asset, share, parentDialog) {
		frappe.confirm(__("撤销后外部访问将立即失效，确认继续？"), async () => {
			await frappe.call({method: "channel_erp.content_center.revoke_share_link", args: {share}});
			parentDialog?.hide();
			frappe.show_alert({message: __("外部分享已撤销"), indicator: "green"});
			this.load();
		});
	}

	reindex_content_asset(asset, parentDialog) {
		frappe.confirm(__("确认重新建立此文件的全文索引？"), async () => {
			await frappe.call({method: "channel_erp.content_center.rebuild_content_index", args: {asset}});
			parentDialog?.hide();
			frappe.show_alert({message: __("文件已加入索引重建队列"), indicator: "green"});
			this.load();
		});
	}

	rebuild_content_index() {
		const canRebuild = (frappe.session.user === "Administrator" || (frappe.user_roles || []).some((role) => ["System Manager", "Content Center Manager"].includes(role))) &&
			Boolean(this.data?.permissions?.can_rebuild_index || this.data?.assets?.some((asset) => Object.prototype.hasOwnProperty.call(asset.permissions || {}, "can_rebuild_index")) || this.data?.indexing_enabled);
		if (!canRebuild) {
			return frappe.msgprint(__("没有批量重建索引的权限，或索引服务尚未启用"));
		}
		frappe.confirm(__("将为可索引文件批量重建全文索引，确认继续？"), async () => {
			const response = await frappe.call({
				method: "channel_erp.content_center.rebuild_content_index",
			});
			const count = response.message?.count ?? response.message?.queued;
			frappe.show_alert({message: count == null ? __("批量索引任务已提交") : __("已提交 {0} 个文件的索引任务", [count]), indicator: "green"});
		});
	}

	generate_content_insights(asset, parentDialog) {
		frappe.confirm(__("确认使用当前文件索引重新进行本地分析？已有未确认建议可能会更新。"), async () => {
			await frappe.call({method: "channel_erp.content_center.generate_content_insights", args: {asset}});
			parentDialog?.hide();
			frappe.show_alert({message: __("本地分析任务已提交"), indicator: "green"});
			await this.load();
			this.show_detail(asset);
		});
	}

	handle_content_suggestion(asset, suggestion, action, parentDialog) {
		const accepting = action === "accept";
		if (!accepting && action !== "dismiss") return;
		const method = accepting ? "accept_content_suggestion" : "dismiss_content_suggestion";
		const message = accepting
			? __("确认接受此建议？系统将更新文件标签，并写入审计记录。")
			: __("确认忽略此建议？该建议将保留为已忽略状态。");
		frappe.confirm(message, async () => {
			await frappe.call({method: `channel_erp.content_center.${method}`, args: {suggestion}});
			parentDialog?.hide();
			frappe.show_alert({message: accepting ? __("建议已接受并应用") : __("建议已忽略"), indicator: "green"});
			await this.load();
			this.show_detail(asset);
		});
	}

}
