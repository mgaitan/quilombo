from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import ApiToken


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
        token_parts = raw_token.split("_", 2)
        if len(token_parts) != 3 or token_parts[0] != "qlo":
            raise AuthenticationFailed("Invalid API token.")

        try:
            token = ApiToken.objects.select_related("user", "workspace").get(
                prefix=token_parts[1], revoked_at__isnull=True
            )
        except ApiToken.DoesNotExist as error:
            raise AuthenticationFailed("Invalid API token.") from error
        if not token.matches(raw_token):
            raise AuthenticationFailed("Invalid API token.")
        return token.user, token

    def authenticate_header(self, request):
        return self.keyword
