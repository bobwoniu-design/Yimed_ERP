from __future__ import annotations

import io
import mimetypes
import os
import re
import zipfile
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.utils import cint, flt


FIELDNAME = "custom_inspection_report_attachment"
SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
MAX_REPORT_BYTES = 100 * 1024 * 1024
MAX_EXPORT_BYTES = 200 * 1024 * 1024
MAX_EXPORT_BATCHES = 500


def _require_user():
	if frappe.session.user == "Guest":
		frappe.throw(_("请先登录"), frappe.PermissionError)
	user = frappe.db.get_value("User", frappe.session.user, ["enabled", "user_type"], as_dict=True)
	if not user or not cint(user.enabled) or user.user_type != "System User":
		frappe.throw(_("仅启用的系统用户可以管理检测报告"), frappe.PermissionError)


def _batch(batch_no):
	_require_user()
	if not batch_no or not frappe.db.exists("Batch", batch_no):
		frappe.throw(_("批次不存在"))
	doc = frappe.get_doc("Batch", batch_no)
	if not frappe.has_permission("Batch", "read", doc=doc):
		frappe.throw(_("没有该批次的读取权限"), frappe.PermissionError)
	return doc


def _lock_batch(batch_no):
	frappe.db.sql("select name from `tabBatch` where name=%s for update", (batch_no,))


def _lock_file(file_name):
	frappe.db.sql("select name from `tabFile` where name=%s for update", (file_name,))


def _file_for_url(file_url):
	if not file_url:
		return None
	name = frappe.db.get_value(
		"File", {"file_url": file_url, "is_folder": 0}, "name", order_by="creation desc"
	)
	return frappe.get_doc("File", name) if name else None


def _owned_by_batch(file_doc, batch_no):
	return bool(
		file_doc
		and file_doc.attached_to_doctype == "Batch"
		and file_doc.attached_to_name == batch_no
		and file_doc.attached_to_field == FIELDNAME
	)


def _serialize(batch_doc, file_doc=None):
	file_url = batch_doc.get(FIELDNAME)
	file_doc = file_doc if file_doc is not None else _file_for_url(file_url)
	valid = bool(file_url and file_doc and cint(file_doc.is_private))
	endpoint = "/api/method/channel_erp.content_center.download_batch_inspection_report"
	return {
		"batch_no": batch_doc.name,
		"item_code": batch_doc.item,
		"has_report": valid,
		"file_name": file_doc.file_name if valid else None,
		"file_size": cint(file_doc.file_size) if valid else 0,
		"file_type": file_doc.file_type if valid else None,
		"mime_type": (
			mimetypes.guess_type(file_doc.file_name or "")[0] or "application/octet-stream"
			if valid else None
		),
		"is_private": 1 if valid else 0,
		"status": "available" if valid else ("broken" if file_url else "missing"),
		"preview_url": f"{endpoint}?{urlencode({'batch_no': batch_doc.name, 'preview': 1})}" if valid else None,
		"download_url": f"{endpoint}?{urlencode({'batch_no': batch_doc.name})}" if valid else None,
		"permissions": {
			"can_view": True,
			"can_upload": True,
			"can_replace": valid,
			"can_delete": valid,
			"can_download": valid,
		},
	}


def get_batch_inspection_report(batch_no):
	return _serialize(_batch(batch_no))


def _validate_temporary_file(file_name):
	if not file_name or not frappe.db.exists("File", file_name):
		frappe.throw(_("临时上传文件不存在"))
	doc = frappe.get_doc("File", file_name)
	if doc.is_folder or not cint(doc.is_private):
		frappe.throw(_("检测报告必须是私有文件"))
	if doc.owner != frappe.session.user:
		frappe.throw(_("只能登记当前用户上传的文件"), frappe.PermissionError)
	if doc.attached_to_doctype or doc.attached_to_name or doc.attached_to_field:
		frappe.throw(_("该文件已经绑定其他记录"))
	if frappe.db.exists("File", {"file_url": doc.file_url, "name": ["!=", doc.name]}):
		frappe.throw(_("该物理文件已被其他附件记录使用，请重新上传独立文件"))
	if cint(doc.file_size) <= 0 or cint(doc.file_size) > MAX_REPORT_BYTES:
		frappe.throw(_("检测报告大小必须在 1 字节到 100 MB 之间"))
	if os.path.splitext(doc.file_name or "")[1].lower() not in SUPPORTED_EXTENSIONS:
		frappe.throw(_("检测报告仅支持 PDF、PNG、JPG 或 JPEG"))
	content = _file_bytes(doc)
	try:
		ext = os.path.splitext(doc.file_name or "")[1].lower()
		if ext == ".pdf":
			from pypdf import PdfReader
			reader = PdfReader(io.BytesIO(content), strict=False)
			if reader.is_encrypted and not reader.decrypt(""):
				raise ValueError("encrypted")
			if not reader.pages:
				raise ValueError("empty")
		else:
			from PIL import Image
			with Image.open(io.BytesIO(content)) as image:
				if image.width * image.height > 80_000_000:
					raise ValueError("pixel-limit")
				image.verify()
	except Exception:
		frappe.throw(_("检测报告文件已损坏或格式与扩展名不符"))
	return doc


