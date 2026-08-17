from django.db import models
import uuid
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.utils import timezone

class Customer(models.Model):
    phone = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=255)

    def __str__(self):
        return f"{self.name} ({self.phone})"
    

class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    full_name = models.CharField(max_length=150, blank=True, default="")
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return self.email


class Organization(models.Model):
    """TechNova's own organization — the Tech Provider."""
    owner = models.OneToOneField(User, on_delete=models.CASCADE, related_name="organization")
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    website = models.URLField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    
class OrganizationMember(models.Model):
    ROLE_CHOICES = [
        ("manager", "Manager"),
        ("sales", "Salesperson"),
    ]
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="members")
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="membership")
    full_name = models.CharField(max_length=255)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.full_name} ({self.role})"


class ClientAccount(models.Model):
    """
    Each business onboarded by TechNova.
    Has its own WABA, its own team, and its own isolated data.
    """
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("active", "Active"),
        ("suspended", "Suspended"),
    ]

    tech_provider = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="clients", help_text="TechNova org that onboarded this client")
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    website = models.URLField(blank=True, null=True)
    industry = models.CharField(max_length=100, blank=True, null=True)

    # Per-client WABA credentials (set via Embedded Signup)
    waba_id = models.CharField(max_length=100, blank=True, null=True)
    phone_number_id = models.CharField(max_length=100, blank=True, null=True)
    business_id = models.CharField(max_length=100, blank=True, null=True)
    waba_name = models.CharField(max_length=255, blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    access_token = models.TextField(blank=True, null=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} (client of {self.tech_provider.name})"

    def waba_connected(self):
        return bool(self.waba_id and self.phone_number_id and self.access_token)


class Conversation(models.Model):
    STATUS_CHOICES = [
        ("prospect", "Prospect"),
        ("confirmed", "Confirmed"),
    ]
    
    CONFIRM_CHOICES = [
        ("pending", "Pending"),
        ("confirmed", "Confirmed"),
        ("cancelled", "Cancelled"),
    ]
    
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='conversations')
    client = models.ForeignKey('ClientAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='conversations')
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="prospect")
    client_name = models.CharField(max_length=255, null=True, blank=True)
    phone_number_id = models.CharField(max_length=100, blank=True, null=True)

    bot_state = models.CharField(max_length=50, blank=True, default="INIT")
    bot_metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"Conversation {self.id} with {self.customer.name}"


class Template(models.Model):

    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("PAUSED", "Paused"),
    ]

    CATEGORY_CHOICES = [
        ("MARKETING", "Marketing"),
        ("UTILITY", "Utility"),
        ("AUTHENTICATION", "Authentication"),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="templates")
    name = models.CharField(max_length=255)
    template_id = models.CharField(max_length=255, blank=True, null=True)
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default="UTILITY")
    language = models.CharField(max_length=50, default="en")
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="PENDING")
    header_type = models.CharField(max_length=50, blank=True, null=True)
    header_text = models.TextField(blank=True, null=True)
    body_text = models.TextField()
    footer_text = models.TextField(blank=True, null=True)
    buttons = models.JSONField(default=list, blank=True)
    variables_count = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.status})"

    
class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    client = models.ForeignKey('ClientAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='messages')
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='messages')
    content = models.TextField()
    reply_of = models.TextField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    client_name = models.CharField(max_length=255, null=True, blank=True)

    
    DIRECTION_CHOICES = [
        ('outbound', 'Outbound'),  # Sent by you
        ('inbound', 'Inbound'),    # Received from customer
    ]
    
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('sent', 'Sent'),
        ('delivered', 'Delivered'),
        ('read', 'Read'),
        ('failed', 'Failed'),
    ]
    
    MESSAGE_TYPE_CHOICES = [
        ('text', 'Text'),
        ('template', 'Template'),
        ('image', 'Image'),
        ('document', 'Document'),
        ('video', 'Video'),
    ]
    
    direction = models.CharField(max_length=20, choices=DIRECTION_CHOICES, default='outbound', help_text="Message direction: sent by us or received from customer")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued', help_text="Meta delivery status")
    message_type = models.CharField(max_length=20, choices=MESSAGE_TYPE_CHOICES, default='text')
    template_name = models.CharField(max_length=255, blank=True, null=True, help_text="Template name if this was a template message")
    template = models.ForeignKey(Template, on_delete=models.SET_NULL, null=True, blank=True, related_name='sent_messages', help_text="Link to template used (optional)")
    meta_message_id = models.CharField(max_length=255, blank=True, null=True, help_text="Meta's unique message ID (for webhook status updates)")
    error_message = models.TextField(blank=True, null=True, help_text="Error details if status=failed")
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.direction} | {self.status} | {self.content[:50]}"
    def __str__(self):
        return self.content

    
