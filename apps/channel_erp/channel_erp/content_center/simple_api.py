"""Company-neutral Content Center service.

The Content Center is an independent enterprise file library.  Access is
derived exclusively from folder ACLs assigned to users and roles.
"""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re
import zipfile
from pathlib import Path
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime


ROOT_NAME = "企业文件"
MAX_FILES_PER_OPERATION = 50
MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024
PERMISSION_FIELDS = (
	"can_view", "can_upload", "can_download", "can_create_folder",
	"can_move", "can_delete", "can_manage",
)


def _manager(user=None):
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	return user == "Administrator" or bool(roles & {"System Manager", "Content Center Manager"})


def _require_user():
	if frappe.session.user == "Guest":
		frappe.throw(_("请先登录"), frappe.PermissionError)
	roles = set(frappe.get_roles())
	if not (_manager() or roles & {"Content Center User", "Content Center Manager"}):
		frappe.throw(_("没有内容中心访问权限"), frappe.PermissionError)


def _parse_json(value, default=None):
	if value in (None, ""):
		return default
	if isinstance(value, (list, tuple, dict)):
		return value
	try:
		return frappe.parse_json(value)
	except Exception:
		return default


def _normalize_tags(value):
	values = value if isinstance(value, (list, tuple)) else re.split(r"[,，\n\r]+", str(value or ""))
	result = []
	seen = set()
	for raw in values:
		tag = str(raw or "").strip()
		key = tag.casefold()
		if not tag or key in seen:
			continue
		if len(tag) > 30:
			frappe.throw(_("单个标签最多 30 个字符"))
		seen.add(key)
		result.append(tag)
		if len(result) > 20:
			frappe.throw(_("最多设置 20 个标签"))
	return ",".join(result)


def append_content_audit(action, asset=None, folder=None, details=None, **kwargs):
	if not frappe.db.exists("DocType", "Channel Content Audit Log"):
		return None
	values = {
		"doctype": "Channel Content Audit Log",
		"action": str(action or "")[:140],
		"asset": getattr(asset, "name", asset),
		"folder": getattr(asset, "folder", None) or getattr(folder, "name", folder),
		"user": frappe.session.user,
		"details": frappe.as_json(details or kwargs or {}),
	}
	try:
		return frappe.get_doc(values).insert(ignore_permissions=True).name
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Content Center audit")
		return None


def _root_folder():
	root = frappe.db.get_value(
		"Channel Content Folder", {"parent_channel_content_folder": ["is", "not set"]}, "name"
	)
	if root:
		return root
	doc = frappe.get_doc({
		"doctype": "Channel Content Folder", "folder_name": ROOT_NAME,
		"is_group": 1, "is_protected": 1,
	}).insert(ignore_permissions=True)
	for role, full in (("Content Center Manager", True), ("Content Center User", False)):
		if frappe.db.exists("Role", role):
			frappe.get_doc({
				"doctype": "Channel Content Folder Access", "folder": doc.name,
				"subject_type": "Role", "role": role, "can_view": 1,
				"can_download": 1, "can_upload": 1 if full else 0,
				"can_create_folder": 1 if full else 0, "can_move": 1 if full else 0,
				"can_delete": 1 if full else 0, "can_manage": 1 if full else 0,
			}).insert(ignore_permissions=True)
	return doc.name


def initialize_content_center(company=None):
	_require_user()
	return {"root_folder": _root_folder()}


def _folder_chain(folder):
	chain = []
	seen = set()
	current = folder
	while current and current not in seen:
		seen.add(current)
		row = frappe.db.get_value(
			"Channel Content Folder", current,
			["name", "folder_name", "parent_channel_content_folder"], as_dict=True,
		)
		if not row:
			break
		chain.append(row)
		current = row.parent_channel_content_folder
	return chain


def _effective_permissions(folder, user=None):
	user = user or frappe.session.user
	if _manager(user):
		return {field: True for field in PERMISSION_FIELDS}
	roles = set(frappe.get_roles(user))
	for node in _folder_chain(folder):
		rows = frappe.get_all(
			"Channel Content Folder Access", filters={"folder": node.name},
			fields=["subject_type", "role", "user", *PERMISSION_FIELDS], limit=0,
		)
		if not rows:
			continue
		matching = [row for row in rows if (
			(row.subject_type == "User" and row.user == user)
			or (row.subject_type == "Role" and row.role in roles)
		)]
		return {field: any(cint(row.get(field)) for row in matching) for field in PERMISSION_FIELDS}
	return {field: False for field in PERMISSION_FIELDS}


def can_access_folder(folder, action="can_view", user=None, raise_exception=True):
	allowed = bool(_effective_permissions(folder, user).get(action))
	if not allowed and raise_exception:
		frappe.throw(_("没有此文件夹的访问权限"), frappe.PermissionError)
	return allowed


