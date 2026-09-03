from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate

from yimed_ecommerce.ecommerce_dashboard.fee_management import (
	_allocate_amount as _allocate_rounded_amount,
	_weights_for,
)


@frappe.whitelist()
def get_filter_options(company: str | None = None):
	_check_dashboard_permission()
	companies = frappe.get_all("Company", pluck="name", order_by="name")
	channels = []
	stores = []
	if _channel_available():
		filters = _active_sales_channel_filters()
		rows = frappe.get_all(
			"Jackyun Sales Channel",
			filters=filters,
			fields=["online_platform_name", "channel_name"],
			order_by="online_platform_name, channel_name",
		)
		channels = sorted({row.online_platform_name for row in rows if row.online_platform_name})
		stores = [
			{"channel": channel, "store": store}
			for channel, store in sorted(
				{(row.online_platform_name, row.channel_name) for row in rows if row.online_platform_name and row.channel_name}
			)
		]
	recommended_company = frappe.db.get_value(
		"Sales Order",
		{"docstatus": 1, "custom_jackyun_sales_channel": ["is", "set"]},
		"company",
		order_by="transaction_date desc, modified desc",
	) if frappe.db.has_column("Sales Order", "custom_jackyun_sales_channel") else None
	return {"companies": companies, "channels": channels, "stores": stores, "recommended_company": recommended_company}


@frappe.whitelist()
def get_dashboard(
	company: str | None = None,
	from_date: str | None = None,
	to_date: str | None = None,
	channel_name: str | None = None,
	store_name: str | None = None,
	listing_dimension: str | None = None,
	mode: str | None = None,
):
	_check_dashboard_permission()
	if not from_date or not to_date:
		frappe.throw(_("From Date and To Date are required."))
	start = getdate(from_date)
	end = getdate(to_date)
	if start > end:
		frappe.throw(_("From Date cannot be later than To Date."))
	if end >= getdate(frappe.utils.nowdate()):
		frappe.throw(_("To Date must be earlier than today; today's real-time data belongs to the realtime sales dashboard."))
	if (end - start).days > 730:
		frappe.throw(_("The dashboard date range cannot exceed two years."))
	company = frappe.utils.cstr(company).strip()
	if company and not frappe.db.exists("Company", company):
		frappe.throw(_("Company does not exist."))
	days = (end - start).days + 1
	companies = (
		[company]
		if company
		else _get_active_dashboard_companies(add_days(start, -days), end)
	)

	listing_dimension = frappe.utils.cstr(listing_dimension or "main").strip().lower()
	if listing_dimension not in {"main", "child"}:
		frappe.throw(_("Listing dimension must be main or child."))
	mode = frappe.utils.cstr(mode or "estimate").strip().lower()
	if mode not in {"estimate", "settle"}:
		frappe.throw(_("Mode must be estimate or settle."))
	filters = {
		"company": company,
		"from_date": start,
		"to_date": end,
		"channel_name": channel_name or "",
		"store_name": store_name or "",
		"listing_dimension": listing_dimension,
	}
	current = _build_periods_for_companies(filters, companies, mode=mode)
	period_rows = current.pop("_detail_rows")
	listing_rows = []
	for company_name in companies:
		company_filters = {**filters, "company": company_name}
		company_period_rows = [row for row in period_rows if row.get("company") == company_name]
		listing_rows.extend(_listing_performance(company_filters, company_period_rows, listing_dimension, mode))
	current["listings"] = _aggregate_listing_period(listing_rows)
	listing_sales = sum(row["sales_amount"] for row in current["listings"])
	identified_listing_sales = sum(
		row["sales_amount"] for row in current["listings"] if row["listing_id"] != "未识别"
	)
	current["data_quality"]["listing_coverage_pct"] = (
		identified_listing_sales / listing_sales * 100 if listing_sales else 0
	)
	current["data_quality"]["unidentified_listing_sales"] = listing_sales - identified_listing_sales
	previous_filters = dict(filters)
	previous_filters["to_date"] = add_days(start, -1)
	previous_filters["from_date"] = add_days(start, -days)
	previous = _build_periods_for_companies(previous_filters, companies, include_rows=False, mode=mode)
	previous.pop("_detail_rows", None)
	current["previous"] = previous["summary"]
	current["filters"] = filters
	current["fee_columns"] = _dashboard_fee_columns()
	current["source"] = {
		"sales": "Submitted and draft Sales Orders (cancelled excluded) by transaction date",
		"returns": "Submitted return Delivery Notes by posting date",
		"cost": "Current ERPNext warehouse valuation rate; coverage is shown separately",
		"promotion": "Estimated by Ecommerce Fee Estimate Rule" if mode == "estimate" else "Imported Ecommerce Promotion Expense rows",
		"fees": "Estimated by Ecommerce Fee Estimate Rule" if mode == "estimate" else "Effective Ecommerce Fee Rules; store overrides channel, channel overrides global",
	}
	return current


def _get_active_dashboard_companies(from_date, to_date):
	"""Return every company with dashboard activity in the comparison window."""
	return frappe.db.sql_list(
		"""
		select distinct company
		  from (
			select company from `tabSales Order`
			 where docstatus in (0, 1) and transaction_date between %s and %s
			union all
			select company from `tabDelivery Note`
			 where docstatus=1 and is_return=1 and posting_date between %s and %s
			union all
			select company from `tabEcommerce Promotion Expense`
			 where posting_date between %s and %s
			union all
			select company from `tabEcommerce Fee Detail`
			 where status='有效' and posting_date between %s and %s
		  ) activity
		 where coalesce(company, '') != ''
		 order by company
		""",
		[from_date, to_date, from_date, to_date, from_date, to_date, from_date, to_date],
	)


def _build_periods_for_companies(filters, companies, include_rows=True, mode="estimate"):
	if not companies:
		return _format_period(filters, [], include_rows=include_rows)
	if len(companies) == 1:
		return _build_period({**filters, "company": companies[0]}, include_rows=include_rows, mode=mode)

	rows = []
	for company in companies:
		company_filters = {**filters, "company": company}
		period = _build_period(company_filters, include_rows=False, mode=mode)
		for row in period["_detail_rows"]:
			row["company"] = company
			rows.append(row)
	return _format_period(filters, rows, include_rows=include_rows)


