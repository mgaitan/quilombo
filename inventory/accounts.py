from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib import messages
from django.db import transaction

from .models import Membership, Workspace


def ensure_home_workspace(user):
    if user.memberships.exists():
        return user.workspaces.first()
    workspace = Workspace.objects.create(name="Home", slug=f"home-{str(user.id)[:8]}")
    Membership.objects.create(workspace=workspace, user=user, role=Membership.Role.OWNER)
    return workspace


class QuilomboSocialAccountAdapter(DefaultSocialAccountAdapter):
    @transaction.atomic
    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        ensure_home_workspace(user)
        return user


class QuilomboAccountAdapter(DefaultAccountAdapter):
    @transaction.atomic
    def save_user(self, request, user, form, commit=True):
        user = super().save_user(request, user, form, commit)
        ensure_home_workspace(user)
        return user

    def respond_email_verification_sent(self, request, user):
        # The verification page already explains that the email was sent. Drop any
        # message carried over from the preceding authentication step.
        list(messages.get_messages(request))
        return super().respond_email_verification_sent(request, user)