def has_valid_asset_grant(*args, **kwargs):
	return False


def _visible_folders(action="can_view"):
	return [row for row in frappe.get_all(
		"Channel Content Folder",
		fields=["name", "folder_name", "parent_channel_content_folder", "is_protected", "lft", "rgt"],
		order_by="lft asc", limit=0,
	) if can_access_folder(row.name, action, raise_exception=False)]


def _folder_breadcrumbs(folder):
	return [{"name": row.name, "folder_name": row.folder_name} for row in reversed(_folder_chain(folder))]


def _file_bytes(file_doc):
	if not file_doc:
		return b""
	if not getattr(file_doc, "is_remote_file", False) and file_doc.file_url:
		name = os.path.basename(file_doc.file_url.split("?", 1)[0])
		base = ("private", "files") if cint(file_doc.is_private) else ("public", "files")
		path = frappe.get_site_path(*base, name)
		if os.path.isfile(path):
			with open(path, "rb") as handle:
				return handle.read()
	content = file_doc.get_content()
	return content if isinstance(content, bytes) else str(content or "").encode()


def _file_content_bytes(file_doc):
	return _file_bytes(file_doc)


def _current_file(asset):
	asset_doc = asset if hasattr(asset, "doctype") else frappe.get_doc("Channel Content Asset", asset)
	if not asset_doc.current_version:
		frappe.throw(_("文件没有可用版本"))
	version = frappe.get_doc("Channel Content Asset Version", asset_doc.current_version)
	if version.asset != asset_doc.name:
		frappe.throw(_("文件版本关系异常"))
	file_doc = frappe.get_doc("File", version.file)
	return asset_doc, version, file_doc


def _safe_asset(asset, action="can_view", active_only=True, allow_locked=False):
	_require_user()
	doc = frappe.get_doc("Channel Content Asset", asset)
	can_access_folder(doc.folder, action)
	if active_only and doc.status != "Active":
		frappe.throw(_("文件不可用"))
	if not allow_locked and cint(doc.get("content_locked")) and action in {"can_upload", "can_move", "can_delete"}:
		frappe.throw(_("文件已锁定，请先解锁"))
	return doc


def _asset_row(row, favorite_names=None, access_map=None):
	perms = _effective_permissions(row.folder)
	version = frappe.db.get_value(
		"Channel Content Asset Version", row.current_version,
		["name", "version_number", "file", "file_url"], as_dict=True,
	) if row.current_version else None
	preview = f"/api/method/channel_erp.content_center.preview_asset?asset={quote(row.name, safe='')}"
	thumbnail = None
	if str(row.mime_type or "").startswith("image/"):
		thumbnail = f"/api/method/channel_erp.content_center.get_asset_thumbnail?asset={quote(row.name, safe='')}&size=240"
	return {
		"name": row.name, "title": row.title, "folder": row.folder, "status": row.status,
		"file_name": row.file_name, "file_type": row.file_type, "mime_type": row.mime_type,
		"file_size": row.file_size or 0, "rating": row.rating or 0, "tags": row.tags or "",
		"notes": row.notes or "", "uploaded_by": row.uploaded_by, "responsible_user": row.responsible_user,
		"department": row.department, "expires_on": row.expires_on, "modified": row.modified,
		"current_version": row.current_version, "version_number": version.version_number if version else 0,
		"file": version.file if version else None, "file_url": preview if version else None,
		"thumbnail_status": "Supported" if thumbnail else "Unsupported", "thumbnail_url": thumbnail,
		"is_favorite": row.name in (favorite_names or set()),
		"last_accessed": (access_map or {}).get(row.name), "relation_count": 0,
		"is_locked": cint(row.get("content_locked")), "locked_by": row.get("locked_by"),
		"locked_at": row.get("locked_at"), "expired": False,
		"permissions": {
			"can_upload": perms["can_upload"], "can_download": perms["can_download"],
			"can_move": perms["can_move"], "can_delete": perms["can_delete"],
			"can_rate": perms["can_upload"], "can_manage": perms["can_manage"],
		},
	}


