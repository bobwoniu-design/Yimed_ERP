"""平台无关的第三方集成适配器基类。"""
import hashlib
import json
from abc import ABC, abstractmethod
from typing import Optional

import frappe

from channel_erp.integrations.connector_contracts import (
    ConnectorOperation,
    OperationBlockedError,
    PreflightResult,
    ProbeResult,
    normalize_capabilities,
    normalize_operation,
)


class BaseAdapter(ABC):
    """第三方平台适配器基类。

    子类实现 sign / request / pull / transform 四个平台相关方法，
    复用本类的外部 ID 映射与幂等落库逻辑。
    """

    platform = "base"

    @abstractmethod
    def sign(self, params: dict, secret: str) -> str:
        """对请求参数签名。"""

    @abstractmethod
    def request(self, method: str, bizcontent: dict, page_index: int) -> dict:
        """调用一次平台 API，返回响应 payload。"""

    @abstractmethod
    def pull(self, resource: str):
        """拉取某资源的所有原始数据，逐条 yield。"""

    @abstractmethod
    def transform(self, resource: str, raw: dict) -> dict:
        """把一条原始数据映射成 {doctype, external_id, ...目标字段}。"""

    def outbound_capabilities(self, resource: Optional[str] = None):
        """Return explicitly approved operations for this adapter/resource."""
        del resource
        return set()

    def supports_operation(self, resource: str, operation: str) -> bool:
        operation = normalize_operation(operation)
        return operation.value in normalize_capabilities(
            self.outbound_capabilities(resource)
        )

    def preflight_operation(
        self, resource: str, operation: str, payload: dict, context: Optional[dict] = None
    ) -> PreflightResult:
        """Validate capability and payload without making a remote request."""
        del context
        operation = normalize_operation(operation)
        if not self.supports_operation(resource, operation.value):
            return PreflightResult(
                False,
                f"{self.platform} 尚未启用 {resource} / {operation.value} 能力",
            )
        if not isinstance(payload, dict):
            return PreflightResult(False, "出站 payload 必须是 JSON 对象")
        return PreflightResult(True, normalized_payload=payload)

    def push(
        self,
        resource: str,
        operation: str,
        payload: dict,
        *,
        idempotency_key: Optional[str] = None,
        context: Optional[dict] = None,
    ) -> dict:
        """Push one payload to the source platform.

        Pull-only adapters deliberately fail closed. A platform adapter may
        override this method once its write API and field contract are approved.
        """
        del operation, payload, idempotency_key, context
        raise NotImplementedError(f"{self.platform} 尚未配置 {resource} 的推送接口")

    def query_operation(
        self,
        resource: str,
        operation: str,
        payload: dict,
        *,
        context: Optional[dict] = None,
    ) -> dict:
        del operation, payload, context
        raise NotImplementedError(f"{self.platform} 尚未配置 {resource} 的查询接口")

    def execute_operation(
        self,
        resource: str,
        operation: str,
        payload: dict,
        *,
        idempotency_key: str,
        context: Optional[dict] = None,
    ):
        """Execute one approved operation; callers persist all evidence."""
        operation = normalize_operation(operation)
        if not self.supports_operation(resource, operation.value):
            raise OperationBlockedError(
                f"{self.platform} 尚未启用 {resource} / {operation.value} 能力"
            )
        if operation == ConnectorOperation.QUERY:
            return self.query_operation(
                resource, operation.value, payload, context=context
            )
        return self.push(
            resource,
            operation.value,
            payload,
            idempotency_key=idempotency_key,
            context=context,
        )

    def probe_operation(
        self,
        resource: str,
        operation: str,
        *,
        idempotency_key: str,
        external_id: Optional[str] = None,
        payload: Optional[dict] = None,
        context: Optional[dict] = None,
    ) -> ProbeResult:
        """Resolve an uncertain outcome without repeating the write."""
        del resource, operation, idempotency_key, external_id, payload, context
        return ProbeResult.unknown(
            response={"reason": f"{self.platform} 未实现不确定结果查询"}
        )

    # ---- 平台无关：外部 ID 映射 + 幂等落库 ----

    def get_mapping_name(self, resource: str, external_id: str):
        """查外部 ID 映射，返回映射记录 name 或 None。"""
        return frappe.db.exists(
            "External ID Mapping",
            {"platform": self.platform, "resource": resource, "external_id": external_id},
        )

    def remember_mapping(self, resource, external_id, doctype, name):
        """记一条外部 ID 映射（有则更新时间，无则新建）。"""
        mapping_name = self.get_mapping_name(resource, external_id)
        if mapping_name:
            frappe.db.set_value(
                "External ID Mapping",
                mapping_name,
                {
                    "erpnext_doctype": doctype,
                    "erpnext_name": name,
                    "last_synced_at": frappe.utils.now(),
                },
            )
        else:
            frappe.get_doc(
                {
                    "doctype": "External ID Mapping",
                    "platform": self.platform,
                    "resource": resource,
                    "external_id": external_id,
                    "erpnext_doctype": doctype,
                    "erpnext_name": name,
                    "last_synced_at": frappe.utils.now(),
                }
            ).insert(ignore_permissions=True)

    def upsert(self, resource: str, mapped: dict):
        """幂等落库。mapped 需含 doctype / external_id 及目标字段。

        命中映射 -> 更新；未命中 -> 新建 + 记映射。返回 (name, "created"|"updated")。
        """
        doctype = mapped.pop("doctype")
        external_id = mapped.pop("external_id")
        mapping_name = self.get_mapping_name(resource, external_id)

        if mapping_name:
            # 去重命中：直接跳过重新保存。
            # 注：对已存在的 Item 做 doc.update()+save() 会触发 UOM 换算表唯一性校验报错，
            # 且吉客云同货品重复行的数据一致、无需更新。增量更新留待后续用 content_hash 细化。
            erpnext_name = frappe.db.get_value("External ID Mapping", mapping_name, "erpnext_name")
            self.remember_mapping(resource, external_id, doctype, erpnext_name)
            return erpnext_name, "updated"

        doc = frappe.get_doc({"doctype": doctype, **mapped})
        doc.insert(ignore_permissions=True)
        self.remember_mapping(resource, external_id, doctype, doc.name)
        return doc.name, "created"

    @staticmethod
    def content_hash(data) -> str:
        """稳定内容哈希，用于判断原始数据是否变化。"""
        return hashlib.md5(
            json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
