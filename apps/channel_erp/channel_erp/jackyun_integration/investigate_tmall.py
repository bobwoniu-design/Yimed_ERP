"""调查天猫 TM-医麦德旗舰店 销售单为什么没同步——直接调吉客云 API 查 08-24 的单。"""
import frappe
from datetime import timedelta

from channel_erp.integrations.jackyun import (
    JackYunAdapter,
    METHOD_MAP,
    SALES_ORDER_FIELDS,
    SALES_ORDER_PAGE_SIZE,
    extract_records_by_identity,
)


def investigate():
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)

    # 先查 Sales Channel 表里 TM-医麦德 的 channel_id / channel_code
    rows = frappe.db.get_all(
        "Jackyun Sales Channel",
        filters={"online_platform_name": "天猫商城"},
        fields=["channel_id", "channel_code", "channel_name"],
    )
    print("=== Sales Channel 表里天猫渠道 ===")
    for r in rows:
        print(f"  {r.channel_name}  channel_id={r.channel_id}  channel_code={r.channel_code}")

    end = frappe.utils.now_datetime()
    # 查 08-22 ~ 08-25 三天
    start = end - timedelta(days=4)

    # 方式1：按日期窗口 + 全渠道拉，筛 shopId=TM-医麦德
    print(f"\n=== 调 oms.trade.fullinfoget 查 {start.date()} ~ {end.date()} 全渠道，筛天猫 ===")
    method = METHOD_MAP["Sales Order"]
    from datetime import datetime
    s = start
    found_tmall = []
    while s <= end:
        window_end = min(s + timedelta(days=1) - timedelta(seconds=1), end)
        payload = adapter.request(
            method,
            {
                "startCreated": s.strftime("%Y-%m-%d %H:%M:%S"),
                "endCreated": window_end.strftime("%Y-%m-%d %H:%M:%S"),
                "fields": SALES_ORDER_FIELDS,
                "isTableSwitch": 1,
                "isDelete": "0",
                "tradeTypeList": [1, 2, 3, 4, 5, 6, 9, 10, 14],
            },
            page_index=0,
            page_size=SALES_ORDER_PAGE_SIZE,
        )
        records = extract_records_by_identity(payload, ["tradeId"])
        for r in records:
            shop_name = r.get("shopName") or ""
            shop_id = r.get("shopId")
            if "TM-" in shop_name or shop_id == "1181057603336307712":
                found_tmall.append({
                    "tradeNo": r.get("tradeNo"),
                    "shopName": shop_name,
                    "shopId": shop_id,
                    "tradeStatus": r.get("tradeStatus"),
                    "gmtCreate": r.get("gmtCreate"),
                    "totalFee": r.get("totalFee"),
                })
        s = window_end + timedelta(seconds=1)

    print(f"\n=== API 查到的天猫(TM-医麦德)销售单: {len(found_tmall)} 张 ===")
    for x in found_tmall[:20]:
        print(f"  {x['tradeNo']}  {x['shopName']}  shopId={x['shopId']}  status={x['tradeStatus']}  {x['gmtCreate']}  {x['totalFee']}")


def by_date():
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)
    end = frappe.utils.now_datetime()
    start = end - timedelta(days=4)
    method = METHOD_MAP["Sales Order"]
    s = start
    counts = {}
    while s <= end:
        window_end = min(s + timedelta(days=1) - timedelta(seconds=1), end)
        payload = adapter.request(
            method,
            {
                "startCreated": s.strftime("%Y-%m-%d %H:%M:%S"),
                "endCreated": window_end.strftime("%Y-%m-%d %H:%M:%S"),
                "fields": SALES_ORDER_FIELDS,
                "isTableSwitch": 1,
                "isDelete": "0",
                "tradeTypeList": [1, 2, 3, 4, 5, 6, 9, 10, 14],
            },
            page_index=0,
            page_size=SALES_ORDER_PAGE_SIZE,
        )
        records = extract_records_by_identity(payload, ["tradeId"])
        for r in records:
            if r.get("shopId") == "1181057603336307712":
                d = (r.get("gmtCreate") or "")[:10]
                counts[d] = counts.get(d, 0) + 1
        s = window_end + timedelta(seconds=1)
    print("=== TM-医麦德 按 gmtCreate 日期分布（API 直查）===")
    for d in sorted(counts):
        print(f"  {d}: {counts[d]} 张")