def _delete_owned_file(file_doc, batch_no):
	if not _owned_by_batch(file_doc, batch_no):
		return False
	# A normally-created report is never shared.  This defensive check also
	# protects records manually repaired by an administrator.
	if frappe.db.count("Batch", filters={FIELDNAME: file_doc.file_url, "name": ["!=", batch_no]}):
		return False
	if frappe.db.exists("File", {"file_url": file_doc.file_url, "name": ["!=", file_doc.name]}):
		# Frappe may deduplicate physical bytes. Delete this owned File record
		# without removing a path still referenced by another File record.
		frappe.db.set_value("File", file_doc.name, "file_url", None, update_modified=False)
		file_doc.reload()
	file_doc.flags.allow_content_center_delete = True
	file_doc.delete(ignore_permissions=True)
	return True


def set_batch_inspection_report(batch_no, file):
	batch_doc = _batch(batch_no)
	new_file = _validate_temporary_file(file)
	frappe.db.savepoint("set_batch_inspection_report")
	try:
		_lock_batch(batch_no)
		batch_doc.reload()
		_lock_file(file)
		# Revalidate after acquiring the File row lock. This closes the race where
		# two batches try to claim the same temporary File concurrently.
		new_file = _validate_temporary_file(file)
		old_file = _file_for_url(batch_doc.get(FIELDNAME))
		if batch_doc.get(FIELDNAME) == new_file.file_url:
			frappe.throw(_("该文件已经是当前检测报告"))
		if frappe.db.exists("Batch", {FIELDNAME: new_file.file_url, "name": ["!=", batch_no]}):
			frappe.throw(_("该文件已经用于其他批次"))
		frappe.db.set_value(
			"File", new_file.name,
			{"attached_to_doctype": "Batch", "attached_to_name": batch_no,
				"attached_to_field": FIELDNAME}, update_modified=False,
		)
		frappe.db.set_value("Batch", batch_no, FIELDNAME, new_file.file_url)
		# Delete only files created by this simplified workflow.  Migrated files
		# may still belong to a Content Asset and are intentionally not detached.
		if old_file and old_file.name != new_file.name:
			_delete_owned_file(old_file, batch_no)
	except Exception:
		frappe.db.rollback(save_point="set_batch_inspection_report")
		raise
	return get_batch_inspection_report(batch_no)


def delete_batch_inspection_report(batch_no):
	batch_doc = _batch(batch_no)
	frappe.db.savepoint("delete_batch_inspection_report")
	try:
		_lock_batch(batch_no)
		batch_doc.reload()
		old_file = _file_for_url(batch_doc.get(FIELDNAME))
		frappe.db.set_value("Batch", batch_no, FIELDNAME, None)
		deleted = _delete_owned_file(old_file, batch_no) if old_file else False
	except Exception:
		frappe.db.rollback(save_point="delete_batch_inspection_report")
		raise
	return {"batch_no": batch_no, "has_report": False, "deleted_file": bool(deleted)}


def _file_bytes(file_doc):
	from channel_erp.content_center.simple_api import _file_content_bytes
	return _file_content_bytes(file_doc)


def download_batch_inspection_report(batch_no, preview=0):
	batch_doc = _batch(batch_no)
	file_doc = _file_for_url(batch_doc.get(FIELDNAME))
	if not file_doc or not cint(file_doc.is_private):
		frappe.throw(_("该批次没有可用的检测报告"))
	content = _file_bytes(file_doc)
	frappe.local.response.filename = file_doc.file_name or f"{batch_no}-inspection-report"
	frappe.local.response.filecontent = content
	frappe.local.response.type = "download"
	frappe.local.response.content_type = (
		mimetypes.guess_type(file_doc.file_name or "")[0] or "application/octet-stream"
	)
	frappe.local.response.display_content_as = "inline" if cint(preview) else "attachment"
	return {"batch_no": batch_no, "filename": frappe.local.response.filename}


def _delivery_batches(delivery_note):
	doc = frappe.get_doc("Delivery Note", delivery_note)
	rows = {}
	for item in doc.items:
		if item.serial_and_batch_bundle:
			for entry in frappe.get_all(
				"Serial and Batch Entry",
				filters={"parent": item.serial_and_batch_bundle, "batch_no": ["is", "set"]},
				fields=["batch_no", "item_code", "qty"], limit=0,
			):
				key = (entry.item_code or item.item_code, entry.batch_no)
				rows[key] = rows.get(key, 0) + abs(flt(entry.qty))
		elif item.batch_no:
			key = (item.item_code, item.batch_no)
			rows[key] = rows.get(key, 0) + abs(flt(item.qty))
	return doc, rows