def _build_period(filters, include_rows=True, mode="estimate"):
	rows = _sales_rows(filters)
	returns = _return_rows(filters)
	refunds = _refund_order_rows(filters)
	# 推广费是导入的实际数据（真金白银），不参与预估，两个口径都读。
	if mode == "settle":
		fee_details = _fee_detail_rows(filters)
	else:
		fee_details = {}
	promotions = _promotion_rows(filters)
	for key, values in returns.items():
		row = rows.setdefault(key, _empty_row(key))
		row["return_qty"] += values["return_qty"]
		row["return_cost"] += values["return_cost"]
		row["covered_return_qty"] += values["covered_return_qty"]
	for key, values in refunds.items():
		row = rows.setdefault(key, _empty_row(key))
		row["return_amount"] += values["return_amount"]
	for key, values in promotions.items():
		row = rows.setdefault(key, _empty_row(key))
		row["promotion_expense"] += values["promotion_expense"]
		row["impressions"] += values["impressions"]
		row["clicks"] += values["clicks"]
		row["attributed_orders"] += values["attributed_orders"]
		row["attributed_sales"] += values["attributed_sales"]
	for key, values in fee_details.items():
		row = rows.setdefault(key, _empty_row(key))
		for fee_type, amount in values.items():
			row["fee_breakdown"][fee_type] = flt(row["fee_breakdown"].get(fee_type)) + flt(amount)
			row["unified_fee_breakdown"][fee_type] = flt(row["unified_fee_breakdown"].get(fee_type)) + flt(amount)

	data = list(rows.values())
	for row in data:
		# 销售退货（吉客云店铺利润表口径）= 售后退货/仅退款负数单，按退款单自己的日期归集；
		# 退货入库单只贡献数量/成本，金额不重复扣减。
		row["net_sales"] = row["sales_amount"] - row["return_amount"]
		row["net_qty"] = row["sales_qty"] - row["return_qty"]
		row["product_cost"] = row["sales_cost"] - row["return_cost"]
	if mode != "settle":
		_apply_fee_estimates(data, filters)
	for row in data:
		row["rule_fees"] = sum(row["fee_breakdown"].values())
		row["gross_profit"] = row["net_sales"] - row["product_cost"]
		row["contribution_profit"] = row["gross_profit"] - row["promotion_expense"] - row["rule_fees"]
		row["company"] = filters["company"]
	return _format_period(filters, data, include_rows=include_rows)


def _apply_fee_estimates(rows, filters):
	"""预估口径：实时读「费用预估规则」，按计费方式估算费用到各店铺。"""
	rules = frappe.get_all(
		"Ecommerce Fee Estimate Rule",
		filters={"enabled": 1, "company": filters["company"]},
		fields=["name", "fee_item", "calculation_method", "rate"],
	)
	if not rules:
		return

	scopes_by_rule = defaultdict(list)
	if frappe.db.table_exists("Ecommerce Fee Estimate Scope"):
		for scope in frappe.get_all(
			"Ecommerce Fee Estimate Scope",
			filters={"parent": ["in", [rule.name for rule in rules]], "parenttype": "Ecommerce Fee Estimate Rule"},
			fields=["parent", "channel_name", "store_name"],
			order_by="parent, idx",
		):
			scopes_by_rule[scope.parent].append(scope)

	fee_item_map = {
		row["name"]: row
		for row in frappe.get_all("Ecommerce Fee Item", fields=["name", "item_name", "dashboard_field"])
	}
	# 推广费以导入的实际数据为准（_promotion_rows 已计入），跳过指向推广费的预估规则防止重复扣减。
	promotion_item_ids = {
		row["name"]
		for row in fee_item_map.values()
		if row.get("dashboard_field") == "promotion_expense"
	}
	rules = [rule for rule in rules if rule.fee_item not in promotion_item_ids]
	if not rules:
		return

	fixed_groups = defaultdict(list)
	for row in rows:
		for rule in rules:
			if not _estimate_rule_matches(scopes_by_rule.get(rule.name), row):
				continue
			fee_item = fee_item_map.get(rule.fee_item) or {}
			fee_type = fee_item.get("item_name") or rule.fee_item
			is_promotion = fee_item.get("dashboard_field") == "promotion_expense"
			if rule.calculation_method == "固定金额":
				fixed_groups[(row["date"], rule.name, fee_type, is_promotion)].append((row, rule))
				continue
			base = {
				"按销售额比例": row["net_sales"] / 100,
				"按订单数": row["orders"],
				"按件数": row["net_qty"],
			}.get(rule.calculation_method, 0)
			_add_estimate(row, fee_type, is_promotion, max(0, base * flt(rule.rate)))

	for (_date, _rule_name, fee_type, is_promotion), matched in fixed_groups.items():
		total_sales = sum(max(0, flt(row["net_sales"])) for row, _rule in matched)
		rate = flt(matched[0][1].rate)
		for row, _rule in matched:
			weight = max(0, flt(row["net_sales"])) / total_sales if total_sales else 1 / len(matched)
			_add_estimate(row, fee_type, is_promotion, rate * weight)


def _estimate_rule_matches(scopes, row):
	"""范围表为空表示适用全部；否则任一行命中（渠道匹配且店铺为空或相等）即适用。"""
	if not scopes:
		return True
	return any(
		scope.channel_name == row["channel"]
		and (not scope.store_name or scope.store_name == row["store"])
		for scope in scopes
	)


def _add_estimate(row, fee_type, is_promotion, amount):
	if is_promotion:
		row["promotion_expense"] = flt(row.get("promotion_expense")) + flt(amount)
	else:
		row["fee_breakdown"][fee_type] = flt(row["fee_breakdown"].get(fee_type)) + flt(amount)


def _format_period(filters, data, include_rows=True):
	summary = _summarize(data)
	result = {"summary": summary, "_detail_rows": data}
	if not include_rows:
		return result
	result["trend"] = _aggregate(data, "date")
	result["stores"] = _aggregate_stores(data)
	result["channels"] = _aggregate(data, "channel")
	result["fee_breakdown"] = _fee_breakdown(data)
	result["data_quality"] = {
		"cost_coverage_pct": summary["cost_coverage_pct"],
		"promotion_days": len({row["date"] for row in data if row["promotion_expense"] > 0}),
		"period_days": (getdate(filters["to_date"]) - getdate(filters["from_date"])).days + 1,
		"unmapped_sales_amount": sum(row["sales_amount"] for row in data if row["channel"] == "未映射"),
	}
	return result