def check_goodsdetail():
    """查 TM-医麦德 单的 goodsDetail.sellCount 结构，确认是否被「正数量」过滤。"""
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)
    from datetime import timedelta
    end = frappe.utils.now_datetime()
    start = end - timedelta(days=4)
    method = METHOD_MAP["Sales Order"]
    s = start
    checked = 0
    while s <= end and checked < 5:
        window_end = min(s + timedelta(days=1) - timedelta(seconds=1), end)
        payload = adapter.request(
            method,
            {
                "startCreated": s.strftime("%Y-%m-%d %H:%M:%S"),
                "endCreated": window_end.strftime("%Y-%m-%d %H:%M:%S"),
                "fields": SALES_ORDER_FIELDS,
                "isTableSwitch": 1,
                "isDelete": "0",
                "tradeTypeList": [1, 2, 3, 4, 5, 6, 9, 10, 14],
            },
            page_index=0,
            page_size=SALES_ORDER_PAGE_SIZE,
        )
        records = extract_records_by_identity(payload, ["tradeId"])
        for r in records:
            if r.get("shopId") != "1181057603336307712":
                continue
            gd = r.get("goodsDetail") or []
            print(f"\n{r.get('tradeNo')} status={r.get('tradeStatus')} totalFee={r.get('totalFee')} goodsDetail行数={len(gd)}")
            for row in gd[:3]:
                print(f"    sellCount={row.get('sellCount')} baseUnitSellCount={row.get('baseUnitSellCount')} goodsNo={row.get('goodsNo')} isGift={row.get('isGift')}")
            checked += 1
            if checked >= 5:
                break
        s = window_end + timedelta(seconds=1)


def compare_channels():
    """对比天猫 vs 其他渠道的 goodsDetail 是否为空。"""
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)
    from datetime import timedelta
    end = frappe.utils.now_datetime()
    start = end - timedelta(days=2)
    method = METHOD_MAP["Sales Order"]
    payload = adapter.request(
        method,
        {
            "startCreated": start.strftime("%Y-%m-%d %H:%M:%S"),
            "endCreated": end.strftime("%Y-%m-%d %H:%M:%S"),
            "fields": SALES_ORDER_FIELDS,
            "isTableSwitch": 1,
            "isDelete": "0",
            "tradeTypeList": [1, 2, 3, 4, 5, 6, 9, 10, 14],
        },
        page_index=0,
        page_size=SALES_ORDER_PAGE_SIZE,
    )
    records = extract_records_by_identity(payload, ["tradeId"])
    by_shop = {}
    for r in records:
        shop = r.get("shopName") or "?"
        gd_len = len(r.get("goodsDetail") or [])
        d = by_shop.setdefault(shop, {"total": 0, "empty_gd": 0})
        d["total"] += 1
        if gd_len == 0:
            d["empty_gd"] += 1
    print("=== 各店铺 goodsDetail 空单率 ===")
    for shop, v in sorted(by_shop.items()):
        print(f"  {shop:30s} 总{v['total']:>4d}  空goodsDetail={v['empty_gd']:>4d}")


def test_params():
    """测不同参数能否让天猫单返回 goodsDetail。"""
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)
    from datetime import timedelta
    end = frappe.utils.now_datetime()
    start = end - timedelta(days=2)
    method = METHOD_MAP["Sales Order"]

    for is_table in [1, 0]:
        payload = adapter.request(
            method,
            {
                "startCreated": start.strftime("%Y-%m-%d %H:%M:%S"),
                "endCreated": end.strftime("%Y-%m-%d %H:%M:%S"),
                "fields": SALES_ORDER_FIELDS,
                "isTableSwitch": is_table,
                "isDelete": "0",
                "tradeTypeList": [1, 2, 3, 4, 5, 6, 9, 10, 14],
            },
            page_index=0,
            page_size=10,
        )
        records = extract_records_by_identity(payload, ["tradeId"])
        tm = [r for r in records if r.get("shopId") == "1181057603336307712"]
        print(f"isTableSwitch={is_table}: 天猫单 {len(tm)} 张", end="")
        if tm:
            gd_lens = [len(r.get("goodsDetail") or []) for r in tm]
            print(f"  goodsDetail行数={gd_lens}")
            # 看天猫单有哪些字段有数据
            if len(tm[0].get("goodsDetail") or []) == 0:
                print(f"    样单字段: {[k for k,v in tm[0].items() if v not in (None,'',[],{})][:25]}")
        else:
            print()


