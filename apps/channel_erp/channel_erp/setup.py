import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from channel_erp.integrations.field_mapping_catalog import FIELD_MAPPING_CATALOG


CONTENT_CENTER_DOCTYPES = (
    "Channel Content Access Grant",
    "Channel Content Access Log",
    "Channel Content Asset",
    "Channel Content Asset Tag",
    "Channel Content Asset Version",
    "Channel Content Audit Log",
    "Channel Content Expiry Reminder Log",
    "Channel Content Favorite",
    "Channel Content Folder",
    "Channel Content Folder Access",
    "Channel Content Insight",
    "Channel Content Quality Metric",
    "Channel Content Search Event",
    "Channel Content Search Index",
    "Channel Content Settings",
    "Channel Content Share Link",
    "Channel Content Suggestion",
    "Channel Content Workflow Reminder Log",
)

OBSOLETE_CONTENT_CENTER_DOCTYPES = (
    "Channel Content Archive Source",
    "Channel Content Archive Template",
    "Channel Content Asset Company Access",
    "Channel Content Batch Company Membership",
    "Channel Content Batch Report",
    "Channel Content Batch Report Requirement",
    "Channel Content Document Folder Setting",
    "Channel Content Relation",
)

JACKYUN_INTEGRATION_DOCTYPES = (
    "Connector Connection",
    "Connector Outbound Message",
    "External ID Mapping",
    "Integration Reconciliation Run",
    "Integration Field Mapping",
    "Jackyun Connection",
    "Jackyun Goods Category",
    "Jackyun Inventory Snapshot",
    "Jackyun Raw Record",
    "Jackyun Sales Channel",
    "Jackyun Sync Log",
    "Jackyun Sync Schedule",
)


def refresh_module_map():
    """Make modules added to modules.txt visible inside the active migration process."""
    frappe.cache.delete_value("app_modules")
    frappe.client_cache.delete_value("installed_app_modules")
    frappe.setup_module_map(include_all_apps=False)


def ensure_module_defs():
    """Create the two business modules without removing the legacy compatibility module."""
    for module_name in ("Jackyun Integration", "Content Center"):
        if frappe.db.exists("Module Def", module_name):
            frappe.db.set_value(
                "Module Def",
                module_name,
                {"module_name": module_name, "app_name": "channel_erp", "custom": 0},
                update_modified=False,
            )
            continue

        frappe.get_doc(
            {
                "doctype": "Module Def",
                "module_name": module_name,
                "app_name": "channel_erp",
                "custom": 0,
            }
        ).insert(ignore_permissions=True)


def apply_module_assignments():
    """Idempotently align metadata before and after model synchronization."""
    for doctype in CONTENT_CENTER_DOCTYPES:
        if frappe.db.exists("DocType", doctype):
            frappe.db.set_value("DocType", doctype, "module", "Content Center", update_modified=False)

    for doctype in JACKYUN_INTEGRATION_DOCTYPES:
        if frappe.db.exists("DocType", doctype):
            frappe.db.set_value(
                "DocType", doctype, "module", "Jackyun Integration", update_modified=False
            )

    assignments = (
        ("Page", "content-center", "Content Center"),
        ("Workspace", "Content Hub", "Content Center"),
        ("Workspace", "Channel Erp", "Jackyun Integration"),
        ("Workspace Sidebar", "Content Hub", "Content Center"),
        ("Workspace Sidebar", "Channel Erp", "Jackyun Integration"),
    )
    for doctype, name, module_name in assignments:
        if frappe.db.exists(doctype, name):
            frappe.db.set_value(doctype, name, "module", module_name, update_modified=False)

    frappe.clear_cache()


def before_migrate():
    refresh_module_map()
    ensure_module_defs()
    apply_module_assignments()
    refresh_module_map()