def _listing_performance(filters, period_rows, listing_dimension="main", mode="estimate"):
	"""Build a reconciled link-level view beneath the store summary.

	Store/day fees and promotion rows without a link identifier are allocated by
	net-sales share. Link-specific promotion rows remain directly attributable.
	"""
	if not frappe.db.has_column("Sales Order Item", "custom_ecommerce_listing_id"):
		return []
	identifier_column, name_column, _promotion_column = _listing_dimension_columns(
		listing_dimension
	)
	if not frappe.db.has_column("Sales Order Item", identifier_column):
		return []
	join, channel, store = _channel_sql("so")
	conditions, params = _base_conditions("so", "transaction_date", filters, channel, store, include_drafts=True)
	result = frappe.db.sql(
		f"""
		select so.transaction_date as date, {channel} as channel, {store} as store,
		       coalesce(nullif(soi.{identifier_column}, ''), '未识别') listing_id,
		       max(nullif(soi.{name_column}, '')) listing_name,
		       count(distinct so.name) orders, sum(soi.qty) sales_qty,
		       sum(soi.base_net_amount) sales_amount,
		       sum(case when soi.custom_jackyun_refund_status = 2 then soi.base_net_amount else 0 end) as refunded_amount,
		       sum(soi.qty * coalesce(cost.valuation_rate, 0)) sales_cost,
		       sum(case when coalesce(cost.valuation_rate, 0)>0 then soi.qty else 0 end) covered_sales_qty
		  from `tabSales Order` so
		  join `tabSales Order Item` soi on soi.parent=so.name and soi.parenttype='Sales Order'
		  {join}
		  left join (
			select item_code, coalesce(
			  sum(case when actual_qty>0 and valuation_rate>0 then actual_qty*valuation_rate else 0 end)
			    / nullif(sum(case when actual_qty>0 and valuation_rate>0 then actual_qty else 0 end), 0),
			  max(valuation_rate), 0) valuation_rate
			from tabBin group by item_code
		  ) cost on cost.item_code=soi.item_code
		 where {conditions}
		 group by so.transaction_date, {channel}, {store}, listing_id
		""",
		params,
		as_dict=True,
	)
	rows = {}
	for value in result:
		key = (str(value.date), value.channel, value.store, value.listing_id)
		row = _empty_listing_row(key)
		row["listing_name"] = value.listing_name or ""
		for field in ("orders", "sales_qty", "sales_amount", "refunded_amount", "sales_cost", "covered_sales_qty"):
			row[field] = flt(value.get(field))
		rows[key] = row

	_apply_listing_returns(rows, filters, listing_dimension)
	_apply_listing_refunds(rows, filters, listing_dimension)
	if mode == "settle":
		_apply_unified_listing_fees(rows, filters, listing_dimension)
	by_scope = defaultdict(list)
	for row in rows.values():
		# 退款口径与店铺一致：退款负数单按自己的日期+链接归属（吉客云店铺利润表口径）
		row["net_sales"] = row["sales_amount"] - row["return_amount"]
		row["net_qty"] = row["sales_qty"] - row["return_qty"]
		row["product_cost"] = row["sales_cost"] - row["return_cost"]
		by_scope[(row["date"], row["channel"], row["store"])].append(row)

	period_by_scope = {(row["date"], row["channel"], row["store"]): row for row in period_rows}
	for scope, scoped_rows in by_scope.items():
		store_row = period_by_scope.get(scope)
		if not store_row:
			continue
		# 新费用模型的直接归属/正式分摊结果已在上一步写入链接；这里只分摊旧规则费用。
		legacy_breakdown = dict(store_row.get("fee_breakdown") or {})
		for fee_type, amount in (store_row.get("unified_fee_breakdown") or {}).items():
			legacy_breakdown[fee_type] = flt(legacy_breakdown.get(fee_type)) - flt(amount)
		legacy_breakdown = {key: value for key, value in legacy_breakdown.items() if abs(value) > 0.000001}
		_allocate_fee_breakdown(scoped_rows, legacy_breakdown)

	data = list(rows.values())
	for row in data:
		row["gross_profit"] = row["net_sales"] - row["product_cost"]
		row["contribution_profit"] = (
			row["gross_profit"] - row["promotion_expense"] - row["rule_fees"]
		)
		row["contribution_margin_pct"] = (
			row["contribution_profit"] / row["net_sales"] * 100 if row["net_sales"] else 0
		)
	return _aggregate_listing_period(data)


def _aggregate_listing_period(rows):
	grouped = {}
	sum_fields = (
		"orders", "sales_qty", "sales_amount", "sales_cost", "covered_sales_qty",
		"return_amount", "return_qty", "return_cost", "covered_return_qty",
		"promotion_expense", "rule_fees", "net_sales", "net_qty", "product_cost",
		"gross_profit", "contribution_profit",
	)
	for row in rows:
		key = (row["channel"], row["store"], row["listing_id"])
		target = grouped.setdefault(
			key,
			{
				"channel": row["channel"],
				"store": row["store"],
				"listing_id": row["listing_id"],
				"listing_name": row.get("listing_name") or "",
				"fee_breakdown": {},
				**{field: 0.0 for field in sum_fields},
			},
		)
		if not target["listing_name"] and row.get("listing_name"):
			target["listing_name"] = row["listing_name"]
		for field in sum_fields:
			target[field] += flt(row.get(field))
		for fee_type, amount in (row.get("fee_breakdown") or {}).items():
			target["fee_breakdown"][fee_type] = (
				flt(target["fee_breakdown"].get(fee_type)) + flt(amount)
			)
	for row in grouped.values():
		row["contribution_margin_pct"] = (
			row["contribution_profit"] / row["net_sales"] * 100 if row["net_sales"] else 0
		)
	return sorted(grouped.values(), key=lambda row: row["net_sales"], reverse=True)