def check_dn_goodsdetail():
    """查天猫(TM-医麦德)出库单的 goodsDocDetailList 是否为空。"""
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)
    from datetime import timedelta
    end = frappe.utils.now_datetime()
    start = end - timedelta(days=4)

    # 出库单按 inOutDate 拉取，筛天猫
    from channel_erp.integrations.jackyun import DELIVERY_NOTE_FIELDS
    s = start
    by_shop = {}
    while s <= end:
        window_end = min(s + timedelta(days=1) - timedelta(seconds=1), end)
        payload = adapter.request(
            "erp-busiorder.goodsdocout.search",
            {
                "inOutDateStart": s.strftime("%Y-%m-%d %H:%M:%S"),
                "inOutDateEnd": window_end.strftime("%Y-%m-%d %H:%M:%S"),
                "inouttypes": "201",
                "archived": 0,
                "cols": DELIVERY_NOTE_FIELDS,
            },
            page_index=0,
            page_size=100,
        )
        records = extract_records_by_identity(payload, ["recId", "goodsdocNo"])
        for r in records:
            shop = r.get("channelCode") or r.get("warehouseName") or "?"
            # 用 vendCustomerName / 渠道字段判断天猫，先统计所有
            gd = r.get("goodsDocDetailList") or []
            d = by_shop.setdefault(shop, {"total": 0, "empty": 0})
            d["total"] += 1
            if len(gd) == 0:
                d["empty"] += 1
        s = window_end + timedelta(seconds=1)

    print("=== 出库单各渠道 goodsDocDetailList 空单率 ===")
    for shop, v in sorted(by_shop.items()):
        print(f"  {shop:30s} 总{v['total']:>4d}  空={v['empty']:>4d}")


def check_dn_detail_fields():
    """拉一条天猫(102)出库单明细行，看商品字段有没有。"""
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)
    from datetime import timedelta
    from channel_erp.integrations.jackyun import DELIVERY_NOTE_FIELDS
    end = frappe.utils.now_datetime()
    start = end - timedelta(days=2)
    payload = adapter.request(
        "erp-busiorder.goodsdocout.search",
        {
            "inOutDateStart": start.strftime("%Y-%m-%d %H:%M:%S"),
            "inOutDateEnd": end.strftime("%Y-%m-%d %H:%M:%S"),
            "inouttypes": "201",
            "archived": 0,
            "cols": DELIVERY_NOTE_FIELDS,
        },
        page_index=0,
        page_size=50,
    )
    records = extract_records_by_identity(payload, ["recId", "goodsdocNo"])
    print(f"总明细行数: {len(records)}")
    # 找天猫(102)的
    tm = [r for r in records if str(r.get("channelCode")) == "102"]
    print(f"天猫(102)明细行数: {len(tm)}")
    if tm:
        r = tm[0]
        print(f"\n样单 goodsdocNo={r.get('goodsdocNo')} channelCode={r.get('channelCode')}")
        for k in ["goodsNo", "goodsName", "skuName", "quantity", "batchNo", "warehouseCode", "warehouseName", "vendCustomerName"]:
            print(f"  {k} = {r.get(k)}")