def ensure_custom_fields():
    create_custom_fields(
        {
            "Sales Order": [
                {
                    "fieldname": "custom_jackyun_sales_channel",
                    "label": "销售渠道",
                    "fieldtype": "Link",
                    "options": "Jackyun Sales Channel",
                    "insert_after": "customer_name",
                    "in_list_view": 1,
                    "in_standard_filter": 1,
                    "reqd": 1,
                    "read_only": 0,
                    "description": "吉客云订单自动匹配；手工新增销售订单时请选择",
                },
                {
                    "fieldname": "custom_jackyun_trade_type",
                    "label": "销售业务类型（零售/批发）",
                    "fieldtype": "Select",
                    "options": "零售业务\n批发业务",
                    "insert_after": "custom_jackyun_sales_channel",
                    "in_list_view": 1,
                    "in_standard_filter": 1,
                    "mandatory_depends_on": "eval:doc.custom_connector_source != 'JackYun'",
                    "description": "订单自身的业务类型，不属于销售渠道；写入吉客云时零售映射为1、批发映射为9",
                },
                {
                    "fieldname": "custom_jackyun_trade_no",
                    "label": "吉客云销售单号",
                    "fieldtype": "Data",
                    "insert_after": "custom_jackyun_trade_type",
                    "read_only": 1,
                    "in_list_view": 0,
                    "in_standard_filter": 1,
                    "unique": 1,
                    "description": "吉客云销售单 tradeNo；吉客云同步订单同时以此作为 ERPNext 编号",
                },
                {
                    "fieldname": "custom_jackyun_source_trade_no",
                    "label": "网店订单号",
                    "fieldtype": "Data",
                    "length": 1000,
                    "insert_after": "custom_jackyun_trade_no",
                    "read_only": 1,
                    "in_list_view": 1,
                    "in_standard_filter": 1,
                    "description": "平台/网店订单号；吉客云来源字段 onlineTradeNo，多子单时逗号拼接",
                },
                {
                    "fieldname": "custom_jackyun_order_time",
                    "label": "订单时间",
                    "fieldtype": "Datetime",
                    "insert_after": "custom_jackyun_source_trade_no",
                    "read_only": 1,
                    "in_list_view": 1,
                    "in_standard_filter": 1,
                    "description": "吉客云销售单 tradeTime，保留日期、时、分、秒",
                },
                {
                    "fieldname": "custom_jackyun_discount_fee",
                    "label": "整单优惠",
                    "fieldtype": "Currency",
                    "insert_after": "custom_jackyun_order_time",
                    "read_only": 1,
                    "options": "currency",
                    "description": "吉客云整单优惠 discountFee；店铺利润表口径 销售收入=货款合计-整单优惠",
                },
                {
                    "fieldname": "custom_jackyun_received_post_fee",
                    "label": "应收邮资",
                    "fieldtype": "Currency",
                    "insert_after": "custom_jackyun_discount_fee",
                    "read_only": 1,
                    "options": "currency",
                    "description": "吉客云应收邮资 receivedPostFee；店铺利润表口径计入邮资收入",
                },
                {
                    "fieldname": "custom_connector_sync_section",
                    "label": "外部ERP写入",
                    "fieldtype": "Section Break",
                    "insert_after": "custom_jackyun_received_post_fee",
                    "collapsible": 1,
                },
                {
                    "fieldname": "custom_connector_source",
                    "label": "订单来源系统",
                    "fieldtype": "Select",
                    "options": "ERPNext\nJackYun\nOther",
                    "default": "ERPNext",
                    "insert_after": "custom_connector_sync_section",
                    "read_only": 1,
                    "in_standard_filter": 1,
                    "description": "由连接器维护；JackYun 来源订单永不回推",
                },
                {
                    "fieldname": "custom_external_sync_status",
                    "label": "外部ERP写入状态",
                    "fieldtype": "Select",
                    "options": "\nPending\nPreflight Failed\nQueued\nProcessing\nUncertain\nSucceeded\nFailed\nBlocked\nConflict\nCancelled",
                    "insert_after": "custom_connector_source",
                    "read_only": 1,
                    "in_list_view": 1,
                    "in_standard_filter": 1,
                },
                {
                    "fieldname": "custom_external_revision",
                    "label": "外部订单修订版本",
                    "fieldtype": "Int",
                    "default": "0",
                    "insert_after": "custom_external_sync_status",
                    "read_only": 1,
                },
                {
                    "fieldname": "custom_external_online_trade_no",
                    "label": "外部幂等业务号",
                    "fieldtype": "Data",
                    "insert_after": "custom_external_revision",
                    "read_only": 1,
                    "in_standard_filter": 1,
                    "description": "推送前用于查询去重；修订订单使用 -R1、-R2 后缀",
                },
                {
                    "fieldname": "custom_external_sync_last_at",
                    "label": "最近外部写入时间",
                    "fieldtype": "Datetime",
                    "insert_after": "custom_external_online_trade_no",
                    "read_only": 1,
                },
                {
                    "fieldname": "custom_external_sync_message",
                    "label": "外部写入信息",
                    "fieldtype": "Small Text",
                    "insert_after": "custom_external_sync_last_at",
                    "read_only": 1,
                },
            ],
            "Sales Order Item": [
                {
                    "fieldname": "custom_ecommerce_listing_id",
                    "label": "在售链接ID",
                    "fieldtype": "Data",
                    "insert_after": "customer_item_code",
                    "read_only": 1,
                    "in_list_view": 1,
                    "in_standard_filter": 1,
                    "description": "电商平台商品/在售链接ID；吉客云来源字段 platGoodsId",
                },
                {
                    "fieldname": "custom_ecommerce_platform_code",
                    "label": "电商平台编码",
                    "fieldtype": "Data",
                    "insert_after": "custom_ecommerce_platform_sku_id",
                    "read_only": 1,
                    "in_standard_filter": 1,
                },
                {
                    "fieldname": "custom_ecommerce_platform_sku_id",
                    "label": "商品链接SkuID",
                    "fieldtype": "Data",
                    "insert_after": "custom_ecommerce_listing_id",
                    "read_only": 1,
                    "in_list_view": 1,
                    "in_standard_filter": 1,
                    "description": "电商平台子链接/SKU ID；吉客云来源字段 platSkuId",
                },
                {
                    "fieldname": "custom_ecommerce_platform_item_name",
                    "label": "平台商品名称",
                    "fieldtype": "Data",
                    "insert_after": "custom_ecommerce_platform_code",
                    "read_only": 1,
                },
                {
                    "fieldname": "custom_ecommerce_platform_sku",
                    "label": "平台/商家SKU编码",
                    "fieldtype": "Data",
                    "insert_after": "custom_ecommerce_platform_item_name",
                    "read_only": 1,
                    "in_standard_filter": 1,
                    "description": "网店子订单或商家SKU编码；不是商品链接SkuID",
                },
                {
                    "fieldname": "custom_jackyun_outer_id",
                    "label": "吉客云外部商品编码",
                    "fieldtype": "Data",
                    "insert_after": "custom_ecommerce_platform_sku",
                    "read_only": 1,
                },
                {
                    "fieldname": "custom_jackyun_outer_sku_id",
                    "label": "吉客云外部SKU编码",
                    "fieldtype": "Data",
                    "insert_after": "custom_jackyun_outer_id",
                    "read_only": 1,
                },
                {
                    "fieldname": "custom_jackyun_source_trade_no",
                    "label": "网店订单号",
                    "fieldtype": "Data",
                    "insert_after": "custom_jackyun_outer_sku_id",
                    "read_only": 1,
                },
                {
                    "fieldname": "custom_jackyun_source_subtrade_no",
                    "label": "网店子订单号",
                    "fieldtype": "Data",
                    "insert_after": "custom_jackyun_source_trade_no",
                    "read_only": 1,
                },
                {
                    "fieldname": "custom_jackyun_refund_status",
                    "label": "退款状态",
                    "fieldtype": "Int",
                    "insert_after": "custom_jackyun_source_subtrade_no",
                    "read_only": 1,
                    "description": "吉客云订单行退款状态；2=已退款",
                },
                {
                    "fieldname": "custom_jackyun_line_sell_total",
                    "label": "行销货金额",
                    "fieldtype": "Currency",
                    "insert_after": "custom_jackyun_refund_status",
                    "read_only": 1,
                    "options": "currency",
                    "description": "吉客云订单行 sellTotal（货款口径）；店铺利润表 销售收入=Σ行销货金额-Σ整单优惠",
                },
            ],
            "Delivery Note": [
                {
                    "fieldname": "custom_jackyun_sales_channel",
                    "label": "销售渠道",
                    "fieldtype": "Link",
                    "options": "Jackyun Sales Channel",
                    "insert_after": "customer_name",
                    "in_list_view": 1,
                    "in_standard_filter": 1,
                    "reqd": 0,
                    "mandatory_depends_on": "eval:doc.is_return != 1",
                    "read_only": 0,
                    "description": "吉客云销售出库自动匹配；手工交货单必选。退货接口无法识别原渠道时允许留空",
                },
                {
                    "fieldname": "custom_jackyun_source_trade_no",
                    "label": "来源销售单号",
                    "fieldtype": "Data",
                    "insert_after": "custom_jackyun_sales_channel",
                    "read_only": 1,
                    "in_standard_filter": 1,
                    "description": "退货单对应的原吉客云销售单 tradeNo（由售后单反查）；用于退货归因到原订单日期",
                },
                {
                    "fieldname": "custom_jackyun_source_order",
                    "label": "来源销售订单",
                    "fieldtype": "Data",
                    "insert_after": "custom_jackyun_source_trade_no",
                    "read_only": 1,
                    "in_standard_filter": 1,
                    "description": "销售出库单来源的吉客云销售单号；占位订单（天猫）通过它反查出库明细计算货品成本",
                },
            ],
            "Batch": [
                {
                    "fieldname": "custom_inspection_report_attachment",
                    "label": "检测报告附件",
                    "fieldtype": "Attach",
                    "insert_after": "expiry_date",
                    "read_only": 1,
					"in_list_view": 1,
					"in_standard_filter": 1,
                    "description": "由批次检测报告接口维护的唯一当前私有附件",
                },
            ],
        },
        update=True,
    )


