(() => {
	const SIDEBAR = "JD Self Operated";
	const ECOMMERCE_SIDEBAR = "Ecommerce Dashboard";
	const ROUTE_OWNERSHIP = Object.freeze({
		pages: new Set(["jd-purchase-workbench", "jd-packing-workbench"]),
		doctypes: new Set([
			"JD Purchase Import Batch",
			"JD Purchase Order",
			"JD Carton",
			"JD Handover",
			"JD Sorting Allocation",
			"JD Stocking Pool Item",
			"JD SKU Mapping",
			"JD Self Operated Settings",
		]),
		reports: new Set(["JD Stocking Summary", "JD Warehouse Summary"]),
	});

	const normalize = (value) => {
		try {
			return decodeURIComponent(String(value || ""));
		} catch (error) {
			return String(value || "");
		}
	};

	const owns_route = (route) => {
		const parts = Array.isArray(route) ? route.map(normalize) : [];
		if (!parts.length) return false;
		if (parts.length === 1) return ROUTE_OWNERSHIP.pages.has(parts[0]);

		const view = parts[0].toLowerCase();
		const entity = parts[1];
		if (["form", "list", "tree", "calendar", "image", "kanban", "gantt"].includes(view)) {
			return ROUTE_OWNERSHIP.doctypes.has(entity);
		}
		if (["query-report", "report"].includes(view)) {
			return ROUTE_OWNERSHIP.reports.has(entity);
		}
		return false;
	};
	const sidebar_available = () => Boolean(frappe.boot?.workspace_sidebar_item?.[SIDEBAR.toLowerCase()]);
	const ecommerce_owns_route = (route) => {
		const parts = Array.isArray(route) ? route.map(normalize) : [];
		if (!parts.length) return false;
		if (parts.length === 1) {
			return new Set(["ecommerce-business-dashboard", "ecommerce-promotion-dashboard"]).has(parts[0]);
		}
		const view = parts[0].toLowerCase();
		return ["form", "list", "tree", "calendar", "image", "kanban", "gantt"].includes(view)
			&& new Set([
				"Ecommerce Fee Item", "Ecommerce Fee Rule", "Ecommerce Fee Detail",
				"Ecommerce Product Listing", "Ecommerce Promotion Import Batch",
				"Ecommerce Promotion Expense", "Ecommerce Promotion Performance",
			]).has(parts[1]);
	};
	const ecommerce_sidebar_available = () => Boolean(
		frappe.boot?.workspace_sidebar_item?.[ECOMMERCE_SIDEBAR.toLowerCase()],
	);
	const resolve_owned_sidebar = (route) => {
		if (sidebar_available() && owns_route(route)) return SIDEBAR;
		if (ecommerce_sidebar_available() && ecommerce_owns_route(route)) return ECOMMERCE_SIDEBAR;
		return null;
	};

	frappe.provide("yimed_ecommerce.jd_sidebar_routes");
	Object.assign(yimed_ecommerce.jd_sidebar_routes, { SIDEBAR, ROUTE_OWNERSHIP, owns_route, sidebar_available });

	const install = (attempt = 0) => {
		const Sidebar = frappe.ui?.Sidebar;
		if (!Sidebar?.prototype?.resolve_sidebar) {
			if (attempt < 20) window.setTimeout(() => install(attempt + 1), 50);
			return;
		}

		const prototype = Sidebar.prototype;
		if (!prototype.__jd_route_ownership_installed) {
			const original_resolve_sidebar = prototype.resolve_sidebar;
			prototype.resolve_sidebar = function (entity, module) {
				const owned_sidebar = resolve_owned_sidebar(frappe.get_route());
				if (owned_sidebar) {
					this.preferred_sidebars = [owned_sidebar];
					return owned_sidebar;
				}
				return original_resolve_sidebar.call(this, entity, module);
			};
			Object.defineProperty(prototype, "__jd_route_ownership_installed", { value: true });
		}

		if (resolve_owned_sidebar(frappe.get_route())) {
			frappe.app?.sidebar?.set_workspace_sidebar(frappe.router);
		}
	};

	install();
})();