@frappe.whitelist()
def get_workspace(company=None, company_scope=None, folder=None, search=None, show_trashed=0,
	view="folder", file_type=None, uploaded_by=None, responsible_user=None, department=None,
	expiry_state=None, lock_state=None, modified_from=None, modified_to=None,
	search_all_folders=0, tags=None, tag_match="any", rating_mode="all", rating=None,
	min_rating=None, sort_by="modified", sort_order="desc", **kwargs):
	_require_user()
	root = _root_folder()
	folders = _visible_folders()
	visible = {row.name for row in folders}
	if not folder or folder not in visible:
		folder = root
	can_access_folder(folder)
	filters = {"status": "Trashed" if cint(show_trashed) else "Active", "folder": ["in", list(visible) or ["__none__"]]}
	if view == "folder" and not cint(search_all_folders):
		filters["folder"] = folder
	if view == "mine":
		filters["uploaded_by"] = frappe.session.user
	if file_type:
		filters["file_type"] = file_type
	if uploaded_by:
		filters["uploaded_by"] = uploaded_by
	if responsible_user:
		filters["responsible_user"] = responsible_user
	if department:
		filters["department"] = department
	if modified_from:
		filters["modified"] = [">=", modified_from]
	if modified_to:
		filters["modified"] = ["between", [modified_from or "1900-01-01", f"{modified_to} 23:59:59"]]
	allowed_sort = {"modified", "title", "rating", "file_size"}
	sort_by = sort_by if sort_by in allowed_sort else "modified"
	sort_order = "asc" if str(sort_order).lower() == "asc" else "desc"
	rows = frappe.get_all("Channel Content Asset", filters=filters, fields=[
		"name", "title", "folder", "status", "current_version", "file_name", "file_type", "mime_type",
		"file_size", "rating", "tags", "notes", "uploaded_by", "responsible_user", "department",
		"expires_on", "content_locked", "locked_by", "locked_at", "creation", "modified",
	], order_by=f"{sort_by} {sort_order}", limit=0)
	query = str(search or "").strip().casefold()
	if query:
		rows = [row for row in rows if query in " ".join(str(row.get(key) or "") for key in ("title", "file_name", "tags", "notes")).casefold()]
	requested_tags = _parse_json(tags, tags) or []
	if isinstance(requested_tags, str):
		requested_tags = [part.strip() for part in re.split(r"[,，]", requested_tags) if part.strip()]
	requested_keys = {str(tag).casefold() for tag in requested_tags}
	if requested_keys:
		def tag_matcher(row):
			present = {part.strip().casefold() for part in re.split(r"[,，]", row.tags or "") if part.strip()}
			return requested_keys <= present if tag_match == "all" else bool(requested_keys & present)
		rows = [row for row in rows if tag_matcher(row)]
	rating_value = cint(rating or min_rating)
	if rating_mode == "unrated": rows = [row for row in rows if not cint(row.rating)]
	elif rating_mode == "exact": rows = [row for row in rows if cint(row.rating) == rating_value]
	elif rating_mode == "at_least": rows = [row for row in rows if cint(row.rating) >= rating_value]
	favorites = set(frappe.get_all("Channel Content Favorite", filters={"user": frappe.session.user}, pluck="asset", limit=0))
	if view == "favorites": rows = [row for row in rows if row.name in favorites]
	access_rows = frappe.get_all("Channel Content Access Log", filters={"user": frappe.session.user}, fields=["asset", "last_accessed"], limit=0)
	access_map = {row.asset: row.last_accessed for row in access_rows}
	if view == "recent": rows = [row for row in rows if row.name in access_map]
	assets = [_asset_row(row, favorites, access_map) for row in rows]
	file_types = sorted({row.file_type for row in rows if row.file_type})
	tag_counts = {}
	for row in rows:
		for tag in [part.strip() for part in re.split(r"[,，]", row.tags or "") if part.strip()]:
			key = tag.casefold(); tag_counts.setdefault(key, {"tag": tag, "display_tag": tag, "normalized_tag": key, "count": 0}); tag_counts[key]["count"] += 1
	active_visible = frappe.get_all("Channel Content Asset", filters={"status": "Active", "folder": ["in", list(visible) or ["__none__"]]}, fields=["name", "file_size", "uploaded_by"], limit=0)
	current_children = [row for row in folders if row.parent_channel_content_folder == folder]
	serialized_folders = [{
		**dict(row), "permissions": {
			"can_create_folder": _effective_permissions(row.name)["can_create_folder"],
			"can_move": _effective_permissions(row.name)["can_move"],
			"can_delete": _effective_permissions(row.name)["can_delete"] and not cint(row.is_protected),
			"can_manage": _effective_permissions(row.name)["can_manage"],
		},
	} for row in folders]
	permissions = _effective_permissions(folder)
	return {
		"root_folder": root,
		"current_folder": folder, "folders": serialized_folders, "breadcrumbs": _folder_breadcrumbs(folder),
		"assets": assets, "available_types": file_types,
		"summary": {"file_count": len(assets), "total_size": sum(row["file_size"] for row in assets)},
		"view": view, "dashboard": {
			"total_files": len(active_visible), "total_size": sum(cint(row.file_size) for row in active_visible),
			"favorite_count": len(favorites), "recent_count": len(access_map),
			"my_upload_count": sum(row.uploaded_by == frappe.session.user for row in active_visible),
		},
		"tag_facets": sorted(tag_counts.values(), key=lambda row: (-row["count"], row["display_tag"])),
		"filters": {"tags": list(requested_tags), "tag_match": tag_match, "rating_mode": rating_mode, "rating": rating_value or None, "sort_by": sort_by, "sort_order": sort_order},
		"feature_flags": {"approval_workflow": False, "borrowing": False, "responsible_governance": False},
		"permissions": {**permissions, "can_view_knowledge_dashboard": False, "can_view_quality_metrics": False, "can_manage_knowledge": False},
	}