def simulate_upsert():
    """拉一张天猫销售单，模拟 transform，看缺什么字段。"""
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)
    from datetime import timedelta
    end = frappe.utils.now_datetime()
    start = end - timedelta(days=2)
    payload = adapter.request(
        METHOD_MAP["Sales Order"],
        {
            "startCreated": start.strftime("%Y-%m-%d %H:%M:%S"),
            "endCreated": end.strftime("%Y-%m-%d %H:%M:%S"),
            "fields": SALES_ORDER_FIELDS,
            "isTableSwitch": 1,
            "isDelete": "0",
            "tradeTypeList": [1, 2, 3, 4, 5, 6, 9, 10, 14],
        },
        page_index=0,
        page_size=50,
    )
    records = extract_records_by_identity(payload, ["tradeId"])
    tm = [r for r in records if r.get("shopId") == "1181057603336307712"]
    if not tm:
        print("无天猫单")
        return
    r = tm[0]
    print(f"=== 天猫单 {r.get('tradeNo')} 表头字段 ===")
    print(f"  tradeNo={r.get('tradeNo')}  tradeStatus={r.get('tradeStatus')}")
    print(f"  totalFee={r.get('totalFee')}  payment={r.get('payment')}  receivedTotal={r.get('receivedTotal')}")
    print(f"  customerCode={r.get('customerCode')}  customerName={r.get('customerName')}")
    print(f"  warehouseCode={r.get('warehouseCode')}  channelCode={r.get('channelCode')}")
    print(f"  companyName={r.get('companyName')}  tradeTime={r.get('tradeTime')}  gmtCreate={r.get('gmtCreate')}")
    print(f"  goodsDetail 行数={len(r.get('goodsDetail') or [])}")
    print(f"  goodslist(货品摘要)={r.get('goodslist')}")

    # 模拟 transform
    mapped = adapter.transform("Sales Order", r)
    print(f"\n=== transform 后 ===")
    print(f"  trade_no={mapped.get('trade_no')}  trade_time={mapped.get('trade_time')}")
    print(f"  customer_code={mapped.get('customer_code')}  warehouse_code={mapped.get('warehouse_code')}")
    print(f"  company_name={mapped.get('company_name')}")
    print(f"  items 行数={len(mapped.get('items') or [])}")
    print(f"  totalFee(原)= {r.get('totalFee')}")


def create_placeholder():
    if frappe.db.exists("Item", "TM-PLACEHOLDER"):
        print("占位商品已存在")
        return
    frappe.get_doc({
        "doctype": "Item",
        "item_code": "TM-PLACEHOLDER",
        "item_name": "天猫占位商品（奇门限制无明细）",
        "item_group": "All Item Groups",
        "stock_uom": "Nos",
        "is_stock_item": 0,
        "is_sales_item": 1,
        "description": "奇门资质未开通前，天猫销售订单无货品明细，用此占位商品承载表头金额",
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    print("占位商品已创建")


def check_by_tradeno():
    """用 tradeNo 单条查天猫单，看 goodsDetail 是否也空。"""
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)
    # 已知的天猫单号（之前查到的）
    for trade_no in ["JY202608230762", "JY202608240020", "JY202608220783"]:
        payload = adapter.request(
            METHOD_MAP["Sales Order"],
            {
                "tradeNo": trade_no,
                "fields": SALES_ORDER_FIELDS,
                "isTableSwitch": 1,
                "isDelete": "0",
            },
            page_index=0,
            page_size=10,
        )
        records = extract_records_by_identity(payload, ["tradeId"])
        for r in records:
            if r.get("tradeNo") == trade_no:
                gd = r.get("goodsDetail") or []
                print(f"{trade_no}: goodsDetail 行数={len(gd)}")
                if gd:
                    for row in gd[:3]:
                        print(f"    goodsNo={row.get('goodsNo')} sellCount={row.get('sellCount')} sellTotal={row.get('sellTotal')}")
                else:
                    print(f"    (空)")
                break
        else:
            print(f"{trade_no}: 未查到")


def check_tradeno_batch():
    """用 tradeNo 查多张天猫单，统计 goodsDetail 行数 vs tradeStatus。"""
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)
    from datetime import timedelta
    # 先用日期批量拉天猫单号
    end = frappe.utils.now_datetime()
    start = end - timedelta(days=3)
    payload = adapter.request(
        METHOD_MAP["Sales Order"],
        {
            "startCreated": start.strftime("%Y-%m-%d %H:%M:%S"),
            "endCreated": end.strftime("%Y-%m-%d %H:%M:%S"),
            "fields": "tradeNo,tradeStatus,shopId,scrollId",
            "isTableSwitch": 1,
            "isDelete": "0",
            "tradeTypeList": [1, 2, 3, 4, 5, 6, 9, 10, 14],
        },
        page_index=0,
        page_size=100,
    )
    records = extract_records_by_identity(payload, ["tradeId"])
    tm = [r for r in records if r.get("shopId") == "1181057603336307712"][:15]
    print(f"取 {len(tm)} 张天猫单，用 tradeNo 单查 goodsDetail")
    by_status = {}
    for r in tm:
        trade_no = r.get("tradeNo")
        ts = r.get("tradeStatus")
        p = adapter.request(
            METHOD_MAP["Sales Order"],
            {"tradeNo": trade_no, "fields": SALES_ORDER_FIELDS, "isTableSwitch": 1, "isDelete": "0"},
            page_index=0, page_size=10,
        )
        recs = extract_records_by_identity(p, ["tradeId"])
        for rec in recs:
            if rec.get("tradeNo") == trade_no:
                gd_len = len(rec.get("goodsDetail") or [])
                d = by_status.setdefault(str(ts), {"total": 0, "has_detail": 0})
                d["total"] += 1
                if gd_len > 0:
                    d["has_detail"] += 1
                break
    print("\n=== tradeStatus vs goodsDetail 有无 ===")
    for ts, v in sorted(by_status.items()):
        print(f"  tradeStatus={ts}: 总{v['total']}  有明细={v['has_detail']}")


