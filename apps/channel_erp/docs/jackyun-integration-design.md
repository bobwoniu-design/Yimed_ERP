# JackYun（吉客云）集成 — 设计文档

> 第一个里程碑：把「从吉客云拉取 SKU → 落库 ERPNext」的最小闭环跑通，并搭好多平台可扩展的骨架。
> 本文档同时作为规格说明（spec）与未来派单的 brief。

---

## 1. 目标与范围

**本期做：**
1. 吉客云（JackYun，域名 `open.jackyun.com`）的鉴权 + 签名客户端（Python）
2. 「拉 SKU（商品）→ 映射 → 幂等 upsert 进 ERPNext `Item`」完整跑通
3. 同步日志 + 原始数据镜像 + 外部 ID 映射，三张自定义 DocType 落地

**本期不做（留到后续）：**
- 销售订单 / 采购订单 / 库存的拉取（方法映射先登记，逻辑下一期）
- OAuth2 鉴权模式（本期用老的 `appkey + secret + md5 签名`）
- 推送到吉客云（只拉不推）

---

## 2. 整体架构

```
吉客云 API (open.jackyun.com)
        │  ① 签名 + POST + 分页拉取
        ▼
┌─ 镜像层 ─────────────────────────────┐
│  Jackyun Sync Log    （一次同步批次）   │
│  Jackyun Raw Record  （原始记录，未加工） │
└──────────────────────────────────────┘
        │  ② 映射（adapter.transform）
        ▼
┌─ 落库层 ─────────────────────────────┐
│  ERPNext 原生 DocType（本期：Item）     │
└──────────────────────────────────────┘
        │  ③ 记映射
        ▼
   External ID Mapping（外部ID ↔ ERPNext 单据）
```

**关键原则：**
- 拉回来的数据先原样存进 `Jackyun Raw Record`（可重放、可审计、不污染标准表）
- 落库只往 ERPNext 标准 DocType upsert，不动其表结构
- 靠 `External ID Mapping` 实现增量/去重（否则每次同步都会重复建 Item）

---

## 3. DocType 定义（4 个）

> 命名约定：英文 DocType 名用 `Jackyun` 前缀（对应真实产品名 JackYun），中文 label 用「吉客云」。
> 吉客云同步单据归属 `module = "Jackyun Integration"`；除同步计划子表外，业务单据均为非子表。

### 3.1 Jackyun Connection（吉客云连接配置）

存 API 凭证。普通 DocType（可配多个账号，本期只用 1 条）。

| 字段名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| title | Data | ✅ | 显示名，如「吉客云主账号」 |
| enabled | Check | | 是否启用（定时任务只扫启用的） |
| gateway | Data | ✅ | API 网关，默认 `https://open.jackyun.com/open/openapi/do` |
| app_key | Data | ✅ | appKey |
| app_secret | Password | ✅ | secret（加密存储） |
| version | Data | ✅ | 默认 `v1.0` |
| last_sync_at | Datetime | | 最近一次同步完成时间（watermark 起点） |

> OAuth2 字段（account_token/refresh_token）本期不加，切 OAuth2 模式时再补。

### 3.2 Jackyun Sync Log（吉客云同步日志）

一次同步 = 一条。`naming_series = JKSY-.#####`。

| 字段名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| connection | Link → Jackyun Connection | ✅ | 用哪个连接 |
| resource | Select | ✅ | 资源类型（见 §4.4） |
| method | Data | ✅ | 实际调用的吉客云方法名，如 `erp.storage.goodslist` |
| status | Select | ✅ | `进行中 / 成功 / 部分失败 / 失败` |
| started_at | Datetime | | 开始时间 |
| finished_at | Datetime | | 结束时间 |
| last_modified_at | Datetime | | 增量 watermark（`gmtModified` 时间窗） |
| last_page_index | Int | | 分页游标（`pageIndex`） |
| total_created | Int | | 新建条数 |
| total_updated | Int | | 更新条数 |
| total_failed | Int | | 失败条数 |
| error_log | Long Text | | 错误摘要 |

### 3.3 Jackyun Raw Record（吉客云原始记录）

原始镜像，一条外部记录 = 一条。`naming_series = JKRAW-.#####`。

| 字段名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| sync_log | Link → Jackyun Sync Log | ✅ | 属于哪次同步 |
| resource | Select | ✅ | 资源类型 |
| external_id | Data | ✅ | 吉客云侧唯一 ID（如 SKU 编码） |
| raw_data | JSON | ✅ | 原始返回字段（未加工） |
| content_hash | Data | | 原始内容哈希，用于判断是否变化 |
| processed | Check | | 是否已落库 |
| processed_at | Datetime | | 落库时间 |