@frappe.whitelist()
def create_folder(folder_name, company=None, parent=None):
	_require_user(); parent = parent or _root_folder(); can_access_folder(parent, "can_create_folder")
	doc = frappe.get_doc({"doctype": "Channel Content Folder", "folder_name": folder_name, "parent_channel_content_folder": parent, "is_group": 1}).insert(ignore_permissions=True)
	append_content_audit("folder-create", folder=doc, details={"folder_name": doc.folder_name})
	return {"name": doc.name, "folder_name": doc.folder_name}


@frappe.whitelist()
def rename_folder(folder, folder_name):
	can_access_folder(folder, "can_move"); doc = frappe.get_doc("Channel Content Folder", folder)
	if doc.is_protected or not doc.parent_channel_content_folder: frappe.throw(_("根目录不能重命名"))
	doc.folder_name = folder_name; doc.save(ignore_permissions=True); return {"name": doc.name, "folder_name": doc.folder_name}


@frappe.whitelist()
def move_folder(folder, target_parent):
	can_access_folder(folder, "can_move"); can_access_folder(target_parent, "can_create_folder")
	doc = frappe.get_doc("Channel Content Folder", folder)
	if doc.is_protected or not doc.parent_channel_content_folder: frappe.throw(_("根目录不能移动"))
	if target_parent == folder or any(row.name == folder for row in _folder_chain(target_parent)):
		frappe.throw(_("不能移动到自身下级"))
	doc.parent_channel_content_folder = target_parent; doc.save(ignore_permissions=True); return {"name": doc.name, "parent": target_parent}


@frappe.whitelist()
def delete_folder(folder):
	can_access_folder(folder, "can_delete"); doc = frappe.get_doc("Channel Content Folder", folder)
	if doc.is_protected or not doc.parent_channel_content_folder: frappe.throw(_("根目录不能删除"))
	if frappe.db.exists("Channel Content Folder", {"parent_channel_content_folder": folder}) or frappe.db.exists("Channel Content Asset", {"folder": folder}): frappe.throw(_("只能删除空文件夹"))
	doc.delete(ignore_permissions=True); return {"deleted": folder}


@frappe.whitelist()
def get_folder_permissions(folder):
	can_access_folder(folder, "can_manage"); doc = frappe.get_doc("Channel Content Folder", folder)
	rows = frappe.get_all("Channel Content Folder Access", filters={"folder": folder}, fields=["name", "subject_type", "role", "user", *PERMISSION_FIELDS], limit=0)
	ancestors = _folder_chain(folder)[1:]
	source = {"type": "direct", "folder": folder, "folder_name": doc.folder_name} if rows else ({"type": "inherited", "folder": ancestors[0].name, "folder_name": ancestors[0].folder_name} if ancestors else {"type": "none"})
	return {"folder": folder, "folder_name": doc.folder_name, "permissions": rows, "permission_source": source, "inherited_from": list(reversed([dict(row) for row in ancestors]))}


@frappe.whitelist()
def save_folder_permissions(folder, permissions):
	can_access_folder(folder, "can_manage"); rows = _parse_json(permissions, []) or []
	prepared = []
	for row in rows:
		row = frappe._dict(row); subject = row.subject_type
		if subject not in {"Role", "User"}: frappe.throw(_("授权对象类型无效"))
		if subject == "Role" and not frappe.db.exists("Role", row.role): frappe.throw(_("角色不存在"))
		if subject == "User" and not frappe.db.exists("User", row.user): frappe.throw(_("用户不存在"))
		prepared.append(row)
	frappe.db.delete("Channel Content Folder Access", {"folder": folder})
	for row in prepared:
		frappe.get_doc({"doctype": "Channel Content Folder Access", "folder": folder, "subject_type": row.subject_type, "role": row.role if row.subject_type == "Role" else None, "user": row.user if row.subject_type == "User" else None, **{field: cint(row.get(field)) for field in PERMISSION_FIELDS}}).insert(ignore_permissions=True)
	return get_folder_permissions(folder)