def verify_jy202608240020():
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)
    payload = adapter.request(
        METHOD_MAP["Sales Order"],
        {"tradeNo": "JY202608240020", "fields": SALES_ORDER_FIELDS, "isTableSwitch": 1, "isDelete": "0"},
        page_index=0, page_size=10,
    )
    records = extract_records_by_identity(payload, ["tradeId"])
    for r in records:
        if r.get("tradeNo") == "JY202608240020":
            print(f"shopId={r.get('shopId')} shopName={r.get('shopName')} tradeStatus={r.get('tradeStatus')}")
            print(f"goodsDetail 行数={len(r.get('goodsDetail') or [])}")
            for row in (r.get("goodsDetail") or [])[:3]:
                print(f"  goodsNo={row.get('goodsNo')} sellCount={row.get('sellCount')}")
            break
    else:
        print("未查到")


def check_online_order_api():
    """测 omsapi-business.order.get 接口能否返回天猫网店订单明细。"""
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)
    from datetime import timedelta
    end = frappe.utils.now_datetime()
    start = end - timedelta(days=2)

    fields = [
        "tradeOnline.tradeNo", "tradeOnline.shopId", "tradeOnline.shopName",
        "tradeOnline.tradeStatusExplain", "tradeOnline.totalFee", "tradeOnline.payment",
        "tradeOnline.createTime", "tradeOnline.gmtCreate", "tradeOnline.sysTradeId_String",
        "tradeOnlineGoodsList.goodsBarcode", "tradeOnlineGoodsList.goodsName",
        "tradeOnlineGoodsList.sellCount", "tradeOnlineGoodsList.sellPrice",
        "tradeOnlineGoodsList.sellTotal", "tradeOnlineGoodsList.outerId",
        "tradeOnlineGoodsList.subTradeNo", "tradeOnlineGoodsList.sysGoodsId",
        "tradeOnlineGoodsList.sysSpecId", "tradeOnlineGoodsList.platGoodsId",
        "tradeOnlineGoodsList.platSkuId", "tradeOnlineGoodsList.isGift",
        "tradeOnlineGoodsList.discountFee", "tradeOnlineGoodsList.divideSellTotal",
    ]
    payload = adapter.request(
        "omsapi-business.order.get",
        {
            "startModified": start.strftime("%Y-%m-%d %H:%M:%S"),
            "endModified": end.strftime("%Y-%m-%d %H:%M:%S"),
            "shopIds": ["1181057603336307712"],
            "pageIndex": 0,
            "pageSize": 10,
            "fields": frappe.as_json(fields),
            "hasQueryHistory": 0,
        },
        page_index=0,
        page_size=10,
    )
    # 看返回结构
    data = (payload.get("result") or {}).get("data")
    print(f"data 类型: {type(data).__name__}")
    if isinstance(data, list):
        print(f"订单数: {len(data)}")
        for r in data[:3]:
            gdl = r.get("goodsDetailList") or []
            print(f"\n  {r.get('tradeNo')} shop={r.get('shopName')} totalFee={r.get('totalFee')} 明细行={len(gdl)}")
            for row in gdl[:3]:
                print(f"    goodsBarcode={row.get('goodsBarcode')} goodsName={row.get('goodsName')} sellCount={row.get('sellCount')} sellTotal={row.get('sellTotal')}")
    elif isinstance(data, dict):
        gdl = data.get("goodsDetailList") or []
        print(f"单订单 {data.get('tradeNo')} shop={data.get('shopName')} 明细行={len(gdl)}")
        for row in gdl[:3]:
            print(f"  goodsBarcode={row.get('goodsBarcode')} sellCount={row.get('sellCount')} sellTotal={row.get('sellTotal')}")
    else:
        print(f"payload: {frappe.as_json(payload)[:500]}")


