"""Read-only documentation catalog for fields handled by installed adapters.

The catalog deliberately documents fields that are retained but not written to
ERPNext.  It is not an executable mapping language: transformations remain in
the reviewed adapter code.
"""

ERP_FIELD = "ERPNext字段"
SYNC_CONTROL = "同步控制"
RAW_ONLY = "原始记录保留"
NOT_USED = "未使用/不适用"

API_METHODS = {
    "Company": "erp.company.query v1.0",
    "Department": "erp.depart.query v1.0",
    "Category": "erp.goodscate.get v1.0",
    "Sales Channel": "erp.sales.get v1.0",
    "Supplier": "erp.vend.get v1.0",
    "Offline Customer": "crm.customer.list.customized v1.0",
    "Warehouse": "erp.warehouse.get v1.0",
    "SKU": "erp.storage.goodslist v1.0",
    "Inventory": "erp.stockquantity.get v1.0",
    "Product Bundle": "erp-goods.goods.listgoodspackage v1.0",
    "Batch": "erp.goodsbatchinfo.get v1.0",
    "Purchase Order": "erp.purch.search v1.0",
    "Purchase Receipt": "erp-busiorder.goodsdocin.search v1.0",
    "Purchase Return": "erp-busiorder.goodsdocout.search v1.0",
    "Sales Order": "oms.trade.fullinfoget v1.0",
    "Delivery Note": "erp-busiorder.goodsdocout.search v1.0",
    "Sales Return": "erp-busiorder.goodsdocin.search v1.0",
    "Stock Transfer": "erp.allocate.search v1.0",
    "Stock Movement": "erp-busiorder.goodsdocin/goodsdocout.search v1.0",
    "Stocktake": "wms.stocktake.get v1.0",
}

FIELD_MAPPING_CATALOG = []

SYNC_RESOURCES = (
    "Company", "Department", "Category", "Sales Channel", "Supplier",
    "Offline Customer", "Warehouse", "SKU", "Inventory", "Product Bundle",
    "Batch", "Purchase Order", "Purchase Receipt", "Purchase Return",
    "Sales Order", "Delivery Note", "Sales Return", "Stock Transfer",
    "Stock Movement", "Stocktake",
)


def _field_governance(resource, source_fields, classification, note):
    joined = " ".join(source_fields).lower()
    external_key = "外部id" in note.lower() or "幂等" in note.lower()
    business_key = "业务键" in note or "作为erpnext编号" in note.lower()
    if external_key:
        key_role = "外部ID"
    elif business_key:
        key_role = "业务键"
    elif classification == SYNC_CONTROL:
        key_role = "控制字段"
    else:
        key_role = "普通字段"

    if any(token in joined for token in ("time", "date", "gmt", "modified")):
        data_type = "Datetime"
    elif any(token in joined for token in ("quantity", "amount", "price", "rate", "fee", "count")):
        data_type = "Decimal"
    elif any(token in joined for token in ("isdelete", "isblockup", "disabled", "enable")):
        data_type = "Boolean"
    elif "detail" in joined or "list" in joined or len(source_fields) > 8:
        data_type = "Mixed/JSON"
    else:
        data_type = "String/Number"

    required = 1 if key_role in {"外部ID", "业务键"} else 0
    if classification == SYNC_CONTROL:
        null_policy = "使用连接配置或适配器默认值"
        update_policy = "不直接落库"
        sync_direction = "不适用"
        source_of_truth = "连接配置/吉客云响应"
    elif classification == RAW_ONLY:
        null_policy = "允许为空并原样保留"
        update_policy = "随原始记录追加，不覆盖ERPNext"
        sync_direction = "拉取"
        source_of_truth = "吉客云"
    elif classification == NOT_USED:
        null_policy = "忽略空值"
        update_policy = "不落库，仅原始记录留档"
        sync_direction = "不适用"
        source_of_truth = "吉客云"
    else:
        null_policy = (
            "缺失则本条失败" if required else
            ("候选字段依次回退" if any(word in note for word in ("优先", "回退", "首个")) else "允许为空，目标字段保持默认")
        )
        update_policy = "幂等更新；已提交业务单据受状态保护"
        sync_direction = "拉取"
        source_of_truth = "吉客云"

    sensitive_level = "普通"
    if any(token in joined for token in ("phone", "tel", "email", "address", "payer", "account")):
        sensitive_level = "敏感"
    elif any(token in joined for token in ("tax", "bank", "website", "customername", "vendname")):
        sensitive_level = "内部"

    return {
        "sync_direction": sync_direction,
        "required": required,
        "key_role": key_role,
        "data_type": data_type,
        "null_policy": null_policy,
        "update_policy": update_policy,
        "source_of_truth": source_of_truth,
        "sensitive_level": sensitive_level,
        "api_method_or_version": API_METHODS[resource],
    }


def _add(resource, fields, classification, target_doctype="", target_field="", note=""):
    resources = (resource,) if isinstance(resource, str) else resource
    source_fields = (fields,) if isinstance(fields, str) else tuple(fields)
    for resource_name in resources:
        governance = _field_governance(
            resource_name, source_fields, classification, note
        )
        FIELD_MAPPING_CATALOG.append(
            {
                "platform": "jackyun",
                "resource": resource_name,
                "source_fields": source_fields,
                "classification": classification,
                "target_doctype": target_doctype,
                "target_field": target_field,
                "transformation": note,
                **governance,
            }
        )


