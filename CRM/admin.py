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
@admin.register(ClientAccount)
class ClientAccountAdmin(admin.ModelAdmin):
    exclude = ('tech_provider',)

    def save_model(self, request, obj, form, change):
        if not getattr(obj, 'tech_provider_id', None):
            # Assign default tech provider (the first organization)
            org = Organization.objects.first()
            if org:
                obj.tech_provider = org
        super().save_model(request, obj, form, change)

admin.site.register(ClientMember)
admin.site.register(Template)
admin.site.register(ConversationState)
admin.site.register(Campaign)
admin.site.register(CampaignRecipient)

admin.site.register(WhatsAppMessage)
admin.site.register(WhatsAppSession)

admin.site.register(MetaRegistrationDetails)