def _apply_listing_returns(rows, filters, listing_dimension="main"):
	identifier_column, _name_column, _promotion_column = _listing_dimension_columns(
		listing_dimension
	)
	join, channel, store = _channel_sql("dn")
	conditions, params = _return_conditions(filters, channel, store)
	result = frappe.db.sql(
		f"""
		select {RETURN_DATE_EXPR} as date, {channel} channel, {store} store,
		       coalesce(nullif(soi.{identifier_column}, ''), '未识别') listing_id,
		       sum(abs(dni.base_net_amount)) return_amount, sum(abs(dni.qty)) return_qty,
		       sum(abs(dni.qty) * coalesce(cost.valuation_rate, 0)) return_cost,
		       sum(case when coalesce(cost.valuation_rate, 0)>0 then abs(dni.qty) else 0 end) covered_return_qty
		  from `tabDelivery Note` dn
		  join `tabDelivery Note Item` dni on dni.parent=dn.name and dni.parenttype='Delivery Note'
		  left join `tabSales Order` so on so.custom_jackyun_trade_no = dn.custom_jackyun_source_trade_no
		  left join `tabSales Order Item` soi on soi.name=dni.so_detail
		  {join}
		  left join (
			select item_code, coalesce(
			  sum(case when actual_qty>0 and valuation_rate>0 then actual_qty*valuation_rate else 0 end)
			    / nullif(sum(case when actual_qty>0 and valuation_rate>0 then actual_qty else 0 end), 0),
			  max(valuation_rate), 0) valuation_rate
			from tabBin group by item_code
		  ) cost on cost.item_code=dni.item_code
		 where {conditions}
		 group by {RETURN_DATE_EXPR}, {channel}, {store}, listing_id
		""",
		params,
		as_dict=True,
	)
	for value in result:
		key = (str(value.date), value.channel, value.store, value.listing_id)
		row = rows.setdefault(key, _empty_listing_row(key))
		# 退款金额已按订单行计入（refunded_amount）；退货入库单只贡献数量/成本
		for field in ("return_qty", "return_cost", "covered_return_qty"):
			row[field] += flt(value.get(field))


def _apply_listing_promotions(rows, filters, listing_dimension="main"):
	_identifier_column, _name_column, promotion_column = _listing_dimension_columns(
		listing_dimension
	)
	conditions = ["company=%s", "posting_date between %s and %s"]
	params = [filters["company"], filters["from_date"], filters["to_date"]]
	if filters.get("channel_name"):
		conditions.append("channel_name=%s")
		params.append(filters["channel_name"])
	if filters.get("store_name"):
		conditions.append("store_name=%s")
		params.append(filters["store_name"])
	result = frappe.db.sql(
		f"""select posting_date date, coalesce(nullif(channel_name,''),'未映射') channel,
		              coalesce(nullif(store_name,''),'未映射') store,
		              nullif({promotion_column},'') listing_id, sum(amount) amount
		         from `tabEcommerce Promotion Expense` expense
		         {_active_promotion_channel_join('expense')}
		        where {' and '.join(conditions)}
		        group by posting_date, channel_name, store_name, {promotion_column}""",
		params,
		as_dict=True,
	)
	for value in result:
		scope = (str(value.date), value.channel, value.store)
		if value.listing_id:
			key = (*scope, value.listing_id)
			rows.setdefault(key, _empty_listing_row(key))["promotion_expense"] += flt(value.amount)
			continue
		scoped_rows = [row for key, row in rows.items() if key[:3] == scope]
		if not scoped_rows:
			key = (*scope, "未识别")
			scoped_rows = [rows.setdefault(key, _empty_listing_row(key))]
		_allocate_amount(scoped_rows, flt(value.amount), "promotion_expense")


def _apply_unified_listing_fees(rows, filters, listing_dimension="main"):
	"""链接口径实时把费用明细摊到各链接，不再读写任何分摊快照。"""
	details = _fee_details_for_listing(filters)
	if not details:
		return
	platform_item_ids = _listing_platform_item_ids(details)
	scoped_rows = defaultdict(list)
	for key, row in rows.items():
		scoped_rows[key[:3]].append(row)

	for detail in details:
		amount = flt(detail.amount)
		scope = (str(detail.posting_date), detail.channel, detail.store)
		if detail.product_listing:
			row = _listing_row_for(rows, scope, detail.product_listing, platform_item_ids, listing_dimension)
			_add_fee_to_listing_row(row, detail.fee_type, detail.dashboard_field, amount)
			continue
		if detail.allocation_method == "不分摊":
			continue
		if detail.allocation_method == "固定比例":
			target_rows, weights = [], []
			for listing_name, ratio in detail.allocation_ratios:
				target_rows.append(
					_listing_row_for(rows, scope, listing_name, platform_item_ids, listing_dimension)
				)
				weights.append(flt(ratio))
		else:
			target_rows = scoped_rows.get(scope) or []
			weights = _listing_weights(detail.allocation_method, target_rows)
		if not target_rows or sum(weights) <= 0:
			continue
		for row, allocated in zip(target_rows, _allocate_rounded_amount(amount, weights)):
			_add_fee_to_listing_row(row, detail.fee_type, detail.dashboard_field, allocated)


def _fee_details_for_listing(filters):
	"""读取有效费用明细并附带固定比例子表，供链接口径实时分摊。"""
	conditions = ["detail.company=%s", "detail.posting_date between %s and %s", "detail.status='有效'"]
	params = [filters["company"], filters["from_date"], filters["to_date"]]
	if filters.get("channel_name"):
		conditions.append("detail.channel_name=%s")
		params.append(filters["channel_name"])
	if filters.get("store_name"):
		conditions.append("detail.store_name=%s")
		params.append(filters["store_name"])
	details = frappe.db.sql(
		f"""select detail.name, detail.posting_date,
		           coalesce(nullif(detail.channel_name,''),'未映射') channel,
		           coalesce(nullif(detail.store_name,''),'未映射') store,
		           detail.product_listing, detail.allocation_method, detail.amount,
		           item.item_name fee_type, item.dashboard_field
		      from `tabEcommerce Fee Detail` detail
		      join `tabEcommerce Fee Item` item on item.name=detail.fee_item
		     where {' and '.join(conditions)}
		     order by detail.posting_date, detail.creation, detail.name""",
		params,
		as_dict=True,
	)
	fixed_parents = [d.name for d in details if d.allocation_method == "固定比例" and not d.product_listing]
	ratios = defaultdict(list)
	if fixed_parents:
		for row in frappe.get_all(
			"Ecommerce Fee Allocation Ratio",
			filters={"parent": ["in", fixed_parents], "parenttype": "Ecommerce Fee Detail"},
			fields=["parent", "product_listing", "ratio"],
			order_by="idx",
		):
			ratios[row.parent].append((row.product_listing, flt(row.ratio)))
	for detail in details:
		detail.allocation_ratios = ratios.get(detail.name, [])
	return details