def get_delivery_note_inspection_report_summary(delivery_note):
	_require_user()
	if not delivery_note or not frappe.db.exists("Delivery Note", delivery_note):
		frappe.throw(_("出库单不存在"))
	doc, pairs = _delivery_batches(delivery_note)
	if not frappe.has_permission("Delivery Note", "read", doc=doc):
		frappe.throw(_("没有出库单读取权限"), frappe.PermissionError)
	if len(pairs) > MAX_EXPORT_BATCHES:
		frappe.throw(_("单次最多处理 500 个批次"))
	batch_nos = list(dict.fromkeys(batch_no for _item, batch_no in pairs))
	batch_values = {
		row.name: row for row in frappe.get_all(
			"Batch", filters={"name": ["in", batch_nos or ["__none__"]]},
			fields=["name", "item", FIELDNAME], limit=0,
		)
	}
	urls = list({row.get(FIELDNAME) for row in batch_values.values() if row.get(FIELDNAME)})
	files = {
		row.file_url: row for row in frappe.get_all(
			"File", filters={"file_url": ["in", urls or ["__none__"]], "is_folder": 0},
			fields=["name", "file_url", "file_name", "file_size", "file_type", "is_private"],
			order_by="creation desc", limit=0,
		)
	}
	rows = []
	for (item_code, batch_no), qty in pairs.items():
		batch = batch_values.get(batch_no)
		file_doc = files.get(batch.get(FIELDNAME)) if batch else None
		valid = bool(file_doc and cint(file_doc.is_private))
		ext = os.path.splitext(file_doc.file_name or "")[1].lower() if valid else ""
		status = "available" if valid and ext in SUPPORTED_EXTENSIONS else (
			"unsupported" if valid else "missing"
		)
		rows.append({
			"item_code": item_code, "batch_no": batch_no, "qty": qty,
			"has_report": valid, "exportable": status == "available", "status": status,
			"file_name": file_doc.file_name if valid else None,
			"file_size": cint(file_doc.file_size) if valid else 0,
			"file_type": file_doc.file_type if valid else None,
		})
	missing = [
		{"item_code": row["item_code"], "batch_no": row["batch_no"], "status": row["status"]}
		for row in rows if not row["has_report"]
	]
	issues = [
		{"item_code": row["item_code"], "batch_no": row["batch_no"],
			"status": row["status"], "file_name": row["file_name"]}
		for row in rows if row["status"] != "available"
	]
	return {
		"delivery_note": delivery_note, "company": doc.company, "docstatus": doc.docstatus,
		"batches": rows, "batch_count": len(rows),
		"report_count": sum(1 for row in rows if row["has_report"]),
		"exportable_count": sum(1 for row in rows if row["exportable"]),
		"missing_count": len(missing),
		"unsupported_count": sum(1 for row in rows if row["status"] == "unsupported"),
		"missing_batches": missing, "issues": issues,
		"permissions": {"can_export": True},
	}


def _safe_zip_name(value, fallback="file"):
	name = os.path.basename(str(value or "").replace("\\", "/"))
	name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "-", name).strip(" .-")
	return (name or fallback)[:180]


def export_delivery_note_inspection_reports_zip(delivery_note):
	summary = get_delivery_note_inspection_report_summary(delivery_note)
	warnings = []
	actual_size = 0
	emitted_batches = set()
	used_names = set()
	output = io.BytesIO()
	archive = zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True)
	for row in summary["batches"]:
		batch_no = row["batch_no"]
		if batch_no in emitted_batches:
			continue
		emitted_batches.add(batch_no)
		if not row["exportable"]:
			warnings.append(f"{batch_no}: {row['status']}")
			continue
		batch_doc = frappe.get_doc("Batch", batch_no)
		file_doc = _file_for_url(batch_doc.get(FIELDNAME))
		if not file_doc or not cint(file_doc.is_private):
			warnings.append(f"{batch_no}: missing")
			continue
		content = _file_bytes(file_doc)
		actual_size += len(content)
		if actual_size > MAX_EXPORT_BYTES:
			archive.close()
			frappe.throw(_("检测报告压缩包总大小不能超过 200 MB"))
		candidate = _safe_zip_name(file_doc.file_name, f"{batch_no}-report")
		stem, ext = os.path.splitext(candidate)
		index = 2
		while candidate.casefold() in used_names:
			candidate = f"{stem} ({index}){ext}"
			index += 1
		used_names.add(candidate.casefold())
		archive.writestr(candidate, content)
	archive.close()
	if not used_names:
		frappe.throw(_("当前出库单没有可导出的检测报告"))
	frappe.local.response.filename = f"{delivery_note}-检测报告.zip"
	frappe.local.response.filecontent = output.getvalue()
	frappe.local.response.type = "download"
	frappe.local.response.content_type = "application/zip"
	frappe.local.response.display_content_as = "attachment"
	return {
		"filename": frappe.local.response.filename, "batch_count": summary["batch_count"],
		"report_count": len(used_names), "missing_count": summary["missing_count"],
		"warnings": warnings,
	}
