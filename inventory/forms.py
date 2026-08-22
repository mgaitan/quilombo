from django import forms
from django.utils.translation import gettext_lazy as _


class WorkspaceCreateForm(forms.Form):
    name = forms.CharField(label=_("Name"), max_length=120)


class WorkspaceRenameForm(forms.Form):
    name = forms.CharField(label=_("Name"), max_length=120)


class WorkspaceShareForm(forms.Form):
    username = forms.CharField(label=_("Username"), max_length=150)
    can_write = forms.BooleanField(label=_("Can edit"), required=False, initial=True)


class MemberAccessForm(forms.Form):
    can_write = forms.BooleanField(label=_("Can edit"), required=False)