# Master data
_add("Company", ("companyId", "companyCode"), SYNC_CONTROL, note="外部ID及公司编码映射键")
_add("Company", "companyName", ERP_FIELD, "Company", "name / company_name", "匹配现有公司，名称不一致时安全重命名")
_add("Company", "currencyCode", RAW_ONLY, note="当前公司以既有ERPNext币种配置为准")
_add("Company", ("groupId", "groupName", "busiBrand", "openingBank", "fax", "tel", "currencyName", "industryCode", "industryName"), RAW_ONLY, note="接口返回并完整保存在原始记录，当前不覆盖ERPNext公司设置")

_add("Department", ("departId", "departCode"), SYNC_CONTROL, note="外部ID和部门编码")
_add("Department", "departName", ERP_FIELD, "Department", "department_name", "按公司内部门名称创建或更新")
_add("Department", "companyCode", ERP_FIELD, "Department", "company", "通过公司外部ID映射")
_add("Department", ("userName", "departTypeName", "departPhone", "departFunctional", "gmtCreate", "gmtModified"), RAW_ONLY, note="负责人等扩展资料仅保留原始记录")

_add("Category", ("cateId", "categoryId", "id"), SYNC_CONTROL, note="分类外部ID")
_add("Category", ("cateCode", "categoryNo", "cateNo", "code"), ERP_FIELD, "Jackyun Goods Category", "category_code", "取首个非空编码")
_add("Category", ("cateName", "categoryName", "name"), ERP_FIELD, "Jackyun Goods Category", "category_name", "取首个非空名称")
_add("Category", ("parentCateId", "parentCategoryId"), ERP_FIELD, "Jackyun Goods Category", "parent_category", "先按父外部ID解析层级")
_add("Category", ("cateFullName", "fullName"), ERP_FIELD, "Jackyun Goods Category", "full_name", "原值写入")
_add("Category", ("isBlockup", "disabled", "isDelete", "deleted"), ERP_FIELD, "Jackyun Goods Category", "disabled / deleted", "转换为勾选值")

_add("Sales Channel", ("channelId", "salesId", "shopId", "id"), SYNC_CONTROL, note="渠道外部ID")
_add("Sales Channel", ("channelCode", "salesNo", "channelNo", "shopCode", "code"), ERP_FIELD, "Jackyun Sales Channel", "channel_code", "取首个非空编码")
_add("Sales Channel", ("channelName", "salesName", "shopName", "name"), ERP_FIELD, "Jackyun Sales Channel", "channel_name", "取首个非空名称")
_add("Sales Channel", ("channelType", "onlinePlatTypeCode", "onlinePlatTypeName", "platShopId", "platShopName"), ERP_FIELD, "Jackyun Sales Channel", "channel_type / online_platform_* / platform_shop_*", "保留渠道类型、平台类型及店铺信息")
_add("Sales Channel", ("warehouseCode", "warehouseName", "companyCode", "companyName", "channelDepartId", "channelDepartName"), ERP_FIELD, "Jackyun Sales Channel", "warehouse_* / company_* / department_*", "保留组织归属信息")
_add("Sales Channel", ("linkMan", "linkTel", "email", "officeAddress", "chargeType", "cateId", "cateName", "responsibleUserName", "memo"), ERP_FIELD, "Jackyun Sales Channel", "contact_* / settlement_type / category_* / responsible_user_name / memo", "原值写入渠道镜像")
_add("Sales Channel", ("isBlockup", "disabled", "isDelete", "deleted"), ERP_FIELD, "Jackyun Sales Channel", "disabled / deleted", "转换为勾选值")
_add("Sales Channel", tuple(f"field{i}" for i in range(1, 31)), RAW_ONLY, "Jackyun Sales Channel", "custom_fields", "以JSON保存平台自定义项，不创建30个ERPNext字段")

_add("Warehouse", ("warehouseId", "warehouseCode"), SYNC_CONTROL, note="外部ID及仓库编码映射键")
_add("Warehouse", "warehouseName", ERP_FIELD, "Warehouse", "warehouse_name / name", "按ERPNext公司简称生成标准仓库名")
_add("Warehouse", ("warehouseCompanyCode", "companyCode", "warehouseCompanyId", "companyId"), ERP_FIELD, "Warehouse", "company", "通过公司外部ID映射")
_add("Warehouse", ("isBlockup", "isDelete"), ERP_FIELD, "Warehouse", "disabled", "删除或停用均禁用仓库")
_add("Warehouse", ("address", "cityName", "provinceName", "postcode", "tel"), ERP_FIELD, "Warehouse", "address_line_1 / city / state / pin / phone_no", "原值写入")

