import frappe
from frappe.model.document import Document

from channel_erp.integrations.connector_contracts import OutboundStatus, normalize_operation


class ConnectorOutboundMessage(Document):
    def validate(self):
        if not self.connector_connection and not self.connection:
            frappe.throw("通用连接与兼容连接至少填写一个")
        normalize_operation(self.operation)
        try:
            OutboundStatus(str(self.status))
        except ValueError:
            frappe.throw(f"未知出站状态：{self.status}")
        if not isinstance(frappe.parse_json(self.payload), dict):
            frappe.throw("业务报文必须为 JSON 对象")
