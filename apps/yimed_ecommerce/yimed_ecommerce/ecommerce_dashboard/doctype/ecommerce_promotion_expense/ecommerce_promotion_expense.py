from frappe.model.document import Document


class EcommercePromotionExpense(Document):
	def validate(self):
		from yimed_ecommerce.ecommerce_dashboard.promotion_import import validate_channel_store

		validate_channel_store(self.channel_name, self.store_name)