def ensure_integration_indexes():
    """Keep high-volume raw/mapping joins bounded as integration history grows."""
    frappe.db.add_index(
        "Jackyun Raw Record",
        ["resource", "external_id", "creation"],
        "idx_jackyun_raw_resource_external_creation",
    )
    frappe.db.add_index(
        "External ID Mapping",
        ["resource", "external_id", "erpnext_name"],
        "idx_external_mapping_resource_external_erpnext",
    )


def backfill_sales_order_channels():
    """Backfill exact, unambiguous shop/channel-name matches for existing orders."""
    channels = frappe.get_all(
        "Jackyun Sales Channel",
        fields=["name", "channel_name", "platform_shop_name"],
    )
    candidates = {}
    ambiguous = set()
    for channel in channels:
        for label in (channel.channel_name, channel.platform_shop_name):
            label = frappe.utils.cstr(label).strip()
            if not label:
                continue
            if label in candidates and candidates[label] != channel.name:
                ambiguous.add(label)
            else:
                candidates[label] = channel.name
    for label in ambiguous:
        candidates.pop(label, None)

    for shop_name, channel in candidates.items():
        frappe.db.sql(
            """
            update `tabSales Order`
               set custom_jackyun_sales_channel = %s
             where ifnull(custom_jackyun_sales_channel, '') = ''
               and customer = %s
            """,
            (channel, f"电商客户 - {shop_name}"),
        )

    from channel_erp.integrations.jackyun import JackYunAdapter

    adapter = JackYunAdapter(None)
    missing = frappe.db.sql(
        """
        select distinct so.name, m.external_id
          from `tabSales Order` so
          join `tabExternal ID Mapping` m
            on m.resource = 'Sales Order'
           and m.external_id not like 'trade:%%'
           and m.erpnext_name = so.name
         where ifnull(so.custom_jackyun_sales_channel, '') = ''
        """,
        as_dict=True,
    )
    for row in missing:
        raw_data = frappe.db.get_value(
            "Jackyun Raw Record",
            {"resource": "Sales Order", "external_id": row.external_id},
            "raw_data",
            order_by="creation desc",
        )
        if not raw_data:
            continue
        raw = frappe.parse_json(raw_data) if isinstance(raw_data, str) else raw_data
        channel = adapter._resolve_sales_channel(
            {
                "channel_code": raw.get("channelCode"),
                "shop_id": raw.get("shopId"),
                "shop_name": raw.get("shopName"),
            }
        )
        if channel:
            frappe.db.set_value(
                "Sales Order",
                row.name,
                "custom_jackyun_sales_channel",
                channel,
                update_modified=False,
            )