def _validate_temp_file(file_name):
	file_doc = frappe.get_doc("File", file_name)
	if not cint(file_doc.is_private): frappe.throw(_("内容中心只接受私有文件"))
	if file_doc.attached_to_doctype or file_doc.attached_to_name: frappe.throw(_("上传文件已经绑定其他单据"))
	if file_doc.owner != frappe.session.user and not _manager(): frappe.throw(_("不能使用其他用户上传的文件"), frappe.PermissionError)
	return file_doc


def _hash(content): return hashlib.sha256(content).hexdigest()


@frappe.whitelist()
def register_uploaded_file(file, folder, company=None, title=None, tags=None, allow_duplicate=0):
	can_access_folder(folder, "can_upload"); file_doc = _validate_temp_file(file); content = _file_bytes(file_doc); digest = _hash(content)
	duplicate_version = frappe.db.get_value("Channel Content Asset Version", {"content_hash": digest}, "asset")
	if duplicate_version and not cint(allow_duplicate):
		existing = frappe.db.get_value("Channel Content Asset", duplicate_version, ["name", "title"], as_dict=True)
		return {"duplicate": True, "existing": existing}
	asset = frappe.get_doc({"doctype": "Channel Content Asset", "title": title or file_doc.file_name, "folder": folder, "status": "Active", "file_name": file_doc.file_name, "file_type": (Path(file_doc.file_name or "").suffix[1:] or "FILE").upper(), "mime_type": file_doc.get("content_type") or mimetypes.guess_type(file_doc.file_name or "")[0] or "application/octet-stream", "file_size": len(content), "tags": _normalize_tags(tags), "uploaded_by": frappe.session.user}).insert(ignore_permissions=True)
	version = frappe.get_doc({"doctype": "Channel Content Asset Version", "asset": asset.name, "version_number": 1, "file": file_doc.name, "file_url": file_doc.file_url, "original_filename": file_doc.file_name, "mime_type": asset.mime_type, "size_bytes": len(content), "content_hash": digest, "created_by_user": frappe.session.user}).insert(ignore_permissions=True)
	frappe.db.set_value("Channel Content Asset", asset.name, "current_version", version.name, update_modified=False)
	frappe.db.set_value("File", file_doc.name, {"attached_to_doctype": "Channel Content Asset", "attached_to_name": asset.name}, update_modified=False)
	append_content_audit("asset-create", asset, details={"file": file_doc.name})
	return {"asset": asset.name, "version": version.name, "duplicate": False}


@frappe.whitelist()
def discard_uploaded_file(file):
	file_doc = _validate_temp_file(file); file_doc.flags.allow_content_center_delete = True; file_doc.delete(ignore_permissions=True); return {"deleted": file}


@frappe.whitelist()
def add_version(asset, file, change_note=None):
	doc = _safe_asset(asset, "can_upload"); file_doc = _validate_temp_file(file); content = _file_bytes(file_doc)
	last = frappe.db.get_value("Channel Content Asset Version", {"asset": asset}, "version_number", order_by="version_number desc") or 0
	version = frappe.get_doc({"doctype": "Channel Content Asset Version", "asset": asset, "version_number": cint(last) + 1, "file": file_doc.name, "file_url": file_doc.file_url, "original_filename": file_doc.file_name, "mime_type": file_doc.get("content_type") or mimetypes.guess_type(file_doc.file_name or "")[0] or "application/octet-stream", "size_bytes": len(content), "content_hash": _hash(content), "change_note": change_note, "created_by_user": frappe.session.user}).insert(ignore_permissions=True)
	frappe.db.set_value("File", file_doc.name, {"attached_to_doctype": "Channel Content Asset", "attached_to_name": asset}, update_modified=False)
	frappe.db.set_value("Channel Content Asset", asset, {"current_version": version.name, "file_name": file_doc.file_name, "file_type": (Path(file_doc.file_name or "").suffix[1:] or "FILE").upper(), "mime_type": version.mime_type, "file_size": len(content)})
	return {"asset": asset, "version": version.name}


@frappe.whitelist()
def set_current_version(asset, version):
	_safe_asset(asset, "can_upload"); row = frappe.get_doc("Channel Content Asset Version", version)
	if row.asset != asset: frappe.throw(_("版本不属于此文件"))
	frappe.db.set_value("Channel Content Asset", asset, {"current_version": version, "file_name": row.original_filename, "file_type": (Path(row.original_filename or "").suffix[1:] or "FILE").upper(), "mime_type": row.mime_type, "file_size": row.size_bytes}); return {"asset": asset, "version": version}


