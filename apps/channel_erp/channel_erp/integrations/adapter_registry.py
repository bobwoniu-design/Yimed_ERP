"""第三方平台适配器注册表：按连接的平台类型返回对应适配器。

后续接入新平台（如旺店通）时：
1. 新建 XxxAdapter(BaseAdapter)，实现 sign/request/pull/transform；
2. 在本文件注册一行；即可被 tasks 主流程复用，无需改动同步逻辑。
"""
import frappe

from channel_erp.integrations.base import BaseAdapter
from channel_erp.integrations.jackyun import JackYunAdapter

# 平台标识 -> 适配器类。平台标识来自连接配置，缺省为 jackyun。
ADAPTER_REGISTRY = {
    "jackyun": JackYunAdapter,
    "jikeyun": JackYunAdapter,  # 吉客云历史别名
}


def get_adapter(connection) -> BaseAdapter:
    """根据连接返回适配器实例。

    connection 可带 platform 字段（未同步时回退 jackyun），
    兼容现有所有连接的读取方式。
    """
    platform = None
    if hasattr(connection, "get"):
        platform = connection.get("platform")
    if not platform and hasattr(connection, "platform"):
        platform = getattr(connection, "platform", None)
    platform = platform or "jackyun"

    adapter_class = ADAPTER_REGISTRY.get(platform)
    if not adapter_class:
        raise NotImplementedError(
            f"不支持的平台类型 {platform}，请在 adapter_registry.py 注册"
        )
    return adapter_class(connection)
