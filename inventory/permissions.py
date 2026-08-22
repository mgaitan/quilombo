from rest_framework.exceptions import PermissionDenied

from .models import Membership


def membership_can_write(membership):
    return membership.role == Membership.Role.OWNER or membership.can_write


def request_can_write_workspace(request, workspace):
    identity = request.auth
    if getattr(identity, "workspace_id", None):
        return identity.workspace_id == workspace.id and identity.can_write
    membership = Membership.objects.filter(workspace=workspace, user=request.user).first()
    return bool(membership and membership_can_write(membership))


def require_workspace_write(request, workspace):
    if not request_can_write_workspace(request, workspace):
        raise PermissionDenied("This workspace is shared as read-only.")


def user_can_manage_workspace(user, workspace):
    return Membership.objects.filter(
        workspace=workspace,
        user=user,
        role__in=[Membership.Role.OWNER, Membership.Role.ADMIN],
    ).exists()