def _listing_platform_item_ids(details):
	names = {d.product_listing for d in details if d.product_listing}
	for detail in details:
		if detail.allocation_method == "固定比例":
			names.update(listing_name for listing_name, _ratio in detail.allocation_ratios)
	names.discard(None)
	if not names:
		return {}
	return {
		row.name: row.platform_item_id
		for row in frappe.get_all(
			"Ecommerce Product Listing",
			filters={"name": ["in", list(names)]},
			fields=["name", "platform_item_id"],
		)
	}


def _listing_row_for(rows, scope, listing_name, platform_item_ids, listing_dimension):
	key = (*scope, _listing_id_for(listing_name, platform_item_ids, listing_dimension))
	return rows.setdefault(key, _empty_listing_row(key))


def _listing_id_for(listing_name, platform_item_ids, listing_dimension):
	if listing_dimension == "child":
		return "未识别"
	return platform_item_ids.get(listing_name) or "未识别"


def _listing_weights(method, target_rows):
	"""把链接行驱动字段换算成 fee_management 的规范权重输入。"""
	driver_rows = [
		{
			"sales_amount": max(0, flt(row.get("sales_amount"))),
			"order_count": max(0, flt(row.get("orders"))),
			"quantity": max(0, flt(row.get("sales_qty"))),
		}
		for row in target_rows
	]
	return _weights_for(method, driver_rows)


def _add_fee_to_listing_row(row, fee_type, dashboard_field, amount):
	if dashboard_field == "promotion_expense":
		row["promotion_expense"] += flt(amount)
	else:
		row["fee_breakdown"][fee_type] = flt(row["fee_breakdown"].get(fee_type)) + flt(amount)
		row["rule_fees"] += flt(amount)


def _listing_dimension_columns(listing_dimension):
	if listing_dimension == "child":
		return "custom_ecommerce_platform_sku_id", "item_name", "platform_sku"
	return (
		"custom_ecommerce_listing_id",
		"custom_ecommerce_platform_item_name",
		"platform_item_code",
	)


def _allocate_amount(rows, amount, field):
	if not rows or not amount:
		return
	total = sum(max(0, flt(row.get("net_sales", row.get("sales_amount")))) for row in rows)
	for row in rows:
		weight = (
			max(0, flt(row.get("net_sales", row.get("sales_amount")))) / total
			if total
			else 1 / len(rows)
		)
		row[field] += amount * weight


def _allocate_fee_breakdown(rows, fee_breakdown):
	if not rows:
		return
	for fee_type, amount in fee_breakdown.items():
		if not amount:
			continue
		total = sum(max(0, flt(row.get("net_sales"))) for row in rows)
		for row in rows:
			weight = max(0, flt(row.get("net_sales"))) / total if total else 1 / len(rows)
			allocated = flt(amount) * weight
			row["fee_breakdown"][fee_type] = (
				flt(row["fee_breakdown"].get(fee_type)) + allocated
			)
			row["rule_fees"] += allocated


def _empty_listing_row(key):
	date, channel, store, listing_id = key
	return {
		**_empty_row((date, channel, store)),
		"listing_id": listing_id,
		"listing_name": "",
		"rule_fees": 0.0,
	}


def _channel_sql(alias):
	if _channel_available():
		active_conditions = _active_sales_channel_sql_conditions("ch")
		return (
			f"join `tabJackyun Sales Channel` ch on ch.name={alias}.custom_jackyun_sales_channel and {active_conditions}",
			"coalesce(nullif(ch.online_platform_name, ''), '未映射')",
			"coalesce(nullif(ch.channel_name, ''), '未映射')",
		)
	return "", "'未映射'", "'未映射'"


def _base_conditions(alias, date_field, filters, channel_expr, store_expr, include_drafts=False):
	# 销售口径含草稿（未发货的占位单/待提交单），已取消排除；退货口径只认已提交。
	docstatus = f"{alias}.docstatus in (0, 1)" if include_drafts else f"{alias}.docstatus=1"
	conditions = [docstatus, f"{alias}.company=%s", f"{alias}.{date_field} between %s and %s"]
	params = [filters["company"], filters["from_date"], filters["to_date"]]
	if filters.get("channel_name"):
		conditions.append(f"{channel_expr}=%s")
		params.append(filters["channel_name"])
	if filters.get("store_name"):
		conditions.append(f"{store_expr}=%s")
		params.append(filters["store_name"])
	return " and ".join(conditions), params


def _sales_rows(filters):
	join, channel, store = _channel_sql("so")
	conditions, params = _base_conditions("so", "transaction_date", filters, channel, store, include_drafts=True)
	result = frappe.db.sql(
		f"""
		select so.transaction_date as date, {channel} as channel, {store} as store,
		       count(distinct so.name) as orders, sum(soi.qty) as sales_qty,
		       sum(case when soi.item_code = 'TM-PLACEHOLDER'
		                then soi.base_net_amount
		                else coalesce(soi.custom_jackyun_line_sell_total, 0) end) as sales_amount,
		       sum(case when soi.custom_jackyun_refund_status = 2 then soi.base_net_amount else 0 end) as refunded_amount,
		       sum(soi.qty * coalesce(cost.valuation_rate, 0)) as sales_cost,
		       sum(case when coalesce(cost.valuation_rate, 0)>0 then soi.qty else 0 end) as covered_sales_qty
		  from `tabSales Order` so
		  join `tabSales Order Item` soi on soi.parent=so.name and soi.parenttype='Sales Order'
		  {join}
		  left join (
			select item_code,
			       coalesce(
			         sum(case when actual_qty>0 and valuation_rate>0 then actual_qty*valuation_rate else 0 end)
			           / nullif(sum(case when actual_qty>0 and valuation_rate>0 then actual_qty else 0 end), 0),
			         max(valuation_rate), 0
			       ) as valuation_rate
			  from tabBin group by item_code
		  ) cost on cost.item_code=soi.item_code
		 where {conditions}
		 group by so.transaction_date, {channel}, {store}
		 order by so.transaction_date
		""",
		params,
		as_dict=True,
	)
	adjustments = _order_revenue_adjustments(filters)
	dn_costs = _placeholder_dn_cost_rows(filters)
	rows = {}
	for value in result:
		key = (str(value.date), value.channel, value.store)
		row = _empty_row(key)
		for field in ("orders", "sales_qty", "sales_amount", "refunded_amount", "sales_cost", "covered_sales_qty"):
			row[field] = flt(value.get(field))
		# 店铺利润表口径：销售收入 = 货款合计 - 整单优惠 + 应收邮资
		discount, post_fee = adjustments.get(key, (0.0, 0.0))
		row["sales_amount"] += post_fee - discount
		# 占位订单（天猫无明细）货品成本 = 按来源订单号反查销售出库明细 × 库存估值
		extra = dn_costs.get(key)
		if extra:
			row["sales_cost"] += flt(extra.get("dn_cost"))
			row["covered_sales_qty"] += flt(extra.get("covered_qty"))
		rows[key] = row
	return rows


