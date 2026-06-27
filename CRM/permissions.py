from rest_framework import permissions

class IsOwner(permissions.BasePermission):
    """Allow only organization owners (admins)."""
    def has_permission(self, request, view):
        return hasattr(request.user, "organization")

class IsManager(permissions.BasePermission):
    """Allow only managers of an organization."""
    def has_permission(self, request, view):
        return hasattr(request.user, "membership") and request.user.membership.role == "manager"

class IsSalesperson(permissions.BasePermission):
    """Allow only salespersons of an organization."""
    def has_permission(self, request, view):
        return hasattr(request.user, "membership") and request.user.membership.role == "sales"

class IsOwnerOrManager(permissions.BasePermission):
    """Allow both owners and managers."""
    def has_permission(self, request, view):
        return (
            hasattr(request.user, "organization")
            or (hasattr(request.user, "membership") and request.user.membership.role == "manager")
        )
