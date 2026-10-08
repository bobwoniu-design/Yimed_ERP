"""Deterministic first-pass classification for connector failures.

The rules use only persisted exception text.  Operators may correct the
classification and suggestion on the Raw Record; no automatic business action
is executed from these labels.
"""

import re


FAILURE_CATEGORIES = (
    "接口/鉴权",
    "缺少主数据",
    "映射",
    "ERP校验",
    "库存/批次",
    "关联单据",
    "未知",
)


def classify_exception(error):
    message = str(error or "").strip()
    lowered = message.lower()

    if _matches(
        lowered,
        r"signature|签名|token|授权|未订阅|appkey|appsecret|http\s*\d+|"
        r"network|timeout|timed out|connection|接口.*失败|请求重试|限流|429",
    ):
        authentication = _matches(
            lowered, r"signature|签名|token|授权|未订阅|appkey|appsecret"
        )
        return _result(
            "接口/鉴权",
            not authentication,
            "检查接口授权、密钥和订阅；网络或限流错误可稍后重试。",
        )

    if _matches(
        lowered,
        r"数量不可为零|quantity.*(?:cannot|must not).*zero|"
        r"qty.*(?:cannot|can not|must not).*zero|"
        r"相同的商品和仓库组合",
    ):
        return _result(
            "映射",
            True,
            "连接器应过滤零数量行或合并相同商品与仓库，部署修复后可重试。",
        )

    if _matches(
        lowered,
        r"负库存|库存不足|库存流水|stock ledger|"
        r"available qty.*less than|required qty|negative stock|"
        r"batch.*negative|batch no.*negative|批次.*负库存",
    ):
        return _result(
            "库存/批次",
            False,
            "依赖真实库存和批次切换；完成库存基准后再人工允许重试，不得伪造库存。",
        )

    if _matches(lowered, r"batch|批次|serial|序列号|唯一码"):
        return _result(
            "库存/批次",
            True,
            "检查批次或唯一码主数据，修复后从原始记录重试。",
        )

    if _matches(
        lowered,
        r"不能链接到已取消|链接到已取消单据|已取消单据行|已取消.*订单|"
        r"cancelled document|against.*cancelled",
    ):
        return _result(
            "关联单据",
            False,
            "来源单据已在吉客云或ERPNext取消，业务上不可自愈；请人工确认是否需要手工建单，"
            "无需重试。",
        )

    if _matches(
        lowered,
        r"来源.*单|关联.*单|source.*order|against.*order|找不到.*原.*单|"
        r"未找到.*订单",
    ):
        return _result(
            "关联单据",
            True,
            "先同步或修复来源单据及外部ID关联，再重试当前记录。",
        )

    if _matches(
        lowered,
        r"尚未同步|尚未映射|缺少有效仓库|缺少仓库|缺少供应商|缺少客户|"
        r"缺少商品|商品.*未同步|公司.*未同步|未找到可映射|missing master",
    ):
        return _result(
            "缺少主数据",
            True,
            "先同步或补齐公司、商品、仓库、客户/供应商等主数据，再重试。",
        )

    if _matches(
        lowered,
        r"外部id|映射|mapping|已被.*占用|重复|duplicate|冲突|business key",
    ):
        return _result(
            "映射",
            True,
            "检查外部ID映射和业务键是否指向正确ERPNext单据，修复后重试。",
        )

    if _matches(
        lowered,
        r"validationerror|mandatoryerror|linkvalidationerror|校验|必填|mandatory|"
        r"不能提交|提交失败|不能取消|erpnext|does not exist|not permitted|invalid",
    ):
        return _result(
            "ERP校验",
            False,
            "检查ERPNext必填项、单据状态和业务校验；确认修复后再手工重试。",
        )

    return _result(
        "未知",
        False,
        "查看完整错误和原始JSON，确认原因后人工调整分类与重试策略。",
    )


def _matches(value, pattern):
    return bool(re.search(pattern, value, flags=re.IGNORECASE))


def _result(category, retryable, suggestion):
    return {
        "failure_category": category,
        "retryable": 1 if retryable else 0,
        "handling_suggestion": suggestion,
    }