@frappe.whitelist()
def update_asset(asset, title=None, tags=None, notes=None, responsible_user=None, department=None, expires_on=None, **kwargs):
	doc = _safe_asset(asset, "can_upload")
	if title is not None:
		title = str(title).strip()
		if not title: frappe.throw(_("标题不能为空"))
		doc.title = title
	if tags is not None: doc.tags = _normalize_tags(tags)
	if notes is not None: doc.notes = notes
	if responsible_user is not None: doc.responsible_user = responsible_user or None
	if department is not None: doc.department = department or None
	if expires_on is not None: doc.expires_on = expires_on or None
	doc.save(ignore_permissions=True); return {"name": doc.name, "title": doc.title, "tags": doc.tags, "notes": doc.notes}


@frappe.whitelist()
def rename_asset(asset, title): return update_asset(asset, title=title)


@frappe.whitelist()
def lock_asset(asset):
	doc = _safe_asset(asset, "can_upload")
	if cint(doc.content_locked):
		return {"asset": asset, "is_locked": True}
	doc.content_locked = 1
	doc.locked_by = frappe.session.user
	doc.locked_at = now_datetime()
	doc.save(ignore_permissions=True)
	return {"asset": asset, "is_locked": True}


@frappe.whitelist()
def unlock_asset(asset):
	doc = _safe_asset(asset, "can_upload", allow_locked=True)
	if not cint(doc.content_locked):
		return {"asset": asset, "is_locked": False}
	if doc.locked_by != frappe.session.user and not _manager():
		frappe.throw(_("只有锁定人或内容中心管理员可以解锁"), frappe.PermissionError)
	frappe.db.set_value("Channel Content Asset", asset, {
		"content_locked": 0, "locked_by": None, "locked_at": None,
	})
	return {"asset": asset, "is_locked": False}


@frappe.whitelist()
def rate_asset(asset, rating):
	doc = _safe_asset(asset, "can_upload"); rating = cint(rating)
	if rating < 0 or rating > 5: frappe.throw(_("星级必须在0到5之间"))
	doc.rating = rating; doc.save(ignore_permissions=True); return {"rating": rating, "average_rating": rating}


@frappe.whitelist()
def toggle_favorite(asset):
	doc = _safe_asset(asset); filters = {"user": frappe.session.user, "asset": doc.name}; existing = frappe.db.get_value("Channel Content Favorite", filters, "name")
	if existing: frappe.db.delete("Channel Content Favorite", {"name": existing}); active = False
	else: frappe.get_doc({"doctype": "Channel Content Favorite", **filters}).insert(ignore_permissions=True); active = True
	return {"asset": asset, "is_favorite": active}


@frappe.whitelist()
def record_asset_access(asset):
	doc = _safe_asset(asset); name = frappe.db.get_value("Channel Content Access Log", {"user": frappe.session.user, "asset": asset}, "name")
	if name: frappe.db.set_value("Channel Content Access Log", name, {"last_accessed": now_datetime(), "access_count": cint(frappe.db.get_value("Channel Content Access Log", name, "access_count")) + 1}, update_modified=False)
	else: frappe.get_doc({"doctype": "Channel Content Access Log", "user": frappe.session.user, "asset": asset, "last_accessed": now_datetime(), "access_count": 1}).insert(ignore_permissions=True)
	return {"asset": asset}


def _serve(file_doc, download=False, filename=None):
	frappe.local.response.filename = filename or file_doc.file_name
	frappe.local.response.filecontent = _file_bytes(file_doc)
	frappe.local.response.type = "download" if download else "binary"
	frappe.local.response.display_content_as = "attachment" if download else "inline"
	frappe.local.response.content_type = file_doc.get("content_type") or mimetypes.guess_type(file_doc.file_name or "")[0] or "application/octet-stream"


@frappe.whitelist()
def preview_asset(asset):
	doc = _safe_asset(asset); _asset, version, file_doc = _current_file(doc); _serve(file_doc, False, version.original_filename)


@frappe.whitelist()
def download_asset(asset):
	doc = _safe_asset(asset, "can_download"); _asset, version, file_doc = _current_file(doc); _serve(file_doc, True, version.original_filename)


@frappe.whitelist()
def get_asset_thumbnail(asset, size=240):
	doc = _safe_asset(asset); _asset, version, file_doc = _current_file(doc)
	if not str(version.mime_type or "").startswith("image/"): frappe.throw(_("此格式没有缩略图"))
	try:
		from PIL import Image
		image = Image.open(io.BytesIO(_file_bytes(file_doc))); image.thumbnail((max(64, min(cint(size), 512)),) * 2)
		if image.mode not in {"RGB", "RGBA"}: image = image.convert("RGB")
		out = io.BytesIO(); image.save(out, format="PNG", optimize=True)
		frappe.local.response.filename = "thumbnail.png"; frappe.local.response.filecontent = out.getvalue(); frappe.local.response.type = "binary"; frappe.local.response.content_type = "image/png"
	except Exception:
		frappe.throw(_("缩略图生成失败"))