def backfill_delivery_note_channels():
    """Backfill the channel directly from JackYun outbound/return payloads."""
    frappe.db.sql("set sql_big_selects=1")
    frappe.db.sql(
        """
        update `tabDelivery Note` dn
          join (
            select m.erpnext_name, max(ch.name) channel
              from `tabExternal ID Mapping` m
              join `tabJackyun Raw Record` rr
                on rr.resource = m.resource
               and rr.external_id = m.external_id
              join `tabJackyun Sales Channel` ch
                on ch.channel_code = nullif(
                    json_unquote(json_extract(rr.raw_data, '$.channelCode')), 'null'
                )
             where m.erpnext_doctype = 'Delivery Note'
               and m.resource in ('Delivery Note', 'Sales Return')
             group by m.erpnext_name
          ) source on source.erpnext_name = dn.name
           set dn.custom_jackyun_sales_channel = source.channel
         where ifnull(dn.custom_jackyun_sales_channel, '') = ''
        """
    )
    frappe.db.sql(
        """
        update `tabDelivery Note` dn
          join `tabDelivery Note Item` dni on dni.parent = dn.name
          join `tabSales Order` so on so.name = dni.against_sales_order
           set dn.custom_jackyun_sales_channel = so.custom_jackyun_sales_channel
         where ifnull(dn.custom_jackyun_sales_channel, '') = ''
           and ifnull(so.custom_jackyun_sales_channel, '') <> ''
        """
    )


