from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .oauth import resolve_inventory_token


class ApiTokenAuthentication(BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        authorization = request.headers.get("Authorization", "")
        if not authorization:
            return None
        parts = authorization.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != self.keyword.lower():
            return None

        raw_token = parts[1]
        token = resolve_inventory_token(raw_token)
        if not token:
            raise AuthenticationFailed("Invalid API token.")
        return token.user, token

    def authenticate_header(self, request):
        return self.keyword