class EmailVerificationCode(models.Model):
    email = models.EmailField()
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    def is_expired(self):
        return (timezone.now() - self.created_at).seconds > 300  # 5 minutes


# ── WABA (TechNova level) ────────────────────────────────────────────────────

class WABAAccount(models.Model):
    """
    TechNova-level WABA. Used before per-client WABAs are set up.
    After full multi-tenant setup, each ClientAccount has its own WABA fields.
    """
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("connected", "Connected"),
        ("error", "Error"),
        ("disconnected", "Disconnected"),
    ]
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="waba_account")
    waba_id = models.CharField(max_length=100, blank=True, null=True)
    phone_number_id = models.CharField(max_length=100, blank=True, null=True)
    business_id = models.CharField(max_length=100, blank=True, null=True)
    waba_name = models.CharField(max_length=255, blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    access_token = models.TextField(blank=True, null=True)
    auth_code = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    error_message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.organization.name} — WABA {self.waba_id or 'pending'}"

    def is_connected(self):
        return self.status == "connected"


# ── CLIENT (Onboarded Businesses) ────────────────────────────────────────────

class ClientMember(models.Model):
    """
    A user who belongs to a ClientAccount (not TechNova directly).
    Each client has its own owner, managers, and sales team.
    """
    ROLE_CHOICES = [
        ("owner", "Owner"),
        ("manager", "Manager"),
        ("sales", "Salesperson"),
    ]
    client = models.ForeignKey(ClientAccount, on_delete=models.CASCADE, related_name="members")
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="client_membership")
    full_name = models.CharField(max_length=255)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.full_name} ({self.role}) @ {self.client.name}"


class ConversationState(models.Model):
    """
    Tracks per-conversation chatbot progress.
    - stage:            greeting → qualifying → complete → human_handoff
    - collected_fields: JSON dict of qualifying data gathered so far
                        e.g. {"name": "Rahul", "service": "plumbing"}
    - is_complete:      True once lead_threshold is met → customer promoted to lead
    """
    STAGE_CHOICES = [
        ("greeting",      "Greeting"),
        ("qualifying",    "Qualifying"),
        ("complete",      "Complete"),
        ("human_handoff", "Human Handoff"),
        ("Request Quotation", "Request Quotation"),
        ("Get Price", "Get Price"),
        ("Talk to Sales", "Talk to Sales"),
        ("Exploring Menu", "Exploring Menu"),
    ]

    conversation = models.OneToOneField(Conversation, on_delete=models.CASCADE, related_name="chatbot_state")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="conversation_states")
    stage = models.CharField(max_length=30, choices=STAGE_CHOICES, default="greeting")
    collected_fields = models.JSONField(default=dict)
    is_complete = models.BooleanField(default=False)
    human_handoff = models.BooleanField(default=False)
    message_count = models.IntegerField(default=0)
    last_bot_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"State[{self.stage}] conv={self.conversation_id}"


# ── Campaigns ────────────────────────────────────────────────────────────────

class Campaign(models.Model):
    """
    A broadcast campaign: one approved WhatsApp template → N phone numbers.
    Created via POST /api/campaigns/. Sends immediately via Meta Cloud API.
    """

    STATUS_CHOICES = [
        ("queued",    "Queued"),
        ("running",   "Running"),
        ("completed", "Completed"),
        ("failed",    "Failed"),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="campaigns")
    template = models.ForeignKey(Template, on_delete=models.SET_NULL, null=True, related_name="campaigns")
    name = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="queued")
    # Snapshot of template name at send time (template may be deleted later)
    template_name = models.CharField(max_length=255, blank=True)
    # Variables supplied by the user: {"1": "Hello", "2": "Promo"}
    variables = models.JSONField(default=dict, blank=True)
    total_count = models.IntegerField(default=0)   # total recipients
    sent_count = models.IntegerField(default=0)   # successfully sent
    failed_count = models.IntegerField(default=0)   # failed (Meta rejected)
    error_log = models.TextField(blank=True)      # summary of errors
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} [{self.status}] — {self.organization.name}"


