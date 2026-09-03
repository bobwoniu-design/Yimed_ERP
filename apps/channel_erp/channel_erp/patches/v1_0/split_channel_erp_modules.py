from channel_erp.setup import apply_module_assignments, ensure_module_defs, refresh_module_map


def execute():
	"""Prepare module metadata before Frappe imports controllers from their new paths."""
	ensure_module_defs()
	apply_module_assignments()
	refresh_module_map()
