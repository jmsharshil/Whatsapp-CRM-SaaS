"""
user_auth/utils.py
==================
Single source of truth for role-based data access.

5 user types:
  super_admin     → TechNova org owner  → sees ALL data across all clients
  technova_mgr    → TechNova manager    → sees ALL data across all clients
  technova_sales  → TechNova salesperson→ sees assigned conversations only
  client_owner/manager → ClientMember  → sees only their ClientAccount's data
  client_sales    → ClientMember       → sees only assigned client convos
"""

from django.db.models import Q
from rest_framework.response import Response
from .models import Conversation


def get_user_access(user):
    """
    Returns (role, scope_obj, base_qs, error_response).

    role        : str  — super_admin | technova_mgr | technova_sales |
                         owner | manager | sales
    scope_obj   : Organization | ClientAccount | None
    base_qs     : Conversation queryset already filtered for this user
    error_response : None if OK, else DRF Response(403) ready to return
    """

    # 1. TechNova Super Admin (org owner)
    if hasattr(user, "organization") and user.organization is not None:
        return "super_admin", user.organization, Conversation.objects.all(), None

    # 2. TechNova Internal Team (OrganizationMember)
    membership = getattr(user, "membership", None)
    if membership is not None:
        org = membership.organization
        if membership.role == "manager":
            return "technova_mgr", org, Conversation.objects.all(), None
        if membership.role == "sales":
            qs = Conversation.objects.filter(
                Q(assigned_to=membership) | Q(assigned_to__isnull=True)
            )
            return "technova_sales", org, qs, None

    # 3. Client User (ClientMember)
    client_membership = getattr(user, "client_membership", None)
    if client_membership is not None:
        client = client_membership.client
        if client.status == "suspended":
            return None, None, None, Response(
                {"error": "Account suspended. Contact TechNova support."},
                status=403,
            )
        client_qs = Conversation.objects.filter(client=client)
        if client_membership.role in ("owner", "manager"):
            return client_membership.role, client, client_qs, None
        if client_membership.role == "sales":
            qs = client_qs.filter(
                Q(assigned_to=client_membership) | Q(assigned_to__isnull=True)
            )
            return "sales", client, qs, None

    # 4. No valid role
    return None, None, None, Response(
        {
            "error": "Unauthorized",
            "detail": "Your account is not linked to any organization. Complete setup at /setup.",
        },
        status=403,
    )


def get_portal_type(user):
    """Returns 'admin' | 'client' | None."""
    if hasattr(user, "organization") and user.organization is not None:
        return "admin"
    if getattr(user, "membership", None) is not None:
        return "admin"
    if getattr(user, "client_membership", None) is not None:
        return "client"
    return None