def check_online_order_api2():
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)
    from datetime import timedelta
    end = frappe.utils.now_datetime()
    start = end - timedelta(days=2)
    fields = [
        "tradeOnline.tradeId", "tradeOnline.apiType", "tradeOnlineGoodsList.tradeId",
        "tradeOnline.tradeNo", "tradeOnline.shopId", "tradeOnline.shopName",
        "tradeOnline.tradeStatusExplain", "tradeOnline.totalFee", "tradeOnline.payment",
        "tradeOnline.createTime", "tradeOnline.gmtCreate", "tradeOnline.sysTradeId_String",
        "tradeOnlineGoodsList.goodsBarcode", "tradeOnlineGoodsList.goodsName",
        "tradeOnlineGoodsList.sellCount", "tradeOnlineGoodsList.sellPrice",
        "tradeOnlineGoodsList.sellTotal", "tradeOnlineGoodsList.outerId",
        "tradeOnlineGoodsList.subTradeNo", "tradeOnlineGoodsList.sysGoodsId",
        "tradeOnlineGoodsList.sysSpecId", "tradeOnlineGoodsList.isGift",
        "tradeOnlineGoodsList.discountFee", "tradeOnlineGoodsList.divideSellTotal",
    ]
    payload = adapter.request(
        "omsapi-business.order.get",
        {
            "startModified": start.strftime("%Y-%m-%d %H:%M:%S"),
            "endModified": end.strftime("%Y-%m-%d %H:%M:%S"),
            "shopIds": ["1181057603336307712"],
            "pageIndex": 0,
            "pageSize": 5,
            "fields": frappe.as_json(fields),
            "hasQueryHistory": 0,
        },
        page_index=0,
        page_size=5,
    )
    data = (payload.get("result") or {}).get("data")
    print(f"code={payload.get('code')} msg={payload.get('msg')}")
    print(f"data 类型: {type(data).__name__}")
    if isinstance(data, list):
        print(f"订单数: {len(data)}")
        for r in data[:3]:
            gdl = r.get("goodsDetailList") or []
            print(f"\n  {r.get('tradeNo')} shop={r.get('shopName')} totalFee={r.get('totalFee')} 明细行={len(gdl)}")
            for row in gdl[:3]:
                print(f"    goodsBarcode={row.get('goodsBarcode')} goodsName={row.get('goodsName')} sellCount={row.get('sellCount')} sellTotal={row.get('sellTotal')}")
    elif isinstance(data, dict):
        gdl = data.get("goodsDetailList") or []
        print(f"单订单 {data.get('tradeNo')} shop={data.get('shopName')} 明细行={len(gdl)}")
        for row in gdl[:3]:
            print(f"  goodsBarcode={row.get('goodsBarcode')} sellCount={row.get('sellCount')} sellTotal={row.get('sellTotal')}")
    else:
        print(f"payload: {frappe.as_json(payload)[:600]}")