@frappe.whitelist()
def get_asset_detail(asset):
	doc = _safe_asset(asset); versions = frappe.get_all("Channel Content Asset Version", filters={"asset": asset}, fields=["name", "version_number", "original_filename", "mime_type", "size_bytes", "change_note", "created_by_user", "creation"], order_by="version_number desc", limit=0)
	row = _asset_row(frappe.db.get_value("Channel Content Asset", asset, ["name", "title", "folder", "status", "current_version", "file_name", "file_type", "mime_type", "file_size", "rating", "tags", "notes", "uploaded_by", "responsible_user", "department", "expires_on", "content_locked", "locked_by", "locked_at", "creation", "modified"], as_dict=True))
	return {"asset": row, "versions": versions, "activity": [], "audit": get_content_audit(asset), "feature_flags": {"approval_workflow": False, "borrowing": False}}


@frappe.whitelist()
def get_content_audit(asset, limit=50):
	_safe_asset(asset); return frappe.get_all("Channel Content Audit Log", filters={"asset": asset}, fields=["action", "user", "details", "creation"], order_by="creation desc", limit=min(cint(limit) or 50, 100))


@frappe.whitelist()
def bulk_asset_action(assets, action, target_folder=None):
	names = _parse_json(assets, []) or []
	if len(names) > MAX_FILES_PER_OPERATION: frappe.throw(_("一次最多处理50个文件"))
	if action == "move":
		can_access_folder(target_folder, "can_upload")
	for name in names:
		doc = _safe_asset(name, "can_move" if action == "move" else "can_delete", active_only=False)
		if action == "move": doc.folder = target_folder
		elif action == "trash": doc.status = "Trashed"; doc.trashed_at = now_datetime(); doc.trashed_by = frappe.session.user
		elif action == "restore": doc.status = "Active"; doc.trashed_at = None; doc.trashed_by = None
		else: frappe.throw(_("不支持的批量操作"))
		doc.save(ignore_permissions=True)
	return {"count": len(names)}


@frappe.whitelist()
def bulk_update_assets(assets, responsible_user=None, department=None, expires_on=None, tags=None, lock_action=None):
	names = _parse_json(assets, []) or []
	for name in names: update_asset(name, tags=tags if tags is not None else None, responsible_user=responsible_user if responsible_user is not None else None, department=department if department is not None else None, expires_on=expires_on if expires_on is not None else None)
	return {"count": len(names)}


def _delete_file(file_name):
	if not file_name or not frappe.db.exists("File", file_name): return
	doc = frappe.get_doc("File", file_name); doc.flags.allow_content_center_delete = True; doc.delete(ignore_permissions=True)


def _delete_asset(asset):
	doc = _safe_asset(asset, "can_delete", active_only=False)
	files = frappe.get_all("Channel Content Asset Version", filters={"asset": asset}, pluck="file", limit=0)
	for child in ("Channel Content Favorite", "Channel Content Access Log", "Channel Content Asset Tag", "Channel Content Audit Log"):
		if frappe.db.exists("DocType", child): frappe.db.delete(child, {"asset": asset})
	frappe.db.delete("Channel Content Asset Version", {"asset": asset})
	# Detach first so Frappe's generic document deletion does not invoke the
	# attachment cascade while the File protection hook is active.
	for file_name in set(files):
		if file_name and frappe.db.exists("File", file_name):
			frappe.db.set_value("File", file_name, {
				"attached_to_doctype": None, "attached_to_name": None,
				"attached_to_field": None,
			}, update_modified=False)
	doc.flags.allow_content_center_delete = True; doc.delete(ignore_permissions=True)
	for file_name in set(files): _delete_file(file_name)


@frappe.whitelist()
def permanently_delete_assets(assets):
	names = _parse_json(assets, []) or []
	for name in names: _delete_asset(name)
	return {"count": len(names)}


@frappe.whitelist()
def copy_assets(asset_names=None, assets=None, target_folder=None):
	names = _parse_json(asset_names or assets, []) or []; can_access_folder(target_folder, "can_upload"); created = []
	from frappe.utils.file_manager import save_file
	for name in names:
		doc = _safe_asset(name, "can_download"); _asset, version, file_doc = _current_file(doc)
		copy_file = save_file(version.original_filename, _file_bytes(file_doc), "Channel Content Asset", None, is_private=1)
		copy_file.db_set({"attached_to_doctype": None, "attached_to_name": None})
		result = register_uploaded_file(copy_file.name, target_folder, title=doc.title, tags=doc.tags, allow_duplicate=1); created.append(result["asset"])
	return {"assets": created, "count": len(created)}