def backfill_connector_sources():
    """Classify historical mapped orders before outbound automation is ever enabled."""
    if not frappe.get_meta("Sales Order").has_field("custom_connector_source"):
        return
    frappe.db.sql(
        """
        update `tabSales Order` so
          join `tabExternal ID Mapping` m
            on m.platform='jackyun'
           and m.resource='Sales Order'
           and m.erpnext_doctype='Sales Order'
           and m.erpnext_name=so.name
           set so.custom_connector_source='JackYun'
        """
    )
    frappe.db.sql(
        """
        update `tabSales Order`
           set custom_connector_source='ERPNext'
         where ifnull(custom_connector_source, '')=''
        """
    )


def remove_obsolete_custom_fields():
    for obsolete in (
        "Sales Order-custom_jackyun_customer_account",
        "Sales Order-custom_jackyun_customer_email",
        "Sales Order-custom_jackyun_customer_snapshot_section",
        "Sales Order-custom_jackyun_customer_name",
        "Sales Order-custom_jackyun_region_column",
        "Sales Order-custom_jackyun_country",
        "Sales Order-custom_jackyun_state",
        "Sales Order-custom_jackyun_city",
        "Sales Order-custom_jackyun_district",
        "Sales Order-custom_jackyun_town",
        "Sales Order-custom_jackyun_zip",
        "Sales Order-custom_jackyun_payer_section",
        "Sales Order-custom_jackyun_payer_name",
        "Sales Order-custom_jackyun_payer_phone",
        "Sales Order-custom_jackyun_payer_address",
    ):
        if frappe.db.exists("Custom Field", obsolete):
            frappe.delete_doc("Custom Field", obsolete, force=True, ignore_permissions=True)


def repair_private_workspace_sidebars():
    """Restore user-specific workspace navigation removed by standard-entity cleanup."""
    from frappe.desk.doctype.workspace_sidebar.workspace_sidebar import add_to_my_workspace

    workspaces = frappe.get_all(
        "Workspace",
        fields=["name", "title", "for_user"],
        filters={"public": 0, "for_user": ["is", "set"]},
    )
    for workspace in workspaces:
        sidebar_name = f"My Workspaces-{workspace.for_user}"
        if frappe.db.exists("Workspace Sidebar", sidebar_name):
            sidebar = frappe.get_doc("Workspace Sidebar", sidebar_name)
            if any(item.link_to == workspace.name for item in sidebar.items):
                continue
        add_to_my_workspace(frappe.get_doc("Workspace", workspace.name))


def after_migrate():
    ensure_module_defs()
    apply_module_assignments()
    repair_private_workspace_sidebars()
    ensure_custom_fields()
    backfill_connector_sources()
    ensure_integration_indexes()
    ensure_integration_management_metadata()
    ensure_generic_connector_connections()
    ensure_connector_navigation()
    remove_obsolete_custom_fields()
    backfill_sales_order_channels()
    backfill_sales_order_times()
    backfill_sales_order_trade_types()
    backfill_delivery_note_channels()
    drop_content_center_company_columns()
    from channel_erp.content_center.simple_api import _root_folder
    _root_folder()


def ensure_generic_connector_connections():
    """Create capability records without copying or exposing platform secrets."""
    if not frappe.db.exists("DocType", "Connector Connection"):
        return
    for legacy in frappe.get_all("Jackyun Connection", fields=["name", "enabled"]):
        name = f"JackYun - {legacy.name}"
        values = {
            "enabled": legacy.enabled,
            "platform": "jackyun",
            "adapter_key": "jackyun",
            "credential_doctype": "Jackyun Connection",
            "credential_name": legacy.name,
            "source_marker": "erpnext",
            "allow_create": 1,
            "allow_update": 0,
            "allow_cancel": 1,
            "allow_audit": 1,
            "allow_query": 1,
        }
        if frappe.db.exists("Connector Connection", name):
            frappe.db.set_value("Connector Connection", name, values, update_modified=False)
        else:
            frappe.get_doc({
                "doctype": "Connector Connection",
                "connection_name": name,
                **values,
            }).insert(ignore_permissions=True)


