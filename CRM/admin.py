from django.contrib import admin
from .models import *
# Register your models here.

admin.site.register(Customer)
admin.site.register(Message)
admin.site.register(Conversation)
admin.site.register(User)
admin.site.register(EmailVerificationCode)
admin.site.register(Organization)
admin.site.register(OrganizationMember)
admin.site.register(WABAAccount)
admin.site.register(ClientAccount)
admin.site.register(ClientMember)
admin.site.register(Template)
admin.site.register(ConversationState)
admin.site.register(Campaign)
admin.site.register(CampaignRecipient)

admin.site.register(WhatsAppMessage)
admin.site.register(WhatsAppSession)

admin.site.register(MetaRegistrationDetails)

# Avantika Models
admin.site.register(AvantikaContact)
admin.site.register(AvantikaTemplate)