def _placeholder_dn_cost_rows(filters) -> dict:
	"""占位销售订单（TM-PLACEHOLDER）的货品成本：反查销售出库单明细×库存估值。

	只服务占位订单，与常规订单行成本互斥，不会双重计算；成本归到销售订单的
	下单日期/渠道/店铺（与销售额同口径）。
	"""
	if not frappe.db.has_column("Delivery Note", "custom_jackyun_source_order"):
		return {}
	join, channel, store = _channel_sql("so")
	conditions, params = _base_conditions("so", "transaction_date", filters, channel, store, include_drafts=True)
	result = frappe.db.sql(
		f"""
		select so.transaction_date as date, {channel} as channel, {store} as store,
		       sum(dni.qty * coalesce(cost.valuation_rate, 0)) as dn_cost,
		       sum(case when coalesce(cost.valuation_rate, 0) > 0 then dni.qty else 0 end) as covered_qty
		  from `tabSales Order` so
		  join `tabSales Order Item` soi on soi.parent = so.name and soi.parenttype = 'Sales Order'
		   and soi.item_code = 'TM-PLACEHOLDER'
		  join `tabDelivery Note` dn on dn.custom_jackyun_source_order = so.name
		   and dn.is_return = 0 and dn.docstatus != 2
		  join `tabDelivery Note Item` dni on dni.parent = dn.name and dni.parenttype = 'Delivery Note'
		  {join}
		  left join (
			select item_code, coalesce(
			  sum(case when actual_qty>0 and valuation_rate>0 then actual_qty*valuation_rate else 0 end)
			    / nullif(sum(case when actual_qty>0 and valuation_rate>0 then actual_qty else 0 end), 0),
			  max(valuation_rate), 0) valuation_rate
			from tabBin group by item_code
		  ) cost on cost.item_code = dni.item_code
		 where {conditions}
		 group by so.transaction_date, {channel}, {store}
		""",
		params,
		as_dict=True,
	)
	return {
		(str(row.date), row.channel, row.store): {
			"dn_cost": flt(row.dn_cost), "covered_qty": flt(row.covered_qty),
		}
		for row in result
	}


def _order_revenue_adjustments(filters) -> dict:
	"""按 (日期, 渠道, 店铺) 汇总订单整单优惠与应收邮资（独立聚合避免行数放大）。"""
	join, channel, store = _channel_sql("so")
	conditions, params = _base_conditions("so", "transaction_date", filters, channel, store, include_drafts=True)
	has_discount = frappe.db.has_column("Sales Order", "custom_jackyun_discount_fee")
	has_post = frappe.db.has_column("Sales Order", "custom_jackyun_received_post_fee")
	if not has_discount and not has_post:
		return {}
	discount_expr = "sum(coalesce(so.custom_jackyun_discount_fee, 0))" if has_discount else "0"
	post_expr = "sum(coalesce(so.custom_jackyun_received_post_fee, 0))" if has_post else "0"
	result = frappe.db.sql(
		f"""
		select so.transaction_date as date, {channel} as channel, {store} as store,
		       {discount_expr} as discount_total, {post_expr} as post_fee_total
		  from `tabSales Order` so
		  {join}
		 where {conditions}
		 group by so.transaction_date, {channel}, {store}
		""",
		params,
		as_dict=True,
	)
	return {
		(str(row.date), row.channel, row.store): (flt(row.discount_total), flt(row.post_fee_total))
		for row in result
	}


RETURN_DATE_EXPR = (
	# 退货归因到原订单日期（吉客云口径）：源订单日期 > 单号前缀日期 > 退货过账日
	"coalesce("
	"so.transaction_date, "
	"str_to_date(substring(dn.custom_jackyun_source_trade_no, 3, 8), '%%Y%%m%%d'), "
	"dn.posting_date"
	")"
)

REFUND_ORDER_CANCELLED = (4121, 4122, 5010, 5020, 5030)


def _refund_order_rows(filters):
	"""销售退货（吉客云店铺利润表口径）：售后退货/仅退款负数单，按退款单自己的日期归集。"""
	if not frappe.db.table_exists("Jackyun Refund Order"):
		return {}
	conditions = [
		"r.trade_status not in {}".format(REFUND_ORDER_CANCELLED),
		"date(r.refund_date) between %s and %s",
	]
	params = [filters["from_date"], filters["to_date"]]
	if filters.get("company"):
		conditions.append("r.company_name=%s")
		params.append(filters["company"])
	if filters.get("channel_name"):
		conditions.append("coalesce(nullif(ch.online_platform_name, ''), '未映射')=%s")
		params.append(filters["channel_name"])
	if filters.get("store_name"):
		conditions.append("coalesce(nullif(ch.channel_name, ''), '未映射')=%s")
		params.append(filters["store_name"])
	result = frappe.db.sql(
		f"""
		select date(r.refund_date) as date,
		       coalesce(nullif(ch.online_platform_name, ''), '未映射') as channel,
		       coalesce(nullif(ch.channel_name, ''), '未映射') as store,
		       sum(r.amount) as return_amount
		  from `tabJackyun Refund Order` r
		  left join `tabJackyun Sales Channel` ch on ch.name = r.channel
		 where {' and '.join(conditions)}
		 group by date(r.refund_date), channel, store
		""",
		params,
		as_dict=True,
	)
	return {
		(str(row.date), row.channel, row.store): {"return_amount": flt(row.return_amount)}
		for row in result
	}