def ensure_connector_navigation():
    """Keep generic governance pages visible even on previously customized desks."""
    targets = (
        ("接口连接器", "Connector Connection"),
        ("吉客云凭据与写入设置", "Jackyun Connection"),
        ("数据核对与预演", "Integration Reconciliation Run"),
        ("推送队列", "Connector Outbound Message"),
    )
    if frappe.db.exists("Workspace", "Channel Erp"):
        workspace = frappe.get_doc("Workspace", "Channel Erp")
        existing = {row.link_to: row for row in workspace.links or []}
        changed = False
        for label, link_to in targets:
            if link_to in existing:
                if existing[link_to].label != label:
                    existing[link_to].label = label
                    changed = True
                continue
            workspace.append("links", {
                "type": "Link", "label": label, "link_type": "DocType",
                "link_to": link_to, "hidden": 0,
            })
            changed = True
        if changed:
            workspace.save(ignore_permissions=True)

    if frappe.db.exists("Workspace Sidebar", "Channel Erp"):
        sidebar = frappe.get_doc("Workspace Sidebar", "Channel Erp")
        changed = False
        jackyun_rows = [row for row in sidebar.items or [] if row.link_to == "Jackyun Connection"]
        if jackyun_rows:
            if jackyun_rows[0].label != "吉客云凭据与写入设置":
                jackyun_rows[0].label = "吉客云凭据与写入设置"
                changed = True
            if len(jackyun_rows) > 1:
                jackyun_rows[1].label = "同步计划"
                jackyun_rows[1].link_to = "Jackyun Sync Schedule"
                changed = True
        else:
            sidebar.append("items", {
                "type": "Link", "label": "吉客云凭据与写入设置",
                "link_type": "DocType", "link_to": "Jackyun Connection", "child": 1,
            })
            changed = True
        connector_rows = [row for row in sidebar.items or [] if row.link_to == "Connector Connection"]
        if connector_rows:
            if connector_rows[0].label != "接口连接器":
                connector_rows[0].label = "接口连接器"
                changed = True
        else:
            sidebar.append("items", {
                "type": "Link", "label": "接口连接器",
                "link_type": "DocType", "link_to": "Connector Connection", "child": 1,
            })
            changed = True
        if changed:
            sidebar.save(ignore_permissions=True)
    if frappe.db.exists("Workspace Sidebar", "Channel Erp"):
        sidebar = frappe.get_doc("Workspace Sidebar", "Channel Erp")
        existing = {row.link_to for row in sidebar.items or []}
        changed = False
        for label, link_to in targets:
            if link_to in existing:
                continue
            sidebar.append("items", {
                "type": "Link", "label": label, "link_type": "DocType",
                "link_to": link_to, "child": 1, "collapsible": 1,
                "indent": 0, "keep_closed": 0, "show_arrow": 0,
            })
            changed = True
        if changed:
            sidebar.save(ignore_permissions=True)


def ensure_integration_management_metadata():
    """Seed read-only mapping documentation and align statuses on historical raw rows."""
    if frappe.db.exists("DocType", "Integration Field Mapping"):
        expected_keys = set()
        for row in FIELD_MAPPING_CATALOG:
            source_field = " / ".join(row["source_fields"])
            mapping_key = f"{row['platform']}|{row['resource']}|{row['source_fields'][0]}"
            expected_keys.add(mapping_key)
            values = {
                "platform": row["platform"],
                "resource": row["resource"],
                "source_field": source_field,
                "classification": row["classification"],
                "sync_direction": row["sync_direction"],
                "required": row["required"],
                "key_role": row["key_role"],
                "data_type": row["data_type"],
                "target_doctype": row["target_doctype"],
                "target_field": row["target_field"],
                "transformation": row["transformation"],
                "null_policy": row["null_policy"],
                "update_policy": row["update_policy"],
                "source_of_truth": row["source_of_truth"],
                "sensitive_level": row["sensitive_level"],
                "api_method_or_version": row["api_method_or_version"],
            }
            if frappe.db.exists("Integration Field Mapping", mapping_key):
                frappe.db.set_value(
                    "Integration Field Mapping", mapping_key, values, update_modified=False
                )
            else:
                frappe.get_doc(
                    {
                        "doctype": "Integration Field Mapping",
                        "mapping_key": mapping_key,
                        **values,
                    }
                ).insert(ignore_permissions=True)
        stale_names = set(
            frappe.get_all(
                "Integration Field Mapping",
                filters={"platform": "jackyun"},
                pluck="name",
            )
        ) - expected_keys
        for name in stale_names:
            frappe.delete_doc(
                "Integration Field Mapping", name, ignore_permissions=True, force=True
            )

    if frappe.db.has_column("Jackyun Raw Record", "processing_status"):
        # This table can contain close to a million payloads and workers may still
        # append records while migrate runs.  Small committed batches avoid one
        # long-running update conflicting with a live sync transaction.
        last_name = ""
        while True:
            names = frappe.db.sql(
                """
                select name
                  from `tabJackyun Raw Record`
                 where name>%s
                 order by name
                 limit 10000
                """,
                (last_name,),
                pluck=True,
            )
            if not names:
                break
            frappe.db.sql(
                """
                update `tabJackyun Raw Record`
                   set processing_status='Succeeded'
                 where name between %s and %s
                   and processed=1
                   and (processing_status is null or processing_status='' or processing_status='Pending')
                """,
                (names[0], names[-1]),
            )
            last_name = names[-1]
            frappe.db.commit()