@frappe.whitelist()
def download_assets(assets):
	names = _parse_json(assets, []) or []
	if not names or len(names) > MAX_FILES_PER_OPERATION: frappe.throw(_("请选择1到50个文件"))
	out = io.BytesIO(); total = 0; used = set()
	with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
		for name in names:
			doc = _safe_asset(name, "can_download"); _asset, version, file_doc = _current_file(doc); content = _file_bytes(file_doc); total += len(content)
			if total > MAX_DOWNLOAD_BYTES: frappe.throw(_("批量下载总大小超过250MB"))
			filename = os.path.basename(version.original_filename or doc.file_name or f"{name}.bin"); base, ext = os.path.splitext(filename); candidate = filename; index = 2
			while candidate.casefold() in used: candidate = f"{base} ({index}){ext}"; index += 1
			used.add(candidate.casefold()); archive.writestr(candidate, content)
	frappe.local.response.filename = "content-center.zip"; frappe.local.response.filecontent = out.getvalue(); frappe.local.response.type = "download"; frappe.local.response.content_type = "application/zip"


@frappe.whitelist()
def get_content_health(company=None):
	_require_user(); visible = {row.name for row in _visible_folders()}; active = frappe.get_all("Channel Content Asset", filters={"status": "Active", "folder": ["in", list(visible) or ["__none__"]]}, fields=["name", "title", "file_size", "file_type", "current_version"], limit=0); trashed = frappe.db.count("Channel Content Asset", {"status": "Trashed", "folder": ["in", list(visible) or ["__none__"]]})
	types = {}; broken = []
	for row in active:
		types[row.file_type or "FILE"] = types.get(row.file_type or "FILE", 0) + 1
		if not row.current_version or not frappe.db.exists("Channel Content Asset Version", row.current_version): broken.append(row.name)
	return {"status": "passed" if not broken else "warning", "total_files": len(active), "total_size": sum(cint(row.file_size) for row in active), "trashed_files": trashed, "broken_versions": broken, "missing_attachments": [], "duplicate_groups": [], "eligible_trash": [], "largest_files": sorted(active, key=lambda row: cint(row.file_size), reverse=True)[:10], "type_counts": [{"file_type": key, "count": value} for key, value in sorted(types.items())], "settings": {"recycle_retention_days": 30}}


@frappe.whitelist()
def purge_expired_trash():
	_require_user(); names = frappe.get_all("Channel Content Asset", filters={"status": "Trashed"}, pluck="name", limit=0); count = 0
	for name in names:
		if can_access_folder(frappe.db.get_value("Channel Content Asset", name, "folder"), "can_delete", raise_exception=False): _delete_asset(name); count += 1
	return {"count": count}


@frappe.whitelist()
def search_content(query, **kwargs):
	message = get_workspace(search=query, search_all_folders=1, **kwargs)
	return {"available": True, "results": message["assets"], "total": len(message["assets"])}


@frappe.whitelist()
def get_index_status(asset): _safe_asset(asset); return {"status": "Unsupported", "error_summary": _("纯文件中心未启用全文索引")}


@frappe.whitelist()
def get_content_insights(asset): _safe_asset(asset); return {"available": False}


@frappe.whitelist()
def list_share_links(asset): _safe_asset(asset); return []


def run_content_center_maintenance():
	return {"status": "ok", "attachment_sync": "disabled"}


# Batch inspection reports are deliberately independent from Content Center,
# but their stable RPC namespace remains here for existing Desk scripts.
@frappe.whitelist()
def get_batch_inspection_report(batch_no):
	from channel_erp.batch_inspection import get_batch_inspection_report as handler
	return handler(batch_no)


@frappe.whitelist()
def set_batch_inspection_report(batch_no, file):
	from channel_erp.batch_inspection import set_batch_inspection_report as handler
	return handler(batch_no, file)


@frappe.whitelist()
def delete_batch_inspection_report(batch_no):
	from channel_erp.batch_inspection import delete_batch_inspection_report as handler
	return handler(batch_no)


@frappe.whitelist()
def download_batch_inspection_report(batch_no, preview=0):
	from channel_erp.batch_inspection import download_batch_inspection_report as handler
	return handler(batch_no, preview)


@frappe.whitelist()
def get_delivery_note_inspection_report_summary(delivery_note):
	from channel_erp.batch_inspection import get_delivery_note_inspection_report_summary as handler
	return handler(delivery_note)


@frappe.whitelist()
def export_delivery_note_inspection_reports_zip(delivery_note):
	from channel_erp.batch_inspection import export_delivery_note_inspection_reports_zip as handler
	return handler(delivery_note)