> 唯一约束：`(sync_log, resource, external_id)` 防止同批次重复。

### 3.4 External ID Mapping（外部ID映射）

平台无关的映射表，管所有平台/所有资源类型。

| 字段名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| platform | Data | ✅ | 平台标识，默认 `jackyun` |
| resource | Select | ✅ | 资源类型 |
| external_id | Data | ✅ | 外部 ID |
| erpnext_doctype | Link → DocType | ✅ | 对应 ERPNext DocType（如 `Item`） |
| erpnext_name | Dynamic Link → erpnext_doctype | ✅ | 对应 ERPNext 单据 name |
| last_synced_at | Datetime | | 最近同步时间 |

> 唯一约束：`(platform, resource, external_id)`。
> 这是增量的关键：同步时先查映射，命中 → 更新，未命中 → 新建 + 记映射。

---

## 4. 适配器接口设计（`jackyun.py`）

### 4.1 目录结构

```
channel_erp/
├── integrations/
│   ├── __init__.py
│   ├── base.py          # BaseAdapter 抽象接口（平台无关）
│   └── jackyun.py       # JackYunAdapter（吉客云实现）
└── channel_erp/doctype/...   # 上述 4 个 DocType
```

### 4.2 签名算法（老版 appkey 模式）

```
sign = md5( ( secret + 排序后的 key+value 拼接 + secret ).lower() )
```

- 参与签名的参数按 key **字典序排序**
- 每个参数拼成 **`key + value`（没有 `=` 号、不加分隔符）**
- 整个串 `secret + 拼接结果 + secret` **先转小写，再 md5**
- `bizcontent` 的 JSON 序列化必须与 JS 的 `JSON.stringify` 一致：**无空格（`separators=(",", ":")`）、不转义非 ASCII（`ensure_ascii=False`）**，否则签名对不上

### 4.3 公共参数与请求

```
POST {gateway}
Content-Type: application/x-www-form-urlencoded

公共参数：appkey / bizcontent / contenttype=json / method / timestamp / version=v1.0
+ sign（按上面算法算出的签名）
```

- `bizcontent`：业务参数 JSON 序列化后作为字符串值
- **分页**：`pageIndex` 从 **0** 起，逐页拉直到返回空
- **重试**：3 次指数退避
- **错误分类**：`SIGNATURE`（签名错）/ `UNSUBSCRIBED`（未订阅接口）/ `PARAMETER`（参数错）/ `NETWORK`（网络）

### 4.4 资源 → 方法名映射（本期只实现 SKU，其余先登记）

| 资源（resource） | 吉客云方法 | 落库 DocType | 本期 |
|---|---|---|---|
| `sku` | `erp.storage.goodslist` | `Item` | ✅ 实现 |
| `product_bundle` | `erp-goods.goods.listgoodspackage` | `Item` + `Product Bundle` | ✅ 实现 |
| `company` | `erp.company.query` | 现有 `Company` 映射及标准重命名 | ✅ 实现 |
| `department` | `erp.depart.query` | 标准 `Department`（按 `companyCode` 归属公司） | ✅ 实现 |
| `category` | `erp.goodscate.get` | `Jackyun Goods Category` | ✅ 实现 |
| `sales_channel` | `erp.sales.get` | `Jackyun Sales Channel` | ✅ 实现 |
| `sales_order` | `oms.trade.fullinfoget` | `Sales Order` 草稿 | ✅ 实现（创建/修改时间增量） |
| `purchase_order` | `erp.purch.search` | `Purchase Order` 草稿 | ✅ 实现（明细聚合 + 创建/修改时间增量） |
| `purchase_receipt` | `erp-busiorder.goodsdocin.search` | `Purchase Receipt` 草稿 | ✅ 实现（规格行聚合；表头/明细双增量） |
| `delivery_note` | `erp-busiorder.goodsdocout.search` | `Delivery Note` 草稿 | ✅ 实现（逐日拉取 + count核对 + 双增量） |
| `sales_return` | `erp-busiorder.goodsdocin.search`（105） | 退货 `Delivery Note` 草稿 | ✅ 实现（count核对，不关联未提交原单） |
| `purchase_return` | `erp-busiorder.goodsdocout.search`（205） | 退货 `Purchase Receipt` 草稿 | ✅ 实现（count核对，不关联未提交原单） |
| `stock_transfer` | `erp.allocate.search` | `Stock Entry` 草稿 | ✅ 实现（跨公司拆分为调出/调入两张草稿） |
| `stocktake` | `wms.stocktake.get` | `Stock Reconciliation` 草稿 | ✅ 实现（仅完成状态生成业务草稿） |
| `batch` | `erp.goodsbatchinfo.get` | `Batch` 主档 | ✅ 实现（不写库存数量） |
| `inventory` | `erp.stockquantity.get` | `Jackyun Inventory Snapshot`（不修改库存账） | ✅ 实现 |
| `supplier` | `erp.vend.get` | 标准 `Supplier`（全量 + 修改时间增量） | ✅ 实现 |
| `customer` | `crm.customer.list.customized` | 标准 `Customer`（电商按 ID/编号按需；线下来源全量 + 增量） | ✅ 实现 |
| `warehouse` | `erp.warehouse.get` | 标准 `Warehouse` | ✅ 实现 |