def backfill_sales_order_times():
    """Backfill the precise JackYun tradeTime on already synchronized orders."""
    if not frappe.db.has_column("Sales Order", "custom_jackyun_order_time"):
        return
    frappe.db.sql(
        """
        update `tabSales Order` so
        join `tabExternal ID Mapping` map
          on map.platform='jackyun'
         and map.resource='Sales Order'
         and map.erpnext_doctype='Sales Order'
         and map.erpnext_name=so.name
         and map.external_id not like 'trade:%%'
        join (
              select rr.external_id,
                     coalesce(
                         nullif(json_unquote(json_extract(rr.raw_data, '$.tradeTime')), 'null'),
                         nullif(json_unquote(json_extract(rr.raw_data, '$.gmtCreate')), 'null')
                     ) trade_time
                from `tabJackyun Raw Record` rr
                join (
                      select external_id, max(creation) creation
                        from `tabJackyun Raw Record`
                       where resource='Sales Order'
                       group by external_id
                ) newest
                  on newest.external_id=rr.external_id
                 and newest.creation=rr.creation
               where rr.resource='Sales Order'
        ) latest on latest.external_id=map.external_id
           set so.custom_jackyun_order_time=latest.trade_time
         where latest.trade_time is not null
           and (
                so.custom_jackyun_order_time is null
             or so.custom_jackyun_order_time<>latest.trade_time
           )
        """
    )


def backfill_sales_order_trade_types():
    """Populate the order-owned retail/wholesale classification from JackYun."""
    if not frappe.db.has_column("Sales Order", "custom_jackyun_trade_type"):
        return
    frappe.db.sql(
        """
        update `tabSales Order` so
        join `tabExternal ID Mapping` map
          on map.platform='jackyun'
         and map.resource='Sales Order'
         and map.erpnext_doctype='Sales Order'
         and map.erpnext_name=so.name
        join `tabJackyun Raw Record` rr
          on rr.resource='Sales Order'
         and rr.external_id=map.external_id
           set so.custom_jackyun_trade_type = case
               when json_unquote(json_extract(rr.raw_data, '$.tradeType'))='1' then '零售业务'
               when json_unquote(json_extract(rr.raw_data, '$.tradeType'))='9' then '批发业务'
               else so.custom_jackyun_trade_type
           end
         where json_unquote(json_extract(rr.raw_data, '$.tradeType')) in ('1','9')
        """
    )


def drop_content_center_company_columns():
    """Remove the retired company dimension after DocType synchronization."""
    database = frappe.conf.db_name
    for doctype in CONTENT_CENTER_DOCTYPES:
        table = f"tab{doctype}"
        if not frappe.db.table_exists(doctype):
            continue
        columns = frappe.db.sql(
            """select column_name from information_schema.columns
                 where table_schema=%s and table_name=%s and column_name='company'""",
            (database, table),
        )
        if not columns:
            continue
        indexes = frappe.db.sql(
            """select distinct index_name from information_schema.statistics
                 where table_schema=%s and table_name=%s and column_name='company'
                   and index_name <> 'PRIMARY'""",
            (database, table),
            pluck=True,
        )
        for index_name in indexes:
            frappe.db.sql_ddl(f"alter table `{table}` drop index `{index_name}`")
        frappe.db.sql_ddl(f"alter table `{table}` drop column `company`")

    if frappe.db.has_column("Jackyun Raw Record", "failure_category"):
        from channel_erp.integrations.exception_classifier import classify_exception

        failures = frappe.db.sql(
            """
            select name, error_message
              from `tabJackyun Raw Record`
             where processing_status='Failed'
               and ifnull(failure_category, '')=''
            """,
            as_dict=True,
        )
        for failure in failures:
            frappe.db.set_value(
                "Jackyun Raw Record",
                failure.name,
                classify_exception(failure.error_message),
                update_modified=False,
            )

    _backfill_resource_health_summaries()


