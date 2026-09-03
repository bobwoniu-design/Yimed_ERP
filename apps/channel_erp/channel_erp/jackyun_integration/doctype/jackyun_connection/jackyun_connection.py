import frappe
from frappe.model.document import Document


class JackyunConnection(Document):
    def validate(self):
        from channel_erp.integrations.tasks import SUPPORTED_SYNC_INTERVALS

        get = self.get if hasattr(self, "get") else lambda key: getattr(self, key, None)
        if frappe.utils.cint(get("outbound_enabled")) and not get("outbound_enabled_at"):
            # This cutoff prevents deployment or a later toggle from back-pushing
            # historical ERPNext orders.
            self.outbound_enabled_at = frappe.utils.now_datetime()
        if frappe.utils.cint(get("outbound_enabled")) and not get("default_cancel_reason"):
            frappe.throw("启用吉客云写入前必须配置默认取消原因编码")
        if frappe.utils.cint(get("auto_audit_outbound")) and not get("audit_operator"):
            frappe.throw("启用自动审核前必须配置吉客云审核操作员")

        if not self.sync_schedules:
            self.set_default_sync_schedules()
        seen = set()
        for row in self.sync_schedules:
            if row.resource in seen:
                frappe.throw(f"同步类型 {row.resource} 只能配置一次")
            seen.add(row.resource)
            interval = frappe.utils.cint(row.interval_minutes)
            if interval not in SUPPORTED_SYNC_INTERVALS:
                allowed = "/".join(str(value) for value in sorted(SUPPORTED_SYNC_INTERVALS))
                frappe.throw(
                    f"同步类型 {row.resource} 的间隔只支持 {allowed} 分钟"
                )
            direction = row.get("direction") or "Pull Only"
            if direction not in {"Pull Only", "Push Only", "Bidirectional", "Disabled"}:
                frappe.throw(f"同步类型 {row.resource} 的同步方向无效")

    def set_default_sync_schedules(self, now=None):
        from channel_erp.integrations.tasks import DEFAULT_SYNC_INTERVALS, SYNC_RESOURCE_ORDER

        for resource in SYNC_RESOURCE_ORDER:
            interval = DEFAULT_SYNC_INTERVALS.get(resource, 360)
            self.append(
                "sync_schedules",
                {
                    "resource": resource,
                    "enabled": 1,
                    "direction": "Pull Only",
                    "interval_minutes": interval,
                    "last_enqueued_at": now,
                    "next_run_at": (
                        frappe.utils.add_to_date(now, minutes=interval)
                        if now else None
                    ),
                },
            )


@frappe.whitelist()
def test_connection(name, app_key=None, app_secret=None, gateway=None):
    """实时调用吉客云只读接口，校验网关与 AppKey/AppSecret 签名。

    返回 {"ok": bool, "message": str}；复用适配器现有 request()，不新建请求逻辑。
    """
    from channel_erp.integrations.adapter_registry import get_adapter
    from channel_erp.integrations.jackyun import JikeyunError, METHOD_MAP

    connection = frappe.get_doc("Jackyun Connection", name)
    # 用表单当前值覆盖（可能未保存）；app_secret 为占位符时保留已存密钥
    if app_key:
        connection.app_key = app_key
    if gateway:
        connection.gateway = gateway
    if app_secret and not connection.is_dummy_password(app_secret):
        connection.app_secret = app_secret

    try:
        get_adapter(connection).request(METHOD_MAP["Company"], {})
    except JikeyunError as exc:
        return {"ok": False, "message": _test_connection_failure_message(exc)}
    except Exception as exc:
        return {"ok": False, "message": f"连接失败：{exc}"}
    return {"ok": True, "message": "连接成功：网关可达，AppKey/AppSecret 与签名校验通过"}


def _test_connection_failure_message(exc):
    category = getattr(exc, "category", "")
    label = {
        "SIGNATURE": "签名错误",
        "UNSUBSCRIBED": "接口未订阅",
        "PARAMETER": "参数错误",
        "NETWORK": "网络超时",
    }.get(category)
    return f"{label}：{exc}" if label else str(exc)
