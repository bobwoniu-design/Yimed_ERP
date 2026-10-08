# -*- coding: utf-8 -*-
"""补丁：让出库类单据（Delivery Note / Sales Return 等 Selling 单据）的批次负库存
校验尊重全局 Stock Settings.allow_negative_stock 开关。

上游行为（erpnext v16）：SellingController.update_stock_ledger 的
allow_negative_stock 参数默认 False，Delivery Note 提交时不传，
导致批次数量不足时抛 BatchNegativeStockError——即使全局开关已打开。
（Stock Entry 路径正常：提交时自行读取 is_negative_stock_allowed。）

吉客云同步场景依赖此开关：出库单可能引用快照外批次（负库存需先过账、
后续入库单回补），与吉客云侧允许负库存的业务一致。
"""

_PATCHED = False


def apply_stock_patch():
    global _PATCHED
    if _PATCHED:
        return
    try:
        from erpnext.controllers import selling_controller
        from erpnext.stock.stock_ledger import is_negative_stock_allowed

        original = selling_controller.SellingController.update_stock_ledger

        def update_stock_ledger(self, allow_negative_stock=False, *args, **kwargs):
            if not allow_negative_stock and is_negative_stock_allowed():
                allow_negative_stock = True
            return original(self, allow_negative_stock, *args, **kwargs)

        selling_controller.SellingController.update_stock_ledger = update_stock_ledger
        _PATCHED = True
    except Exception:
        import frappe
        frappe.logger("channel_erp.patch").exception("selling controller patch failed")


def before_request():
    apply_stock_patch()


def before_job():
    apply_stock_patch()
