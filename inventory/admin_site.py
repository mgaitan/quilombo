from functools import update_wrapper

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect


class QuilomboAdminSite(AdminSite):
    """Use the public Quilombo login flow for admin authentication."""

    @method_decorator(never_cache)
    @login_not_required
    def login(self, request, extra_context=None):
        next_url = request.GET.get("next") or request.POST.get("next") or reverse("admin:index")
        return redirect_to_login(next_url, reverse("login"))

    def admin_view(self, view, cacheable=False):
        def inner(request, *args, **kwargs):
            if not self.has_permission(request):
                if request.path == reverse("admin:logout", current_app=self.name):
                    return HttpResponseRedirect(reverse("admin:index", current_app=self.name))
                return redirect_to_login(request.get_full_path(), reverse("login"))
            return view(request, *args, **kwargs)

        if not cacheable:
            inner = never_cache(inner)
        if not getattr(view, "csrf_exempt", False):
            inner = csrf_protect(inner)
        return update_wrapper(inner, view)