_add("Supplier", ("vendId", "code"), SYNC_CONTROL, note="供应商外部ID及编码映射键")
_add("Supplier", ("name", "abbreviation"), ERP_FIELD, "Supplier", "supplier_name", "名称为空时回退供应商编码")
_add("Supplier", "className", ERP_FIELD, "Supplier", "supplier_group", "不存在时创建叶子供应商组")
_add("Supplier", "countryName", ERP_FIELD, "Supplier", "country", "转换为ERPNext国家名称")
_add("Supplier", ("taxIdentifyNumber", "taxNumber"), ERP_FIELD, "Supplier", "tax_id", "取首个非空税号")
_add("Supplier", "website", ERP_FIELD, "Supplier", "website", "原值写入")
_add("Supplier", ("memo", "mainProductsAndServices"), ERP_FIELD, "Supplier", "supplier_details", "取首个非空说明")
_add("Supplier", ("isBlockup", "isDelete"), ERP_FIELD, "Supplier", "disabled", "删除或停用均禁用供应商")
_add("Supplier", ("includeDeleteAndBlockup", "needAllPayAccount", "gmtModifiedStart", "gmtModifiedEnd"), SYNC_CONTROL, note="请求过滤及增量时间窗口")

_add("Offline Customer", ("customerId", "customerCode"), SYNC_CONTROL, note="客户外部ID及编码映射键")
_add("Offline Customer", ("nickname", "alias"), ERP_FIELD, "Customer", "customer_name", "名称为空时回退客户编码")
_add("Offline Customer", "customerTypeName", ERP_FIELD, "Customer", "customer_group", "不存在时创建叶子客户组")
_add("Offline Customer", "country", ERP_FIELD, "Customer", "territory", "国家映射为ERPNext区域")
_add("Offline Customer", ("taxNumber", "invoice.taxNumber"), ERP_FIELD, "Customer", "tax_id", "优先客户税号，回退发票税号")
_add("Offline Customer", ("remark", "specialReminding"), ERP_FIELD, "Customer", "customer_details", "取首个非空说明")
_add("Offline Customer", ("isDelete", "blackList", "enable"), ERP_FIELD, "Customer", "disabled", "删除、黑名单或未启用则禁用")
_add("Offline Customer", ("customerIdArr", "customerCodeArr", "customerCreateSourceBatch", "gmtModifiedBegin", "gmtModifiedEnd", "scrollId", "hasTotal"), SYNC_CONTROL, note="按需范围、线下来源、增量时间和滚动游标")

# Product and stock masters
_add("SKU", ("goodsNo", "goodsCode", "code", "skuBarcode"), ERP_FIELD, "Item", "item_code", "取首个非空业务编码；goodsNo优先")
_add("SKU", ("goodsName", "skuName", "name"), ERP_FIELD, "Item", "item_name", "取首个非空名称")
_add("SKU", ("cateName", "categoryName"), ERP_FIELD, "Item", "item_group", "不存在时创建物料组")
_add("SKU", ("unitName", "unit", "baseUnitName"), ERP_FIELD, "Item", "stock_uom", "不存在时创建计量单位")
_add("SKU", ("goodsDesc", "goodsAlias", "spec"), ERP_FIELD, "Item", "description", "取首个非空说明")
_add("SKU", "brandName", ERP_FIELD, "Item", "brand", "不存在时创建品牌")
_add("SKU", ("skuId", "sku_id", "mainBarcode"), SYNC_CONTROL, note="规格别名及外部ID映射，避免同货品重复建Item")
_add("SKU", ("retailPrice", "stockPrice"), RAW_ONLY, note="价格不直接写Item，避免生成重复Item Price")
_add("SKU", ("goodsAttr", "skuWeight", "skuHeight", "skuWidth", "skuLength", "skuLengthVal", "skuLengthUnit", "ownerName"), RAW_ONLY, note="当前无稳定ERPNext目标字段，完整保留原始记录")

_add("Product Bundle", ("skuId", "goodsNo"), SYNC_CONTROL, note="组合装外部ID及父Item业务键")
_add("Product Bundle", ("goodsName", "goodsAlias", "cateName", "unitName", "goodsDesc", "memo", "brandName"), ERP_FIELD, "Item / Product Bundle", "父Item资料 / description", "确保父Item后创建或更新组合装")
_add("Product Bundle", ("goodsPackageDetail.skuId", "goodsPackageDetail.goodsNo"), ERP_FIELD, "Product Bundle Item", "item_code", "通过SKU外部ID或货品编号解析子件")
_add("Product Bundle", "goodsPackageDetail.goodsAmount", ERP_FIELD, "Product Bundle Item", "qty", "转换为数值")
_add("Product Bundle", ("goodsPackageDetail.sharePrice", "goodsPackageDetail.shareAmount", "goodsPackageDetail.shareRatio", "goodsPackageDetail.isGiveaway", "goodsPackageDetail.stockPrice", "goodsPackageDetail.retailPrice"), RAW_ONLY, note="成本分摊和赠品信息当前不写Product Bundle")
_add("Product Bundle", ("maxSkuId", "lastModifiedStart", "lastModifiedEnd"), SYNC_CONTROL, note="大数据量游标及增量时间窗口")