def check_online_order_api3():
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)
    from datetime import timedelta
    end = frappe.utils.now_datetime()
    start = end - timedelta(days=2)
    fields = [
        "tradeOnline.tradeId", "tradeOnline.apiType", "tradeOnlineGoodsList.tradeId",
        "tradeOnline.tradeNo", "tradeOnline.shopId", "tradeOnline.shopName",
        "tradeOnline.tradeStatusExplain", "tradeOnline.totalFee", "tradeOnline.payment",
        "tradeOnline.createTime", "tradeOnline.gmtCreate", "tradeOnline.sysTradeId_String",
        "tradeOnlineGoodsList.goodsBarcode", "tradeOnlineGoodsList.goodsName",
        "tradeOnlineGoodsList.sellCount", "tradeOnlineGoodsList.sellPrice",
        "tradeOnlineGoodsList.sellTotal", "tradeOnlineGoodsList.outerId",
        "tradeOnlineGoodsList.subTradeNo", "tradeOnlineGoodsList.sysGoodsId",
        "tradeOnlineGoodsList.isGift", "tradeOnlineGoodsList.divideSellTotal",
    ]
    payload = adapter.request(
        "omsapi-business.order.get",
        {
            "startModified": start.strftime("%Y-%m-%d %H:%M:%S"),
            "endModified": end.strftime("%Y-%m-%d %H:%M:%S"),
            "shopIds": "1181057603336307712",
            "pageIndex": 0,
            "pageSize": 5,
            "fields": frappe.as_json(fields),
            "hasQueryHistory": 0,
        },
        page_index=0,
        page_size=5,
    )
    data = (payload.get("result") or {}).get("data")
    print(f"code={payload.get('code')} msg={payload.get('msg')}")
    if isinstance(data, list):
        print(f"订单数: {len(data)}")
        for r in data[:3]:
            gdl = r.get("goodsDetailList") or []
            print(f"\n  {r.get('tradeNo')} shop={r.get('shopName')} totalFee={r.get('totalFee')} 明细行={len(gdl)}")
            for row in gdl[:3]:
                print(f"    goodsBarcode={row.get('goodsBarcode')} goodsName={row.get('goodsName')} sellCount={row.get('sellCount')} sellTotal={row.get('sellTotal')}")
    elif isinstance(data, dict):
        gdl = data.get("goodsDetailList") or []
        print(f"单订单 {data.get('tradeNo')} shop={data.get('shopName')} 明细行={len(gdl)}")
        for row in gdl[:3]:
            print(f"  goodsBarcode={row.get('goodsBarcode')} sellCount={row.get('sellCount')} sellTotal={row.get('sellTotal')}")
    else:
        print(f"payload: {frappe.as_json(payload)[:600]}")


def check_online_order_full():
    conn = frappe.get_doc("Jackyun Connection", "吉客云主账号")
    adapter = JackYunAdapter(conn)
    from datetime import timedelta
    end = frappe.utils.now_datetime()
    start = end - timedelta(days=3)
    fields = [
        "tradeOnline.tradeId", "tradeOnline.apiType", "tradeOnlineGoodsList.tradeId",
        "tradeOnline.tradeNo", "tradeOnline.shopId", "tradeOnline.shopName",
        "tradeOnline.tradeStatusExplain", "tradeOnline.totalFee", "tradeOnline.payment",
        "tradeOnline.createTime", "tradeOnline.gmtCreate", "tradeOnline.sysTradeId_String",
        "tradeOnlineGoodsList.goodsBarcode", "tradeOnlineGoodsList.goodsName",
        "tradeOnlineGoodsList.sellCount", "tradeOnlineGoodsList.sellPrice",
        "tradeOnlineGoodsList.sellTotal", "tradeOnlineGoodsList.outerId",
        "tradeOnlineGoodsList.sysGoodsId", "tradeOnlineGoodsList.sysSpecId",
        "tradeOnlineGoodsList.divideSellTotal",
    ]
    payload = adapter.request(
        "omsapi-business.order.get",
        {
            "startModified": start.strftime("%Y-%m-%d %H:%M:%S"),
            "endModified": end.strftime("%Y-%m-%d %H:%M:%S"),
            "shopIds": "1181057603336307712",
            "pageIndex": 0, "pageSize": 2,
            "fields": frappe.as_json(fields),
            "hasQueryHistory": 0,
        },
        page_index=0, page_size=2,
    )
    data = (payload.get("result") or {}).get("data")
    if isinstance(data, list) and data:
        print("=== 第一条订单完整字段 ===")
        print(frappe.as_json(data[0], indent=2)[:1500])
