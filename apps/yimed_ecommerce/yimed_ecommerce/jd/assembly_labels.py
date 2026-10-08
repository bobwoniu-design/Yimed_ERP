"""Read-only 75 mm assembly labels, using component batches from the stocking pool."""
from io import BytesIO
from datetime import timedelta
from dateutil.relativedelta import relativedelta
import base64
import frappe
from frappe import _
from frappe.utils import flt, getdate
from pypdf import PdfReader, PdfWriter
from barcode import Code128
from yimed_ecommerce.jd import workflow
from yimed_ecommerce.jd.report_pdf import _validate_batch_access
from frappe.utils.pdf import get_chrome_pdf


def earliest_batch(batches):
    dated = [b for b in batches if b.get("expiry_date")]
    return min(dated, key=lambda b: (getdate(b["expiry_date"]), b["batch_no"])) if dated else None


def shelf_life(batch):
    """Express the selected batch's date interval without mixing component dates."""
    if not batch or not batch.get("manufacturing_date") or not batch.get("expiry_date"):
        return "未维护"
    start, end = getdate(batch["manufacturing_date"]), getdate(batch["expiry_date"])
    if end < start:
        return "日期异常"
    for boundary in (end, end + timedelta(days=1)):
        delta = relativedelta(boundary, start)
        if delta.days == 0 and (delta.years or delta.months):
            return (f"{delta.years}年" if delta.years else "") + (f"{delta.months}个月" if delta.months else "")
    return f"{(end - start).days + 1}天"


def build_labels(import_batch, platform_item):
    batch = _validate_batch_access(import_batch)
    orders = workflow._batch_orders(batch.name)
    for po in orders:
        po.check_permission("read")
    bundles = workflow._bundle_requirements(orders)
    if platform_item not in bundles:
        frappe.throw(_("请选择当前批次的组套商品。"))
    entry = bundles[platform_item]
    item = frappe.get_doc("Item", platform_item)
    pools = frappe.get_all("JD Stocking Pool Item", filters={"import_batch": batch.name},
        fields=["stock_item", "batch_no", "required_qty", "shortage_qty", "stocked_qty"], order_by="creation")
    assembled = any(p.stock_item == platform_item and flt(p.stocked_qty) > 0 for p in pools)
    batch_rows, seen = [], set()
    for p in pools:
        if p.stock_item not in entry["components"]:
            continue
        qty = flt(p.required_qty) - flt(p.shortage_qty) - flt(p.stocked_qty) if assembled else flt(p.stocked_qty)
        if qty <= 1e-6 or (p.stock_item, p.batch_no) in seen:
            continue
        seen.add((p.stock_item, p.batch_no))
        b = frappe.get_doc("Batch", p.batch_no) if p.batch_no else None
        if b and b.item != p.stock_item:
            frappe.throw(_("组件批号与物料不一致，请核对备货数据。"))
        batch_rows.append({"item": p.stock_item, "batch_no": (b.batch_id or b.name) if b else "未维护",
            "manufacturing_date": b.manufacturing_date if b else None, "expiry_date": b.expiry_date if b else None})
    for code in entry["components"]:
        if not any(r["item"] == code for r in batch_rows):
            batch_rows.append({"item": code, "batch_no": "未维护"})
    earliest = earliest_batch(batch_rows)
    complete_dates = all(r.get("expiry_date") for r in batch_rows)
    def metadata(field):
        if item.get(field):
            return item.get(field)
        return " / ".join(dict.fromkeys(frappe.get_doc("Item", code).get(field) or "未维护" for code in entry["components"]))
    skus = sorted({str(row.jd_sku) for po in orders for row in po.items if row.platform_item == platform_item and row.jd_sku})
    if not skus:
        frappe.throw(_("组套商品未维护京东 SKU。"))
    labels = []
    for sku in skus:
        svg = Code128(sku).render({"write_text": False, "module_height": 16, "quiet_zone": 3})
        labels.append({"name": item.item_name, "registration": metadata("custom_jd_registration"),
            "specification": metadata("custom_jd_specification"), "manufacturer": metadata("custom_jd_manufacturer"),
            "batches": batch_rows, "earliest": earliest if complete_dates else None,
            "shelf_life": shelf_life(earliest if complete_dates else None),
            "date_warning": "部分批号或失效日期未维护" if not complete_dates else "",
            "sku": sku, "barcode": "data:image/svg+xml;base64," + base64.b64encode(svg).decode()})
    return labels


def parse_print_rows(rows, platform_item=None, copies=1):
    if rows is None:
        rows = [{"platform_item": platform_item, "copies": copies}]
    elif isinstance(rows, str):
        rows = frappe.parse_json(rows)
    if not isinstance(rows, list) or not rows or len(rows) > 200:
        frappe.throw(_("请选择要打印的组套商品。"))
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("platform_item") or row["platform_item"] in seen:
            frappe.throw(_("打印商品无效或重复。"))
        try:
            quantity = int(str(row.get("copies", "")))
        except (TypeError, ValueError):
            frappe.throw(_("打印份数必须为整数。"))
        if not 1 <= quantity <= 10000:
            frappe.throw(_("每项打印份数须为 1 至 10000。"))
        seen.add(row["platform_item"])
        result.append((row["platform_item"], quantity))
    return result


@frappe.whitelist()
def download_assembly_labels(import_batch, platform_item=None, copies=1, rows=None):
    requests = parse_print_rows(rows, platform_item, copies)
    labels = [(label, quantity) for item, quantity in requests for label in build_labels(import_batch, item)]
    if sum(quantity for label, quantity in labels) > 10000:
        frappe.throw(_("一次最多打印 10000 张标签，请分批打印。"))
    writer = PdfWriter()
    for label, copies in labels:
        for font_size in (7, 6.5, 6):
            html = frappe.render_template("templates/jd_assembly_label.html", {"label": label, "font_size": font_size})
            pdf = get_chrome_pdf(None, html, {"page-width": "80mm", "page-height": "50mm", "orientation": "Portrait",
                "margin-top": "2mm", "margin-bottom": "2mm", "margin-left": "2mm", "margin-right": "2mm"}, None, pdf_generator="chrome")
            reader = PdfReader(BytesIO(pdf))
            if len(reader.pages) == 1:
                break
        if len(reader.pages) != 1:
            frappe.throw(_("标签内容超过 8×5 厘米，请精简商品标签资料后重试；所有批号均须保留。"))
        for _copy in range(copies):
            writer.add_page(reader.pages[0])
    output = BytesIO(); writer.write(output)
    frappe.local.response.filename = "JD-Assembly-Labels.pdf"
    frappe.local.response.filecontent = output.getvalue()
    frappe.local.response.type = "pdf"
