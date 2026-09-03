import frappe
from frappe.model.document import Document


class JackyunSalesChannel(Document):
    pass


PAIR_FIELDS = (
    ("online_platform_code", "online_platform_name"),
    ("platform_shop_id", "platform_shop_name"),
    ("company_code", "company_name"),
    ("department_id", "department_name"),
    ("warehouse_code", "warehouse_name"),
    ("category_id", "category_name"),
)


def _distinct_pairs(code_field, name_field):
    rows = frappe.db.sql(
        f"""
        select distinct `{code_field}` code, `{name_field}` label
          from `tabJackyun Sales Channel`
         where ifnull(`{code_field}`, '') <> '' or ifnull(`{name_field}`, '') <> ''
         order by `{name_field}`, `{code_field}`
        """,
        as_dict=True,
    )
    return [
        {"code": frappe.utils.cstr(row.code), "name": frappe.utils.cstr(row.label)}
        for row in rows
    ]


@frappe.whitelist()
def get_controlled_field_options():
    """Return choices already observed in the synchronized JackYun master data."""
    result = {
        f"{code_field}:{name_field}": _distinct_pairs(code_field, name_field)
        for code_field, name_field in PAIR_FIELDS
    }
    for fieldname in ("responsible_user_name",):
        result[fieldname] = [
            row[0]
            for row in frappe.db.sql(
                f"""
                select distinct `{fieldname}`
                  from `tabJackyun Sales Channel`
                 where ifnull(`{fieldname}`, '') <> ''
                 order by `{fieldname}`
                """
            )
        ]
    return result
