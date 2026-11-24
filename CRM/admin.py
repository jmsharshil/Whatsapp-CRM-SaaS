from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import *
# Register your models here.

class CustomUserAdmin(BaseUserAdmin):
    list_display = ('email', 'full_name', 'is_staff', 'is_active')
    list_filter = ('is_staff', 'is_active')
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Personal info', {'fields': ('full_name',)}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'password', 'full_name', 'is_staff', 'is_active'),
        }),
    )
    search_fields = ('email', 'full_name')
    ordering = ('email',)

class CustomerAdmin(admin.ModelAdmin):
    search_fields = ['phone', 'name']
    list_display = ['name', 'phone']

admin.site.register(Customer, CustomerAdmin)
admin.site.register(Message)
admin.site.register(Conversation)
admin.site.register(User, CustomUserAdmin)
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