采购入库规格行若只返回 `serialSourceId` 而未内嵌 `serialNo`，适配器会批量调用
`erp.storage.goodsdocserial` 分页补齐唯一码；完整结果保存在原始记录，草稿收货单明细显示摘要。

组合装同步先执行 SKU 同步，为每个吉客云 `skuId` 保存到 ERPNext Item 的外部映射，
随后按 `maxSkuId` 游标拉取虚拟组套。增量同步使用 `lastModifiedStart` / `lastModifiedEnd`
并按一天切分时间窗口。组合装父 Item 为非库存销售商品，`goodsPackageDetail` 写入
`Product Bundle Item`；已有库存流水的父 Item 不会被自动转为非库存商品。

### 4.5 接口签名（BaseAdapter / JackYunAdapter）

```python
class BaseAdapter:
    def sign(self, params: dict, secret: str) -> str: ...
    def request(self, method: str, bizcontent: dict, page_index: int) -> dict: ...
    def pull(self, resource: str, since: datetime) -> Iterator[dict]: ...   # 拉原始数据
    def transform(self, raw: dict) -> dict: ...                             # 原始 → ERPNext 字段
    def upsert(self, resource: str, mapped: dict) -> str: ...               # 幂等写库 + 记映射
```

`JackYunAdapter(BaseAdapter)` 实现上面 5 个方法；`sign` 用 §4.2 的算法，`request` 用 §4.3 的协议。

---

## 5. 幂等 upsert 规则

落库前必走这套（`adapter.upsert`）：

1. 按 `(platform='jackyun', resource, external_id)` 查 `External ID Mapping`
2. **命中** → 拿到 `erpnext_name` → `frappe.get_doc(doctype, name)` 更新字段
3. **未命中** → `frappe.get_doc(doctype)` 新建 → `insert()` → 写一条映射
4. 用 `content_hash` 对比：原始数据没变化 → 跳过写库（避免无谓写入）
5. 更新 `last_synced_at`

---

## 6. 定时任务

在 `channel_erp/hooks.py` 每 15 分钟运行一次计划检查器：

```python
scheduler_events = {
    "cron": {
        "*/15 * * * *": ["channel_erp.integrations.tasks.process_due_schedules"],
    },
}
```

- 走 Frappe Scheduler + Long Queue，站点 Scheduler 已启用
- 每个资源在 `Jackyun Connection.sync_schedules` 中独立配置 30/60/120/360/720/1440 分钟
- 默认订单、出入库、退货、调拨为 30 分钟，库存/采购订单/盘点为 60 分钟，基础资料为 360 分钟
- 相同连接和资源使用唯一后台任务 ID 防重，重复点击不会并发执行
- 连接表单提供“立即同步 → 同步全部/选择类型”手动入口

---

## 7. 后续扩展点

- **加新平台（淘宝/京东…）**：新增一个 `xxx.py` 实现 `BaseAdapter` + 一条 `Jackyun Connection` 同类配置 + 映射表加 `platform` 值，主流程不动
- **加新资源**：在 §4.4 表加一行 + 写 `transform` 映射
- **切 OAuth2 模式**：给连接配置加 token 字段 + 复用 Frappe 的 `Connected App` / `Token Cache`
- **推送到吉客云**：在 adapter 加 `push()`（本期明确不做）

---

## 8. 验收标准（第一个里程碑）

- [ ] `bench --site yimed.local migrate` 后 4 个 DocType 在界面可见、可正常增删改
- [ ] 配好一条 `Jackyun Connection`（真实 appKey/secret）
- [ ] 手动触发一次 SKU 同步：能连上吉客云、拉到真实 SKU 数据
- [ ] `Jackyun Raw Record` 里能看到原始记录，`External ID Mapping` 里 SKU ↔ Item 对应正确
- [ ] ERPNext `Item` 里出现拉下来的 SKU，且重复同步不产生重复 Item（幂等验证）
- [ ] `Jackyun Sync Log` 状态、计数、错误日志正确