_add("Inventory", "quantityId", SYNC_CONTROL, note="库存快照外部ID及游标")
_add("Inventory", ("warehouseCode", "warehouseName"), ERP_FIELD, "Jackyun Inventory Snapshot", "warehouse_code / warehouse_name / warehouse", "编码映射ERPNext仓库并保留源名称")
_add("Inventory", ("goodsNo", "goodsName", "skuId", "skuName", "skuBarcode", "unitName"), ERP_FIELD, "Jackyun Inventory Snapshot", "goods_* / sku_* / unit_name / item", "解析ERPNext Item并保留源商品资料")
_add("Inventory", ("currentQuantity", "useQuantity", "lockedQuantity", "defectiveQuantity", "defectiveQuanity", "defectiveUseQuantity"), ERP_FIELD, "Jackyun Inventory Snapshot", "current_quantity / available_quantity / locked_quantity / defective_*", "转换为数值")
_add("Inventory", ("reserveQuantity", "purchasingQuantity", "allocateQuantity", "orderingQuantity", "outerQuantity", "salesReturnQuantity", "stockInQuantity", "productingQuantity", "stockOutQuantity", "stockOutuantity"), ERP_FIELD, "Jackyun Inventory Snapshot", "reserve_quantity / purchasing_quantity / allocate_quantity / ordering_quantity / outer_quantity / sales_return_quantity / stock_in_quantity / production_quantity / stock_out_quantity", "转换为数值")
_add("Inventory", ("costPrice", "stockIndex", "ownerName", "brandName", "batchList"), ERP_FIELD, "Jackyun Inventory Snapshot", "cost_price / stock_index / owner_name / brand_name / batch_data", "批次列表以JSON保存；快照不直接写库存账")
_add("Inventory", ("maxQuantityId", "isBlockup", "isChannelReserve", "isbatchmanagement", "gmtModifiedStart", "gmtModifiedEnd"), SYNC_CONTROL, note="游标、库存口径和增量窗口请求参数")

_add("Batch", ("batchNo", "batchNumber"), ERP_FIELD, "Batch", "batch_id", "跨商品重号时追加Item编码")
_add("Batch", ("goodsNo", "skuId", "skuBarcode"), ERP_FIELD, "Batch", "item", "通过商品编号或规格映射Item")
_add("Batch", "productionDate", ERP_FIELD, "Batch", "manufacturing_date", "转换为日期")
_add("Batch", "expirationDate", ERP_FIELD, "Batch", "expiry_date", "转换为日期")
_add("Batch", "isFreeze", ERP_FIELD, "Batch", "disabled", "冻结批次设为禁用")
_add("Batch", ("memo", "freezeReason"), ERP_FIELD, "Batch", "description", "原始批次号冲突说明和备注")
_add("Batch", ("goodsName", "skuName", "brandId", "brandName", "shelfLife", "shelfLiftUnit", "gmtCreate", "vendId", "vendName", "companyId", "companyName", "field1", "field2", "field3", "field4", "field5", "field6", "field7", "field8", "field9", "field10"), RAW_ONLY, note="批次扩展资料暂不创建ERPNext字段")
_add("Batch", ("gmtCreateBegin", "gmtCreateEnd", "notFilterNoStockBatch"), SYNC_CONTROL, note="首次/增量范围和不过滤无库存批次参数")

