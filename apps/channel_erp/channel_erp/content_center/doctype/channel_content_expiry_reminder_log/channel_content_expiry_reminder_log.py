import hashlib

from frappe.model.document import Document


class ChannelContentExpiryReminderLog(Document):
	def autoname(self):
		key = "\n".join(
			(str(value or "") for value in (self.asset, self.recipient, self.reminder_date, self.stage))
		)
		self.name = hashlib.sha256(key.encode()).hexdigest()
