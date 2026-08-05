from django.urls import path
from CRM.META.campaign_views import *
from CRM.META.meta_status_views import *
from CRM.META.webhook_views import *
from CRM.META.waba_views import *
from CRM.META.client_views import *
from .views import *
from CRM.views import MetaConversationMessageListView
from CRM.jmschatagents_views import *
from CRM.gigatel_views import GigatelDataExportView
import CRM.gigatel_views
from CRM.globestar_views import GlobestarDataAPIView

urlpatterns = [

    # Dedicated Gigatel Webhook
    path('api/gigatel/webhook/', CRM.gigatel_views.WebhookView.as_view(), name="gigatel-webhook-dedicated"),

    # Old webhook path → now uses multi-tenant WhatsAppWebhookView
    path('webhook/', WhatsAppWebhookView.as_view(), name="webhook"),

    # path('', home, name='home'),


    path("api/industry/dashboard/",                              dashboard_summary,       name="industry_dashboard"),
    path("api/industry/customers/",                              customer_list,           name="industry_customer_list"),
    path("api/industry/customers/<str:phone>/",                  customer_detail,         name="industry_customer_detail"),
    path("api/industry/conversations/",                          conversation_list,       name="industry_conversation_list"),
    path("api/industry/conversations/<int:conversation_id>/messages/", conversation_messages, name="industry_conversation_messages"),
    path("api/v1/conversations/<int:conversation_id>/messages/", MetaConversationMessageListView.as_view()),
    path("api/industry/messages/recent/",                        recent_messages,         name="industry_recent_messages"),
    path("api/industry/analytics/messages/",                     analytics_messages,      name="industry_analytics_messages"),
    path("api/industry/analytics/customers/",                    analytics_customers,     name="industry_analytics_customers"),
    path("api/industry/search/",                                 global_search,           name="industry_search"),


    path("api/sync-tech-provider-waba/",SyncTechProviderWabaView.as_view()),
    path("api/signup/", SignUpView.as_view()),

    # ── Auth ──────────────────────────────────────────────────────────────────
    path("api/auth/login/", LoginView.as_view()),
    path("api/auth/reset-pin/send-code/", ResetPinSendCodeView.as_view()),
    path("api/auth/reset-pin/verify-code/", ResetPinVerifyView.as_view()),

    # ── Current user profile ──────────────────────────────────────────────────
    path("api/user/me/", UserMeView.as_view()),
    path("api/customer/",MetaCustomerListView.as_view()),

    # ── Organization ──────────────────────────────────────────────────────────
    path("api/organization/",                  OrganizationCreateView.as_view()),
    path("api/organization/members/",          OrganizationMemberCreateView.as_view()),
    path("api/organization/members/list/",     OrganizationMemberListView.as_view()),
    path("api/organization/members/<int:pk>/", OrganizationMemberDetailView.as_view()),

    # ── Meta / WhatsApp Embedded Signup ───────────────────────────────────────
    path("api/meta/embedded-signup/start/", EmbeddedSignupView.as_view()),
    path("api/meta/waba/status/",           WABAStatusView.as_view()),
    path("api/meta/waba/disconnect/",       WABADisconnectView.as_view()),

    # ── Analytics ─────────────────────────────────────────────────────────────
    path("api/analytics/", MetaDashboardAPIView.as_view()),

    # ── Templates ─────────────────────────────────────────────────────────────
    path("api/templates/",               TemplateListCreateView.as_view(), name="template-list-create"),
    path("api/templates/sync-all/",      TemplateSyncAllView.as_view(),    name="template-sync-all"),
    path("api/templates/<int:pk>/",      TemplateDetailView.as_view(),     name="template-detail"),
    path("api/templates/<int:pk>/sync/", TemplateSyncView.as_view(),       name="template-sync"),
    

    # ── Campaigns ─────────────────────────────────────────────────────────────
    path("api/campaigns/",          CampaignListCreateView.as_view(), name="campaign-list-create"),
    path("api/campaigns/<int:pk>/", CampaignDetailView.as_view(),     name="campaign-detail"),

    path("api/leads-prospects/", LeadsProspectsView.as_view(), name="leads-prospects"),
    path("api/webhook/whatsapp/", WhatsAppWebhookView.as_view(), name="whatsapp-webhook"),
    path("api/meta/account-status/",WhatsAppAccountStatusView.as_view(),name="meta-account-status"),

    path("api/techprovider/clients/",          TechProviderClientListView.as_view()),
    path("api/techprovider/clients/<int:pk>/", TechProviderClientDetailView.as_view()),
    
    path("api/meta-registration/", ClientMetaRegistrationView.as_view()),
    path("api/techprovider/clients/<int:pk>/meta-registration/", TechProviderMetaRegistrationView.as_view()),
    
    # ── Gigatel Export ────────────────────────────────────────────────────────
    path("api/gigatel/", GigatelDataExportView.as_view(), name="gigatel-export"),
    
    # ── Globestar ─────────────────────────────────────────────────────────────
    path("api/globestar/data/", GlobestarDataAPIView.as_view(), name="globestar-data"),
    
    # ── Avantika Bot ──────────────────────────────────────────────────────────
    path("avantika-template/", avantika_template_view, name="avantika-template-view"),
    path("avantika-campaign/", avantika_campaign_view, name="avantika-campaign-view"),
    path("avantika-upload-csv/", avantika_upload_csv_view, name="avantika-upload-csv-view"),
    path("api/avantika/history/", avantika_history_api, name="avantika-history-api"),
]