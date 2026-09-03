frappe.listview_settings["JD Purchase Order"] = {
	add_fields: ["status", "mapping_status", "workflow_stage"],

	get_indicator(doc) {
		const indicators = {
			待映射: [__("待映射"), "gray", "status,=,待映射"],
			待备货: [__("待备货"), "blue", "status,=,待备货"],
			装箱中: [__("装箱中"), "orange", "status,=,装箱中"],
			已装箱: [__("已装箱"), "blue", "status,=,已装箱"],
			待交接: [__("待交接"), "orange", "status,=,待交接"],
			已发货: [__("已发货"), "green", "status,=,已发货"],
			已完成: [__("已完成"), "green", "status,=,已完成"],
			已取消: [__("已取消"), "red", "status,=,已取消"],
		};
		return indicators[doc.status] || [doc.status || __("未设置"), "gray"];
	},
};