class CampaignRecipient(models.Model):
    """
    One row per phone number in a Campaign.
    Tracks per-number send status for auditability.
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("sent",    "Sent"),
        ("failed",  "Failed"),
    ]

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="recipients")
    phone_number = models.CharField(max_length=30)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    meta_message_id = models.CharField(max_length=255, blank=True, null=True)
    error_detail = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.phone_number} → {self.status}"
    

# ── jms_whatsappbot ────────────────────────────────────────────────────────────────

class Document(models.Model):
    name           = models.CharField(max_length=255)
    file           = models.FileField(upload_to="documents/")
    file_type      = models.CharField(max_length=10)
    extracted_text = models.TextField(blank=True)
    char_count     = models.IntegerField(default=0)
    uploaded_at    = models.DateTimeField(auto_now_add=True)
    is_indexed     = models.BooleanField(default=False)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["-uploaded_at"]

from django.db import models

class ChatSession(models.Model):
    STAGE_CHOICES = [
        ('ideation', 'Ideation'),
        ('mvp', 'MVP Development'),
        ('traction', 'Early Traction'),
        ('scaling', 'Scaling'),
        ('incubated', 'Incubated'),
        ('established', 'Established'),
    ]
    FUNDING_CHOICES = [
        ('no_funding', 'No Funding Yet'),
        ('bootstrapped', 'Bootstrapped/Self-Funded'),
        ('seeking_angel', 'Seeking Angel Investment'),
        ('seed_funded', 'Seed Funded'),
        ('series_a', 'Series A+'),
    ]
    
    title                    = models.CharField(max_length=200, default="New Chat")
    created_at               = models.DateTimeField(auto_now_add=True)
    
    # Founder Profile
    founder_name             = models.CharField(max_length=150, blank=True, null=True)
    startup_idea             = models.TextField(blank=True, null=True)
    startup_stage            = models.CharField(max_length=20, choices=STAGE_CHOICES, blank=True, null=True)
    location                 = models.CharField(max_length=150, blank=True, null=True)
    funding_stage            = models.CharField(max_length=20, choices=FUNDING_CHOICES, blank=True, null=True)
    questionnaire_completed  = models.BooleanField(default=False)

    def __str__(self):
        return self.title

    class Meta:
        ordering = ["-created_at"]


class ChatMessage(models.Model):
    ROLE_CHOICES   = [("user", "User"), ("assistant", "Assistant")]
    SOURCE_CHOICES = [("kb", "Knowledge Base"), ("llm", "LLM")]

    session    = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name="messages")
    role       = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content    = models.TextField()
    source     = models.CharField(max_length=5, choices=SOURCE_CHOICES, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


#gigatel 
from django.db import models
from django.db.models.signals import pre_delete
from django.dispatch import receiver


class WhatsAppSession(models.Model):
    """Tracks per-user conversation state."""

    STATE_CHOICES = [
        ("INIT",                    "Awaiting greeting"),
        ("AWAIT_CIRCUIT_DIGITS",            "Awaiting last 4 digits of Circuit ID"),
        ("AWAIT_CIRCUIT_DIGITS_FOR_TICKET", "Awaiting last 4 digits (ticket check)"),
        ("AWAIT_CIRCUIT_CONFIRM",           "Confirming matched circuit"),
        ("AWAIT_CIRCUIT_CONFIRM_FOR_TICKET","Confirming matched circuit (ticket check)"),
        ("MENU",                    "Main Menu"),
        ("CIRCUIT_LIST",            "Showing Circuit List"),
        ("CIRCUIT_LIST_FOR_TICKET", "Showing Circuit List for Ticket"),
        ("COMPLAINT_TYPE",          "Selecting Fault Type"),
        ("AWAIT_OTDR",              "Awaiting OTDR Yes/No"),
        ("AWAIT_OTDR_FROM",         "Awaiting OTDR From (source station)"),
        ("AWAIT_OTDR_TO",           "Awaiting OTDR To (destination station)"),
        ("AWAIT_OTDR_VALUE",        "Awaiting OTDR Value (reading in metres)"),
        ("AWAIT_REMARK",            "Awaiting Plain Remark"),
        ("AWAIT_OTDR_IMAGE",        "Awaiting First OTDR Image"),
        ("AWAIT_OTDR_IMAGE2",       "Awaiting Optional Second OTDR Image"),
        ("AWAIT_FAULT_SIDE_CHECK", "Validating OTDR fault side"),
        ("AWAIT_RAISE_ANYWAY",     "Awaiting Raise Ticket Anyway? Yes/No"),
        ("DONE",                    "Flow Complete"),
        # Globe Star States
        ("GS_INIT",                 "GS: Awaiting greeting"),
        ("GS_MENU",                 "GS: Main Menu"),
        ("GS_PRODUCTS",             "GS: Product List"),
        ("GS_PRODUCT_DETAIL",       "GS: Product Detail"),
        ("GS_AWAIT_CAPACITY",       "GS: Awaiting Capacity"),
        ("GS_AWAIT_HEAD",           "GS: Awaiting Head"),
        ("GS_AWAIT_APPLICATION",    "GS: Awaiting Application"),
        ("GS_AWAIT_PUMP_TYPE",      "GS: Awaiting Pump Type"),
        ("GS_AWAIT_SPECIFIC_GRAVITY", "GS: Awaiting Specific Gravity"),
        ("GS_DONE",                 "GS: Flow Complete"),
    ]

    mobile_number        = models.CharField(max_length=15, unique=True)
    customer_id          = models.IntegerField(null=True, blank=True)
    customer_company_id  = models.IntegerField(null=True, blank=True)
    contact_person_name  = models.CharField(max_length=200, blank=True)
    customer_email = models.EmailField(blank=True, default="")
    selected_circuit_id  = models.TextField(blank=True, default="")
    nature_of_fault_id   = models.IntegerField(null=True, blank=True)
    ticket_id         = models.CharField(max_length=50,  blank=True, default="")
    ticket_raised_on  = models.CharField(max_length=30,  blank=True, default="")
    fault_label          = models.CharField(max_length=50, blank=True)
    otdr_applicable      = models.BooleanField(null=True)
    ticket_status_local = models.CharField(max_length=40, blank=True, default="")

    # OTDR step-by-step fields
    otdr_from        = models.CharField(max_length=200, blank=True, default="")
    otdr_to          = models.CharField(max_length=200, blank=True, default="")
    otdr_value       = models.CharField(max_length=50,  blank=True, default="")
    otdr_remark      = models.CharField(max_length=500, blank=True, default="")
    otdr_image1_path = models.CharField(max_length=500, blank=True, default="")  
    otdr_image1_url  = models.TextField(blank=True, default="")
    otdr_image2_url  = models.TextField(blank=True, default="")

    fault_side       = models.CharField(max_length=20, blank=True, default="")  # Gigatel / Customer
    circuit_numeric_id = models.IntegerField(null=True, blank=True)

    # Globe Star step-by-step fields
    gs_selected_product  = models.CharField(max_length=200, blank=True, default="")
    gs_capacity          = models.CharField(max_length=100, blank=True, default="")
    gs_head              = models.CharField(max_length=100, blank=True, default="")
    gs_application       = models.CharField(max_length=200, blank=True, default="")
    gs_pump_type         = models.CharField(max_length=100, blank=True, default="")
    gs_specific_gravity  = models.CharField(max_length=100, blank=True, default="")

    state      = models.CharField(max_length=50, choices=STATE_CHOICES, default="INIT")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.mobile_number} — {self.state}"


class WhatsAppMessage(models.Model):
    """Stores every inbound and outbound message."""

    DIRECTION_CHOICES = [("IN", "Inbound"), ("OUT", "Outbound")]

    mobile_number       = models.CharField(max_length=15, db_index=True)
    whatsapp_message_id = models.CharField(max_length=200, blank=True)
    direction           = models.CharField(max_length=3, choices=DIRECTION_CHOICES)
    message_type        = models.CharField(max_length=30, default="text")
    body                = models.TextField(blank=True)
    created_at          = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.direction} | {self.mobile_number} | {self.body[:60]}"
    
    @receiver(pre_delete, sender=WhatsAppSession)
    def _delete_related_messages(sender, instance, **kwargs):
        WhatsAppMessage.objects.filter(mobile_number=instance.mobile_number).delete()

class MetaRegistrationDetails(models.Model):
    """Stores the 19 required Meta Registration fields for a ClientAccount."""
    client = models.OneToOneField('ClientAccount', on_delete=models.CASCADE, related_name='meta_registration')
    
    legal_business_name = models.CharField(max_length=255, blank=True, null=True)
    business_type = models.CharField(max_length=100, blank=True, null=True)
    official_website = models.URLField(blank=True, null=True)
    domain_linked_email = models.EmailField(blank=True, null=True)
    
    business_logo = models.FileField(upload_to='meta_docs/logos/', blank=True, null=True)
    verification_document = models.FileField(upload_to='meta_docs/verification/', blank=True, null=True)
    
    gst_number = models.CharField(max_length=50, blank=True, null=True)
    dedicated_phone_number = models.CharField(max_length=50, blank=True, null=True)
    meta_facebook_account = models.CharField(max_length=255, blank=True, null=True)
    
    facebook_email = models.CharField(max_length=255, blank=True, null=True)
    facebook_password = models.CharField(max_length=255, blank=True, null=True)
    
    international_card_available = models.BooleanField(default=False)
    international_card_number = models.CharField(max_length=100, blank=True, null=True)
    international_card_image = models.FileField(upload_to='meta_docs/cards/', blank=True, null=True)
    
    bot_use_case_brief = models.TextField(blank=True, null=True)
    bot_use_case_document = models.FileField(upload_to='meta_docs/use_cases/', blank=True, null=True)
    business_display_name = models.CharField(max_length=255, blank=True, null=True)
    business_category = models.CharField(max_length=100, blank=True, null=True)
    business_description = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Meta Registration for {self.client.name}"


class AvantikaContact(models.Model):
    """Fixed list of users allowed to trigger Avantika Bot."""
    phone = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=500, blank=True, null=True)
    
    def __str__(self):
        return f"{self.name} ({self.phone})"


class AvantikaTemplate(models.Model):
    """Dynamic templates for Avantika Bot."""
    base_image = models.ImageField(upload_to='avantika/templates/')
    name_x = models.IntegerField(default=100)
    name_y = models.IntegerField(default=100)
    font_size = models.IntegerField(default=60)
    text_color = models.CharField(max_length=20, default="#000000")
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        status = "Active" if self.is_active else "Inactive"
        return f"Avantika Template {self.id} ({status})"

class AvantikaCampaignHistory(models.Model):
    """History of sent Avantika campaigns."""
    contact = models.ForeignKey(AvantikaContact, on_delete=models.SET_NULL, null=True, blank=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    name = models.CharField(max_length=255, blank=True, null=True)
    template = models.ForeignKey(AvantikaTemplate, on_delete=models.SET_NULL, null=True, blank=True)
    campaign_run_id = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=50, default="Sent")
    error_message = models.TextField(blank=True, null=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.phone} - {self.status} at {self.sent_at}"


class NavratriRegistration(models.Model):
    name = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=50)
    email = models.EmailField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    aadhar_card_number = models.CharField(max_length=50, blank=True, null=True)
    
    GUARDIAN_RELATION_CHOICES = [
        ("father", "Father"),
        ("mother", "Mother"),
        ("brother", "Brother"),
        ("sister", "Sister"),
        ("husband", "Husband"),
        ("wife", "Wife"),
        ("legal guardian", "Legal Guardian"),
        ("friend", "Friend"),
    ]

    guardian_relation = models.CharField(max_length=100, choices=GUARDIAN_RELATION_CHOICES, blank=True, null=True)
    guardian_name = models.CharField(max_length=255, blank=True, null=True)
    guardian_phone_number = models.CharField(max_length=50, blank=True, null=True)
    guardian_email = models.EmailField(blank=True, null=True)
    
    PASS_TYPE_CHOICES = [
        ("single_day_pass", "Single Day Pass"),
        ("season_pass", "Season Pass"),
    ]

    pass_type = models.CharField(max_length=100, choices=PASS_TYPE_CHOICES, blank=True, null=True)
    select_date = models.CharField(max_length=100, blank=True, null=True)
    pass_quantity = models.IntegerField(default=1)
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} - {self.phone_number}"