def _backfill_resource_health_summaries():
    if not frappe.db.has_column("Jackyun Sync Schedule", "last_status"):
        return
    for connection_name in frappe.get_all("Jackyun Connection", pluck="name"):
        connection = frappe.get_doc("Jackyun Connection", connection_name)
        changed = False
        for schedule in connection.get("sync_schedules") or []:
            if schedule.last_status:
                continue
            logs = frappe.get_all(
                "Jackyun Sync Log",
                filters={"connection": connection_name, "resource": schedule.resource},
                fields=["status", "finished_at"],
                order_by="finished_at desc",
                limit=100,
            )
            if not logs:
                continue
            schedule.last_status = logs[0].status
            schedule.last_success_at = next(
                (row.finished_at for row in logs if row.status == "成功"), None
            )
            schedule.consecutive_failures = next(
                (index for index, row in enumerate(logs) if row.status == "成功"),
                len(logs),
            )
            interval = max(frappe.utils.cint(schedule.interval_minutes), 30)
            base = schedule.last_enqueued_at or logs[0].finished_at
            schedule.next_run_at = frappe.utils.add_to_date(base, minutes=interval)
            changed = True
        if changed:
            connection.save(ignore_permissions=True)


def reset_content_center():
    """Permanently reset Content Center data without touching ERP or Batch files."""
    batch_urls = set()
    if frappe.db.has_column("Batch", "custom_inspection_report_attachment"):
        batch_urls = set(filter(None, frappe.get_all(
            "Batch", pluck="custom_inspection_report_attachment", limit=0,
        )))
    file_names = set(frappe.get_all(
        "File", filters={"attached_to_doctype": "Channel Content Asset"}, pluck="name", limit=0,
    ))
    if frappe.db.table_exists("Channel Content Asset Version"):
        file_names.update(frappe.get_all("Channel Content Asset Version", pluck="file", limit=0))

    children = [doctype for doctype in CONTENT_CENTER_DOCTYPES if doctype not in {"Channel Content Folder", "Channel Content Asset", "Channel Content Settings"}]
    for doctype in children:
        if frappe.db.table_exists(doctype):
            frappe.db.delete(doctype)
    deleted_files = 0
    preserved_batch_files = 0
    for name in file_names:
        if not name or not frappe.db.exists("File", name):
            continue
        file_doc = frappe.get_doc("File", name)
        if file_doc.file_url in batch_urls:
            frappe.db.set_value("File", name, {"attached_to_doctype": None, "attached_to_name": None, "attached_to_field": None}, update_modified=False)
            preserved_batch_files += 1
            continue
        file_doc.flags.allow_content_center_delete = True
        file_doc.delete(ignore_permissions=True)
        deleted_files += 1
    if frappe.db.table_exists("Channel Content Asset"):
        frappe.db.delete("Channel Content Asset")
    if frappe.db.table_exists("Channel Content Folder Access"):
        frappe.db.delete("Channel Content Folder Access")
    if frappe.db.table_exists("Channel Content Folder"):
        frappe.db.delete("Channel Content Folder")
    frappe.db.delete("Singles", {"doctype": "Channel Content Settings"})

    for doctype in OBSOLETE_CONTENT_CENTER_DOCTYPES:
        if frappe.db.exists("DocType", doctype):
            frappe.delete_doc("DocType", doctype, force=True, ignore_permissions=True)
    # Development reset: remove obsolete physical tables as well. Frappe keeps
    # tables after deleting DocType metadata to protect production data, but this
    # reset is explicitly destructive and leaves no historical company/business
    # attachment schema behind.
    for doctype in OBSOLETE_CONTENT_CENTER_DOCTYPES:
        table = f"tab{doctype}"
        frappe.db.sql_ddl(f"drop table if exists `{table}`")
    frappe.db.commit()
    return {"deleted_files": deleted_files, "preserved_batch_files": preserved_batch_files}