# Sales order: every explicitly requested field is classified.
_add("Sales Order", "tradeId", SYNC_CONTROL, note="幂等外部ID")
_add("Sales Order", "tradeNo", ERP_FIELD, "Sales Order", "name / custom_jackyun_trade_no", "作为ERPNext编号并保留吉客云销售单号")
_add("Sales Order", ("onlineTradeNo", "sourceTradeNo"), ERP_FIELD, "Sales Order", "custom_jackyun_source_trade_no", "优先网店订单号")
_add("Sales Order", ("tradeStatus", "tradeStatusExplain", "isDelete"), SYNC_CONTROL, "Sales Order", "docstatus / remarks", "决定提交、取消并在备注保留状态")
_add("Sales Order", "tradeType", SYNC_CONTROL, note="订单类型用于请求过滤")
_add("Sales Order", "tradeFrom", RAW_ONLY, note="订单来源仅用于原始追溯")
_add("Sales Order", ("tradeTime", "gmtCreate"), ERP_FIELD, "Sales Order", "transaction_date / delivery_date / custom_jackyun_order_time", "优先订单时间，保留到秒")
_add("Sales Order", ("gmtModified", "scrollId"), SYNC_CONTROL, note="增量时间和滚动分页游标")
_add("Sales Order", "payTime", RAW_ONLY, note="未写ERPNext，保留在原始记录")
_add("Sales Order", "companyName", ERP_FIELD, "Sales Order", "company", "匹配已同步公司")
_add("Sales Order", ("shopId", "shopName", "channelCode"), ERP_FIELD, "Sales Order", "custom_jackyun_sales_channel / customer", "解析销售渠道；电商订单统一平台客户")
_add("Sales Order", ("warehouseId", "warehouseCode", "warehouseName"), ERP_FIELD, "Sales Order", "set_warehouse / Sales Order Item.warehouse", "优先ID或编码映射；名称只作源数据追溯")
_add("Sales Order", ("customerCode", "customerName", "customerAccount", "email", "country", "state", "city", "district", "town", "zip", "payerName", "payerPhone", "payerAddress"), RAW_ONLY, note="隐私快照仅保存在受权限控制的原始记录，不新建电商客户档案字段")
_add("Sales Order", ("chargeCurrencyCode", "chargeExchangeRate"), ERP_FIELD, "Sales Order", "currency / conversion_rate", "RMB归一为CNY，无效币种回退公司币种")
_add("Sales Order", ("totalFee", "payment", "receivedTotal"), ERP_FIELD, "Sales Order", "Sales Order Item.rate", "无明细时用于淘系占位行金额，正常订单金额由明细计算")
_add("Sales Order", "discountFee", ERP_FIELD, "Sales Order", "custom_jackyun_discount_fee", "转换为金额")
_add("Sales Order", "receivedPostFee", ERP_FIELD, "Sales Order", "custom_jackyun_received_post_fee", "转换为金额")
_add("Sales Order", ("buyerMemo", "sellerMemo"), ERP_FIELD, "Sales Order", "remarks", "拼接到备注")
_add("Sales Order", ("goodsDetail.goodsNo", "goodsDetail.goodsId", "goodsDetail.specId", "goodsDetail.barcode"), ERP_FIELD, "Sales Order Item", "item_code", "按规格ID、货品编号或条码解析Item")
_add("Sales Order", ("goodsDetail.goodsName", "goodsDetail.specName", "goodsDetail.unit"), ERP_FIELD, "Sales Order Item", "description / source metadata", "商品名写平台商品信息；规格和单位保留源记录")
_add("Sales Order", ("goodsDetail.sellCount", "goodsDetail.sellPrice", "goodsDetail.sellTotal", "goodsDetail.divideSellTotal"), ERP_FIELD, "Sales Order Item", "qty / rate / custom_jackyun_line_sell_total", "优先分摊销售额计算单价")
_add("Sales Order", ("goodsDetail.discountFee", "goodsDetail.taxRate", "goodsDetail.taxFee", "goodsDetail.isGift"), NOT_USED, note="已请求并留在原始记录，但尚无确认后的ERPNext会计口径")
_add("Sales Order", "goodsDetail.refundStatus", ERP_FIELD, "Sales Order Item", "custom_jackyun_refund_status", "转换为整数状态")
_add("Sales Order", ("goodsDetail.outerId", "goodsDetail.outerSkuId", "goodsDetail.platGoodsId", "goodsDetail.platSkuId", "goodsDetail.platCode"), ERP_FIELD, "Sales Order Item", "custom_ecommerce_* / custom_jackyun_outer_*", "保留平台商品及外部SKU标识")
_add("Sales Order", ("goodsDetail.sourceTradeNo", "goodsDetail.sourceSubtradeNo"), ERP_FIELD, "Sales Order Item", "custom_jackyun_source_trade_no / custom_jackyun_source_subtrade_no", "保留行级平台订单号")
_add("Sales Order", "goodsDetail.subTradeId", RAW_ONLY, note="仅原始记录追溯")

# Purchase order: every explicitly requested field is classified.
_add("Purchase Order", ("id", "headId"), SYNC_CONTROL, note="明细ID及表头幂等外部ID")
_add("Purchase Order", "orderNum", ERP_FIELD, "Purchase Order", "name", "吉客云采购单号作为ERPNext编号")
_add("Purchase Order", ("orderTime", "gmtCreate"), ERP_FIELD, "Purchase Order", "transaction_date", "优先业务时间")
_add("Purchase Order", "gmtModified", SYNC_CONTROL, note="增量修改时间")
_add("Purchase Order", ("vendId", "vendCode", "vendName"), ERP_FIELD, "Purchase Order", "supplier", "优先供应商外部ID/编码映射")
_add("Purchase Order", "companyName", ERP_FIELD, "Purchase Order", "company", "匹配已同步公司")
_add("Purchase Order", ("currencyCode", "currencyName"), ERP_FIELD, "Purchase Order", "currency", "编码写币种，名称仅源记录追溯")
_add("Purchase Order", "revwStatus", SYNC_CONTROL, "Purchase Order", "docstatus", "审核状态决定提交/取消")
_add("Purchase Order", "lineColse", NOT_USED, note="已请求并留在原始记录，当前不关闭ERPNext采购订单行")
_add("Purchase Order", "vendOrderId", ERP_FIELD, "Purchase Order", "supplier_order_info", "供应商外部单号")
_add("Purchase Order", ("memo", "inStatusName", "settStatusName"), ERP_FIELD, "Purchase Order", "remarks", "拼接业务状态和备注")
_add("Purchase Order", ("goodsId", "skuId", "goodsNo", "skuBarcode"), ERP_FIELD, "Purchase Order Item", "item_code", "解析ERPNext Item")
_add("Purchase Order", ("goodsName", "rowRemark"), ERP_FIELD, "Purchase Order Item", "description", "优先行备注")
_add("Purchase Order", ("quantity", "unitName"), ERP_FIELD, "Purchase Order Item", "qty / uom", "数量写入；单位由Item主数据确定")
_add("Purchase Order", ("warehouseId", "warehouseName"), ERP_FIELD, "Purchase Order Item", "warehouse", "仓库ID映射；名称仅追溯")
_add("Purchase Order", "recDate", ERP_FIELD, "Purchase Order Item", "schedule_date", "不得早于订单日期")
_add("Purchase Order", ("price", "amount"), ERP_FIELD, "Purchase Order Item", "rate", "优先金额除以数量，回退单价")
_add("Purchase Order", "taxrate", ERP_FIELD, "Purchase Order", "remarks", "当前仅在备注汇总税率，不生成税模板")
_add("Purchase Order", ("inQuantity", "surInQuantity", "payableAmount", "taxAmount", "noTaxAmount"), RAW_ONLY, note="收货进度和金额汇总不覆盖ERPNext计算字段")

