from django.contrib import admin
from .models import *
# Register your models here.

class CustomerAdmin(admin.ModelAdmin):
    search_fields = ['phone', 'name']
    list_display = ['name', 'phone']

admin.site.register(Customer, CustomerAdmin)
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

# Other remaining models
admin.site.register(Document)
admin.site.register(ChatSession)
admin.site.register(ChatMessage)
admin.site.register(NavratriRegistration)


admin.site.register(MetaRegistrationDetails)

# Avantika Models
admin.site.register(AvantikaContact)
admin.site.register(AvantikaTemplate)

class AvantikaCampaignHistoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'phone', 'template', 'campaign_run_id', 'status']
    search_fields = ['name', 'phone', 'campaign_run_id']
    list_filter = ['status', 'template']

admin.site.register(AvantikaCampaignHistory, AvantikaCampaignHistoryAdmin)