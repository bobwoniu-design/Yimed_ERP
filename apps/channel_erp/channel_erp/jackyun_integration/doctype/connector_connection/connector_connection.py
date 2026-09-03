import frappe
from frappe.model.document import Document


class ConnectorConnection(Document):
    def validate(self):
        self.platform = frappe.utils.cstr(self.platform).strip().lower()
        self.adapter_key = (
            frappe.utils.cstr(self.adapter_key).strip().lower() or self.platform
        )
        if not self.platform:
            frappe.throw("平台标识不能为空")
        if bool(self.credential_doctype) != bool(self.credential_name):
            frappe.throw("凭据 DocType 与凭据记录必须同时填写")

    def enabled_operations(self):
        return {
            operation
            for operation, fieldname in {
                "Create": "allow_create",
                "Update": "allow_update",
                "Cancel": "allow_cancel",
                "Audit": "allow_audit",
                "Query": "allow_query",
            }.items()
            if frappe.utils.cint(self.get(fieldname))
        }