# Inbound specification fields shared by purchase receipts and sales returns.
_INBOUND = ("Purchase Receipt", "Sales Return")
_add(_INBOUND, ("docId", "recId"), SYNC_CONTROL, note="表头/明细幂等ID")
_add(_INBOUND, "goodsdocNo", ERP_FIELD, "Purchase Receipt / Delivery Note", "name / supplier_delivery_note", "业务单号作为ERPNext编号")
_add(_INBOUND, ("billNo", "sourceBillNo"), ERP_FIELD, "Purchase Receipt / Delivery Note", "来源单关联 / remarks", "关联采购订单或销售订单")
_add(_INBOUND, ("inOutDate", "gmtCreate"), ERP_FIELD, "Purchase Receipt / Delivery Note", "posting_date / posting_time", "拆分过账日期和时间")
_add(_INBOUND, ("inouttype", "inouttypeName", "redStatus"), SYNC_CONTROL, note="限定采购入库101、销售退货105和有效蓝单")
_add(_INBOUND, ("vendCustomerId", "vendCustomerCode", "vendCode", "vendCustomerName"), ERP_FIELD, "Purchase Receipt / Delivery Note", "supplier / customer", "按往来单位映射")
_add(_INBOUND, ("currencyCode", "currencyRate"), ERP_FIELD, "Purchase Receipt / Delivery Note", "currency / conversion_rate", "币种归一及汇率转换")
_add(_INBOUND, ("warehouseCode", "warehouseName", "companyName"), ERP_FIELD, "Purchase Receipt / Delivery Note", "warehouse / company", "编码解析仓库，名称用于追溯")
_add(_INBOUND, "channelCode", ERP_FIELD, "Delivery Note", "custom_jackyun_sales_channel", "销售退货匹配渠道；采购入库不适用")
_add(_INBOUND, ("goodsdocRemark", "receiveGoodsRemark"), ERP_FIELD, "Purchase Receipt / Delivery Note", "remarks", "拼接单据备注")
_add(_INBOUND, ("goodsNo", "skuBarcode"), ERP_FIELD, "Purchase Receipt Item / Delivery Note Item", "item_code", "解析ERPNext Item")
_add(_INBOUND, ("goodsName", "skuName", "unitName", "goodsDetailRemark"), ERP_FIELD, "Purchase Receipt Item / Delivery Note Item", "description / source metadata", "描述写入，规格单位由Item主数据确定")
_add(_INBOUND, "quantity", ERP_FIELD, "Purchase Receipt Item / Delivery Note Item", "qty", "采购入库正数；销售退货转换负数")
_add(_INBOUND, ("baceCurrencyCostPrice", "baceCurrencyCostAmount", "baceCurrencyWithTaxPrice", "baceCurrencyWithTaxAmount"), ERP_FIELD, "Purchase Receipt Item / Delivery Note Item", "rate", "优先含税金额/价格，回退成本金额/价格")
_add(_INBOUND, ("taxRate", "isCertified"), RAW_ONLY, note="税率与正次品标记当前不生成ERPNext税模板或质量单据")
_add(_INBOUND, ("batchNo", "productionDate", "expirationDate"), ERP_FIELD, "Purchase Receipt Item / Delivery Note Item", "batch_no / description", "批次映射；生产和到期日在描述追溯")
_add(_INBOUND, ("serialNo", "serialSourceId"), ERP_FIELD, "Purchase Receipt Item / Delivery Note Item", "description", "唯一码摘要写描述，完整列表保留原始记录")