def _apply_listing_refunds(rows, filters, listing_dimension="main"):
	"""链接级退款归属：退款单商品明细按链接/子链接归集，无标识的归「未识别」。"""
	import json as _json

	if not frappe.db.table_exists("Jackyun Refund Order"):
		return
	conditions = [
		"r.trade_status not in {}".format(REFUND_ORDER_CANCELLED),
		"date(r.refund_date) between %s and %s",
	]
	params = [filters["from_date"], filters["to_date"]]
	if filters.get("company"):
		conditions.append("r.company_name=%s")
		params.append(filters["company"])
	if filters.get("channel_name"):
		conditions.append("coalesce(nullif(ch.online_platform_name, ''), '未映射')=%s")
		params.append(filters["channel_name"])
	if filters.get("store_name"):
		conditions.append("coalesce(nullif(ch.channel_name, ''), '未映射')=%s")
		params.append(filters["store_name"])
	records = frappe.db.sql(
		f"""
		select date(r.refund_date) as date,
		       coalesce(nullif(ch.online_platform_name, ''), '未映射') as channel,
		       coalesce(nullif(ch.channel_name, ''), '未映射') as store,
		       r.amount, r.refund_lines_json
		  from `tabJackyun Refund Order` r
		  left join `tabJackyun Sales Channel` ch on ch.name = r.channel
		 where {' and '.join(conditions)}
		""",
		params,
		as_dict=True,
	)
	for record in records:
		scope = (str(record.date), record.channel, record.store)
		try:
			lines = _json.loads(record.refund_lines_json or "[]")
		except (TypeError, ValueError):
			lines = []
		allocated = 0.0
		for line in lines:
			if not isinstance(line, dict):
				continue
			identifier = frappe.utils.cstr(
				line.get("plat_sku_id" if listing_dimension == "child" else "plat_goods_id")
			).strip()
			amount = flt(line.get("amount"))
			allocated += amount
			key = (*scope, identifier or "未识别")
			rows.setdefault(key, _empty_listing_row(key))["return_amount"] += amount
		# 明细缺失或与表头的残差归「未识别」，保证链接合计与店铺口径对账一致
		residual = flt(record.amount) - allocated
		if abs(residual) > 0.005:
			key = (*scope, "未识别")
			rows.setdefault(key, _empty_listing_row(key))["return_amount"] += residual


def _return_conditions(filters, channel_expr, store_expr):
	"""退货口径：按归因日期过滤（不沿用 _base_conditions 的 posting_date）。"""
	conditions = [
		"dn.docstatus=1",
		"dn.company=%s",
		f"{RETURN_DATE_EXPR} between %s and %s",
		"dn.is_return=1",
	]
	params = [filters["company"], filters["from_date"], filters["to_date"]]
	if filters.get("channel_name"):
		conditions.append(f"{channel_expr}=%s")
		params.append(filters["channel_name"])
	if filters.get("store_name"):
		conditions.append(f"{store_expr}=%s")
		params.append(filters["store_name"])
	return " and ".join(conditions), params


def _return_rows(filters):
	join, channel, store = _channel_sql("dn")
	conditions, params = _return_conditions(filters, channel, store)
	result = frappe.db.sql(
		f"""
		select {RETURN_DATE_EXPR} as date, {channel} as channel, {store} as store,
		       sum(abs(dni.base_net_amount)) as return_amount, sum(abs(dni.qty)) as return_qty,
		       sum(abs(dni.qty) * coalesce(cost.valuation_rate, 0)) as return_cost,
		       sum(case when coalesce(cost.valuation_rate, 0)>0 then abs(dni.qty) else 0 end) as covered_return_qty
		  from `tabDelivery Note` dn
		  join `tabDelivery Note Item` dni on dni.parent=dn.name and dni.parenttype='Delivery Note'
		  left join `tabSales Order` so on so.custom_jackyun_trade_no = dn.custom_jackyun_source_trade_no
		  {join}
		  left join (
			select item_code, coalesce(
			  sum(case when actual_qty>0 and valuation_rate>0 then actual_qty*valuation_rate else 0 end)
			    / nullif(sum(case when actual_qty>0 and valuation_rate>0 then actual_qty else 0 end), 0),
			  max(valuation_rate), 0) valuation_rate
			from tabBin group by item_code
		  ) cost on cost.item_code=dni.item_code
		 where {conditions}
		 group by {RETURN_DATE_EXPR}, {channel}, {store}
		""",
		params,
		as_dict=True,
	)
	return {
		(str(row.date), row.channel, row.store): {
			"return_amount": flt(row.return_amount), "return_qty": flt(row.return_qty),
			"return_cost": flt(row.return_cost), "covered_return_qty": flt(row.covered_return_qty),
		}
		for row in result
	}


def _promotion_rows(filters):
	conditions = ["detail.company=%s", "detail.posting_date between %s and %s", "detail.status='有效'", "item.dashboard_field='promotion_expense'"]
	params = [filters["company"], filters["from_date"], filters["to_date"]]
	if filters.get("channel_name"):
		conditions.append("detail.channel_name=%s")
		params.append(filters["channel_name"])
	if filters.get("store_name"):
		conditions.append("detail.store_name=%s")
		params.append(filters["store_name"])
	result = frappe.db.sql(
		f"""select detail.posting_date as date, detail.channel_name as channel, detail.store_name as store,
		              sum(amount) promotion_expense
		         from `tabEcommerce Fee Detail` detail
		         join `tabEcommerce Fee Item` item on item.name=detail.fee_item
		         {_active_promotion_channel_join('detail')}
		        where {' and '.join(conditions)}
		        group by detail.posting_date, detail.channel_name, detail.store_name""",
		params,
		as_dict=True,
	)
	return {
		(str(row.date), row.channel or "未映射", row.store or "未映射"): {
			"promotion_expense": flt(row.promotion_expense),
			# 经营仪表盘只使用ERP订单收入；平台归因指标只在独立推广分析页读取。
			"impressions": 0, "clicks": 0, "attributed_orders": 0, "attributed_sales": 0,
		}
		for row in result
	}


def _fee_detail_rows(filters):
	conditions = ["detail.company=%s", "detail.posting_date between %s and %s", "detail.status='有效'", "item.dashboard_field!='promotion_expense'"]
	params = [filters["company"], filters["from_date"], filters["to_date"]]
	if filters.get("channel_name"):
		conditions.append("detail.channel_name=%s")
		params.append(filters["channel_name"])
	if filters.get("store_name"):
		conditions.append("detail.store_name=%s")
		params.append(filters["store_name"])
	result = frappe.db.sql(
		f"""select detail.posting_date date, coalesce(nullif(detail.channel_name,''),'未映射') channel,
		              coalesce(nullif(detail.store_name,''),'未映射') store,
		              item.item_name fee_type, sum(detail.amount) amount
		         from `tabEcommerce Fee Detail` detail
		         join `tabEcommerce Fee Item` item on item.name=detail.fee_item
		        where {' and '.join(conditions)}
		        group by detail.posting_date, detail.channel_name, detail.store_name, item.item_name""",
		params, as_dict=True,
	)
	grouped = defaultdict(dict)
	for row in result:
		grouped[(str(row.date), row.channel, row.store)][row.fee_type] = flt(row.amount)
	return grouped