# Outbound specification fields shared by delivery notes and purchase returns.
_OUTBOUND = ("Delivery Note", "Purchase Return")
_add(_OUTBOUND, "recId", SYNC_CONTROL, note="明细去重ID")
_add(_OUTBOUND, "goodsdocNo", ERP_FIELD, "Delivery Note / Purchase Receipt", "name / supplier_delivery_note", "业务单号作为ERPNext编号")
_add(_OUTBOUND, ("billNo", "sourceBillNo", "deliveryNo"), ERP_FIELD, "Delivery Note / Purchase Receipt", "来源单关联 / remarks", "关联销售订单或采购订单")
_add(_OUTBOUND, ("inOutDate", "gmtCreate"), ERP_FIELD, "Delivery Note / Purchase Receipt", "posting_date / posting_time", "拆分过账日期和时间")
_add(_OUTBOUND, ("inouttype", "inouttypeName", "redStatus"), SYNC_CONTROL, note="限定销售出库201、采购退货205和有效蓝单")
_add(_OUTBOUND, ("vendCode", "vendCustomerName"), ERP_FIELD, "Delivery Note / Purchase Receipt", "customer / supplier", "按往来单位映射")
_add(_OUTBOUND, ("currencyCode", "currencyRate"), ERP_FIELD, "Delivery Note / Purchase Receipt", "currency / conversion_rate", "币种归一及汇率转换")
_add(_OUTBOUND, ("warehouseCode", "warehouseName", "companyName"), ERP_FIELD, "Delivery Note / Purchase Receipt", "warehouse / company", "编码解析仓库")
_add(_OUTBOUND, "channelCode", ERP_FIELD, "Delivery Note", "custom_jackyun_sales_channel", "销售出库匹配渠道；采购退货不适用")
_add(_OUTBOUND, ("goodsdocRemark", "receiveGoodsRemark"), ERP_FIELD, "Delivery Note / Purchase Receipt", "remarks", "拼接单据备注")
_add(_OUTBOUND, ("goodsNo", "skuBarcode"), ERP_FIELD, "Delivery Note Item / Purchase Receipt Item", "item_code", "解析ERPNext Item")
_add(_OUTBOUND, ("goodsName", "skuName", "unitName", "goodsDetailRemark"), ERP_FIELD, "Delivery Note Item / Purchase Receipt Item", "description / source metadata", "描述写入，规格单位由Item主数据确定")
_add(_OUTBOUND, "quantity", ERP_FIELD, "Delivery Note Item / Purchase Receipt Item", "qty", "销售出库正数；采购退货转换负数")
_add(_OUTBOUND, ("baceCurrencyCostPrice", "baceCurrencyCostAmount", "baceCurrencyWithTaxPrice", "baceCurrencyWithTaxAmount"), ERP_FIELD, "Delivery Note Item / Purchase Receipt Item", "rate", "销售出库优先源订单价格；采购退货按接口金额/价格")
_add(_OUTBOUND, ("taxRate", "isCertified"), RAW_ONLY, note="税率与正次品标记当前不生成ERPNext税模板或质量单据")
_add(_OUTBOUND, ("batchNo", "productionDate", "expirationDate"), ERP_FIELD, "Delivery Note Item / Purchase Receipt Item", "batch_no / description", "批次映射，日期仅描述追溯")
_add(_OUTBOUND, ("serialNo", "serialSourceId"), ERP_FIELD, "Delivery Note Item / Purchase Receipt Item", "description", "唯一码摘要写描述，完整列表保留原始记录")
_add(_OUTBOUND, ("logisticNo", "logisticName"), ERP_FIELD, "Delivery Note", "lr_no / remarks", "销售出库物流信息；采购退货仅原始追溯")
_add(_OUTBOUND, ("outBillNo", "trackInOutNo", "flagName"), RAW_ONLY, note="平台外部单号、跟踪号和标记完整保留原始记录")

# Stock movement reuses inbound/outbound specification interfaces.
_add("Stock Movement", ("docId", "recId", "goodsdocNo"), SYNC_CONTROL, note="类型+业务单号组成幂等ID")
_add("Stock Movement", ("inouttype", "inouttypeName", "redStatus"), SYNC_CONTROL, "Stock Entry", "stock_entry_type", "按入出库类型映射Material Receipt/Issue并过滤有效蓝单")
_add("Stock Movement", ("inOutDate", "gmtCreate"), ERP_FIELD, "Stock Entry", "posting_date / posting_time", "拆分过账日期和时间")
_add("Stock Movement", ("companyName", "warehouseCode", "warehouseName"), ERP_FIELD, "Stock Entry", "company / s_warehouse / t_warehouse", "按方向设置源仓或目标仓")
_add("Stock Movement", ("goodsNo", "skuBarcode", "quantity", "batchNo", "goodsName", "goodsDetailRemark"), ERP_FIELD, "Stock Entry Detail", "item_code / qty / batch_no / description", "解析商品、数量及批次")
_add("Stock Movement", ("goodsdocRemark", "receiveGoodsRemark"), ERP_FIELD, "Stock Entry", "remarks", "保留异动类型和备注")
_add("Stock Movement", ("currencyCode", "currencyRate", "taxRate", "baceCurrencyCostPrice", "baceCurrencyCostAmount", "baceCurrencyWithTaxPrice", "baceCurrencyWithTaxAmount", "vendCode", "vendCustomerName", "channelCode", "serialNo", "serialSourceId", "productionDate", "expirationDate"), RAW_ONLY, note="库存异动不建立往来、税额和估值口径；完整原始记录留档")

# Transfer: every explicitly requested field is classified.
_add("Stock Transfer", ("allocateId", "allocateDetailId"), SYNC_CONTROL, note="表头及明细幂等ID")
_add("Stock Transfer", "allocateNo", ERP_FIELD, "Stock Entry", "name / remarks", "吉客云调拨号作为ERPNext编号")
_add("Stock Transfer", "isAllocateDifferentCompany", SYNC_CONTROL, "Stock Entry", "stock_entry_type", "同公司建Material Transfer，跨公司拆为Issue和Receipt")
_add("Stock Transfer", ("outWarehouseId", "outWarehouseCode", "inWarehouseId", "inWarehouseCode"), ERP_FIELD, "Stock Entry Detail", "s_warehouse / t_warehouse", "按仓库编码解析；ID保留映射语义")
_add("Stock Transfer", ("auditDate", "gmtModified", "gmtCreate", "applyDate"), ERP_FIELD, "Stock Entry", "posting_date / posting_time", "优先审核时间，依次回退修改、创建和申请时间")
_add("Stock Transfer", ("status", "outStatus", "inStatus"), SYNC_CONTROL, note="决定是否符合自动提交策略")
_add("Stock Transfer", ("memo", "reason", "sourceNo"), ERP_FIELD, "Stock Entry", "remarks", "拼接原因、备注和来源单号")
_add("Stock Transfer", ("companyId", "companyName"), RAW_ONLY, note="实际公司从出入仓库确定，源公司信息留档")
_add("Stock Transfer", ("goodsId", "skuId", "goodsNo", "skuBarcode", "outSkuCode"), ERP_FIELD, "Stock Entry Detail", "item_code", "解析ERPNext Item")
_add("Stock Transfer", ("goodsName", "skuName", "unitName", "rowRemark"), ERP_FIELD, "Stock Entry Detail", "description / source metadata", "优先行备注写描述")
_add("Stock Transfer", ("goodsSkuCount", "skuPrice"), ERP_FIELD, "Stock Entry Detail", "qty / basic_rate", "转换为数值")
_add("Stock Transfer", ("allocateType", "applyUserName", "applyDepartName", "operator", "auditUserName", "goodsCount", "skuCount", "totalAmount", "planOutDate", "planInDate", "logisticNo", "goodsTotalAmount", "outCount", "inCount", "isCertified"), NOT_USED, note="已请求并留在原始记录，当前不生成审批、计划、汇总或物流字段")

_add("Stocktake", ("stocktakeId", "id"), SYNC_CONTROL, note="盘点单外部ID")
_add("Stocktake", ("warehouseCode", "warehouseId", "warehouseName"), ERP_FIELD, "Stock Reconciliation", "set_warehouse / company", "编码映射仓库，公司由仓库确定")
_add("Stocktake", "stocktakeDate", ERP_FIELD, "Stock Reconciliation", "posting_date / posting_time", "拆分过账日期和时间")
_add("Stocktake", "status", SYNC_CONTROL, note="仅完成状态5落ERPNext")
_add("Stocktake", ("stockTakeDetailViews.goodsNo", "stockTakeDetailViews.skuId", "stockTakeDetailViews.skuBarcode"), ERP_FIELD, "Stock Reconciliation Item", "item_code", "解析ERPNext Item")
_add("Stocktake", ("stockTakeDetailViews.takeQuan", "stockTakeDetailViews.price"), ERP_FIELD, "Stock Reconciliation Item", "qty / valuation_rate", "盘点量和单价转换为数值")
_add("Stocktake", ("stockTakeDetailViews.stockQuan", "stockTakeDetailViews.variQuan", "stockTakeDetailViews.batchNo", "stockTakeDetailViews.positionName", "stockTakeDetailViews.rowRemark", "text", "createName"), RAW_ONLY, note="账面量、差异、货位、批次及制单信息仅原始记录追溯")
_add("Stocktake", ("startPdDate", "endPdDate"), SYNC_CONTROL, note="盘点时间查询窗口")

# Request-only controls. Public signature fields (appkey, timestamp, sign, etc.)
# belong to the connection protocol rather than a business-resource mapping.
_add(SYNC_RESOURCES, ("pageIndex", "pageSize"), SYNC_CONTROL, note="公共分页参数")
_add(("Company", "Department"), "companyCodes", SYNC_CONTROL, note="连接配置的公司范围过滤")
_add("Warehouse", "includeDeleteAndBlockup", SYNC_CONTROL, note="包含停用和删除仓库以同步禁用状态")
_add("Sales Order", ("startCreated", "endCreated", "startModified", "endModified", "fields", "isTableSwitch", "tradeTypeList"), SYNC_CONTROL, note="首次/增量时间窗口、返回字段和订单类型过滤")
_add("Purchase Order", ("startDate", "endDate", "startModifyDate", "endModifyDate", "cols"), SYNC_CONTROL, note="首次/增量时间窗口和返回列")
_add(("Purchase Receipt", "Sales Return", "Stock Movement"), ("inOutDateStart", "inOutDateEnd", "gmtModifiedDateStart", "gmtModifiedDateEnd", "gmtModifiedDetailDateStart", "gmtModifiedDetailDateEnd", "inouttypes", "archived", "cols"), SYNC_CONTROL, note="入库首次/表头/明细增量窗口、类型、归档状态和返回列")
_add(("Delivery Note", "Purchase Return"), ("inOutDateStart", "inOutDateEnd", "gmtModifiedDateStart", "gmtModifiedDateEnd", "gmtModifiedDetailDateStart", "gmtModifiedDetailDateEnd", "inouttypes", "archived", "cols"), SYNC_CONTROL, note="出库首次/表头/明细增量窗口、类型、归档状态和返回列")
_add("Stock Transfer", ("startCreateTime", "endCreateTime", "cols"), SYNC_CONTROL, note="调拨创建时间窗口和返回列")


def catalog_source_fields(resource):
    """Return exact documented source-field tokens for test and audit tooling."""
    return {
        field
        for row in FIELD_MAPPING_CATALOG
        if row["resource"] == resource
        for field in row["source_fields"]
    }