def _empty_row(key):
	date, channel, store = key
	return {
		"date": date, "channel": channel or "未映射", "store": store or "未映射",
		"orders": 0.0, "sales_qty": 0.0, "sales_amount": 0.0, "sales_cost": 0.0,
		"refunded_amount": 0.0, "covered_sales_qty": 0.0, "return_amount": 0.0, "return_qty": 0.0,
		"return_cost": 0.0, "covered_return_qty": 0.0, "promotion_expense": 0.0,
		"impressions": 0.0, "clicks": 0.0, "attributed_orders": 0.0, "attributed_sales": 0.0,
		"fee_breakdown": {},
		"unified_fee_breakdown": {},
	}


def _summarize(rows):
	fields = ("orders", "sales_qty", "sales_amount", "return_amount", "return_qty", "net_sales", "net_qty", "product_cost", "promotion_expense", "rule_fees", "gross_profit", "contribution_profit", "covered_sales_qty", "covered_return_qty")
	result = {field: sum(flt(row.get(field)) for row in rows) for field in fields}
	result["gross_margin_pct"] = result["gross_profit"] / result["net_sales"] * 100 if result["net_sales"] else 0
	result["contribution_margin_pct"] = result["contribution_profit"] / result["net_sales"] * 100 if result["net_sales"] else 0
	result["promotion_ratio_pct"] = result["promotion_expense"] / result["net_sales"] * 100 if result["net_sales"] else 0
	result["return_ratio_pct"] = result["return_amount"] / result["sales_amount"] * 100 if result["sales_amount"] else 0
	coverage_denominator = result["sales_qty"] + result["return_qty"]
	result["cost_coverage_pct"] = (result["covered_sales_qty"] + result["covered_return_qty"]) / coverage_denominator * 100 if coverage_denominator else 0
	return result


def _aggregate(rows, dimension):
	grouped = defaultdict(list)
	for row in rows:
		grouped[row[dimension]].append(row)
	result = []
	for label, values in grouped.items():
		item = {dimension: label, **_summarize(values)}
		result.append(item)
	sort_key = "date" if dimension == "date" else "net_sales"
	return sorted(result, key=lambda row: row[sort_key], reverse=dimension != "date")


def _aggregate_stores(rows):
	"""Keep stores distinct by platform while exposing the Jackyun channel name as store."""
	grouped = defaultdict(list)
	for row in rows:
		grouped[(row["channel"], row["store"])].append(row)
	result = []
	for (channel, store), values in grouped.items():
		fee_breakdown = defaultdict(float)
		for row in values:
			for fee_type, amount in (row.get("fee_breakdown") or {}).items():
				fee_breakdown[fee_type] += flt(amount)
		result.append({
			"channel": channel,
			"store": store,
			"fee_breakdown": dict(fee_breakdown),
			**_summarize(values),
		})
	return sorted(result, key=lambda row: row["net_sales"], reverse=True)


def _fee_breakdown(rows):
	result = defaultdict(float)
	columns = _dashboard_fee_columns()
	visible_names = {item["label"] for item in columns}
	display_order = {item["label"]: item["display_order"] for item in columns}
	promotion_visible = any(item["dashboard_field"] == "promotion_expense" for item in columns)
	for item in columns:
		result[item["label"]] += 0
	for row in rows:
		if promotion_visible:
			promotion_label = next(item["label"] for item in columns if item["dashboard_field"] == "promotion_expense")
			result[promotion_label] += row["promotion_expense"]
		for fee_type, amount in row["fee_breakdown"].items():
			if fee_type in visible_names:
				result[fee_type] += amount
	return [
		{"fee_type": key, "amount": value}
		for key, value in sorted(result.items(), key=lambda pair: (display_order.get(pair[0], 999999), -pair[1], pair[0]))
	]


def _dashboard_fee_columns():
	"""费用列只由启用且允许仪表盘显示的费用项目决定，与规则和金额无关。"""
	if not frappe.db.table_exists("Ecommerce Fee Item"):
		return []
	return [
		{
			"fee_item": item.name,
			"label": item.item_name,
			"dashboard_field": item.dashboard_field,
			"display_order": cint(item.display_order),
		}
		for item in frappe.get_all(
			"Ecommerce Fee Item", filters={"enabled": 1, "show_on_dashboard": 1},
			fields=["name", "item_name", "dashboard_field", "display_order"],
			order_by="display_order, item_name",
		)
	]


def _channel_available():
	return frappe.db.table_exists("Jackyun Sales Channel") and frappe.db.has_column("Sales Order", "custom_jackyun_sales_channel")


def _active_sales_channel_filters():
	filters = {}
	if frappe.db.has_column("Jackyun Sales Channel", "channel_type"):
		filters["channel_type"] = "1"
	for fieldname in ("disabled", "deleted"):
		if frappe.db.has_column("Jackyun Sales Channel", fieldname):
			filters[fieldname] = 0
	return filters


def _active_sales_channel_sql_conditions(alias):
	conditions = []
	if frappe.db.has_column("Jackyun Sales Channel", "channel_type"):
		conditions.append(f"coalesce({alias}.channel_type, '')='1'")
	for fieldname in ("disabled", "deleted"):
		if frappe.db.has_column("Jackyun Sales Channel", fieldname):
			conditions.append(f"coalesce({alias}.{fieldname}, 0)=0")
	return " and ".join(conditions) or "1=1"


def _active_promotion_channel_join(expense_alias):
	if not frappe.db.table_exists("Jackyun Sales Channel"):
		return "join (select null channel, null store) active_channel on 1=0"
	conditions = _active_sales_channel_sql_conditions("active_source")
	return f"""
		join (
			select distinct online_platform_name channel, channel_name store
			  from `tabJackyun Sales Channel` active_source
			 where {conditions}
		) active_channel
		  on active_channel.channel={expense_alias}.channel_name
		 and active_channel.store={expense_alias}.store_name
	"""


def _check_dashboard_permission():
	frappe.only_for(("Sales Manager", "Accounts User", "Accounts Manager", "System Manager"))
