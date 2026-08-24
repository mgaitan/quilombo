import hashlib
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .models import (
    AccessEvent,
    ApiToken,
    Membership,
    OAuthAuthorizationGrant,
    OAuthAuthorizationRequest,
    OAuthClient,
    OAuthCredential,
)

ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=30)
AUTHORIZATION_TTL = timedelta(minutes=10)


class StoredAuthorizationCode(AuthorizationCode):
    record_id: uuid.UUID


class StoredRefreshToken(RefreshToken):
    record_id: uuid.UUID


class StoredAccessToken(AccessToken):
    record_id: uuid.UUID


def _oauth_prefix(raw_token):
    parts = raw_token.split("_", 3)
    if len(parts) != 4 or parts[:2] != ["qlo", "oauth"]:
        return None
    return parts[2]


def resolve_inventory_token(raw_token):
    token_parts = raw_token.split("_", 2)
    if len(token_parts) == 3 and token_parts[0] == "qlo":
        try:
            token = ApiToken.objects.select_related("user", "workspace").get(
                prefix=token_parts[1], revoked_at__isnull=True
            )
        except ApiToken.DoesNotExist:
            token = None
        if token and token.matches(raw_token):
            return token

    prefix = _oauth_prefix(raw_token)
    if not prefix:
        return None
    try:
        credential = OAuthCredential.objects.select_related("user", "workspace", "client").get(
            prefix=prefix,
            kind=OAuthCredential.Kind.ACCESS,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
    except OAuthCredential.DoesNotExist:
        return None
    return credential if credential.matches(raw_token) else None


class QuilomboOAuthProvider:
    def __init__(self):
        self.issuer = settings.PUBLIC_BASE_URL.rstrip("/")
        self.resource = f"{self.issuer}/mcp"

    async def get_client(self, client_id):
        return await sync_to_async(self._get_client)(client_id)

    def _get_client(self, client_id):
        try:
            record = OAuthClient.objects.get(client_id=client_id)
        except OAuthClient.DoesNotExist:
            return None
        return OAuthClientInformationFull.model_validate(record.metadata)

    async def register_client(self, client_info):
        await sync_to_async(self._register_client)(client_info)

    def _register_client(self, client_info):
        redirect_uris = client_info.redirect_uris or []
        if not redirect_uris or len(redirect_uris) > 10:
            raise RegistrationError(
                "invalid_redirect_uri", "One to ten redirect URIs are required."
            )
        for redirect_uri in redirect_uris:
            parsed = urlsplit(str(redirect_uri))
            is_loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            is_secure = parsed.scheme == "https" or (parsed.scheme == "http" and is_loopback)
            if parsed.fragment or not is_secure:
                raise RegistrationError(
                    "invalid_redirect_uri",
                    "Redirect URIs must use HTTPS, except HTTP loopback addresses.",
                )
        OAuthClient.objects.update_or_create(
            client_id=client_info.client_id,
            defaults={"metadata": client_info.model_dump(mode="json")},
        )

    async def authorize(self, client, params):
        return await sync_to_async(self._authorize)(client, params)

    def _authorize(self, client, params: AuthorizationParams):
        if params.resource and params.resource.rstrip("/") != self.resource:
            raise AuthorizeError("invalid_target", "The requested resource is not this MCP server.")
        record = OAuthAuthorizationRequest.objects.create(
            client_id=client.client_id,
            state=params.state or "",
            scopes=params.scopes or ["inventory"],
            code_challenge=params.code_challenge,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource or self.resource,
            expires_at=timezone.now() + AUTHORIZATION_TTL,
        )
        consent_path = reverse("oauth-consent")
        return f"{self.issuer}{consent_path}?{urlencode({'request': record.id})}"

    async def load_authorization_code(self, client, authorization_code):
        return await sync_to_async(self._load_authorization_code)(client, authorization_code)

    def _load_authorization_code(self, client, authorization_code):
        parts = authorization_code.split("_", 2)
        if len(parts) != 3 or parts[0] != "qoc":
            return None
        try:
            grant = OAuthAuthorizationGrant.objects.get(
                code_prefix=parts[1],
                client_id=client.client_id,
                used_at__isnull=True,
            )
        except OAuthAuthorizationGrant.DoesNotExist:
            return None
        if not grant.matches(authorization_code):
            return None
        return StoredAuthorizationCode(
            record_id=grant.id,
            code=authorization_code,
            scopes=grant.scopes,
            expires_at=grant.expires_at.timestamp(),
            client_id=grant.client_id,
            code_challenge=grant.code_challenge,
            redirect_uri=grant.redirect_uri,
            redirect_uri_provided_explicitly=grant.redirect_uri_provided_explicitly,
            resource=grant.resource or None,
            subject=str(grant.user_id),
        )

    async def exchange_authorization_code(self, client, authorization_code):
        return await sync_to_async(self._exchange_authorization_code)(client, authorization_code)

    @transaction.atomic
    def _exchange_authorization_code(self, client, authorization_code):
        try:
            grant = (
                OAuthAuthorizationGrant.objects.select_for_update()
                .select_related("client", "user", "workspace")
                .get(
                    id=authorization_code.record_id,
                    client_id=client.client_id,
                    used_at__isnull=True,
                    expires_at__gt=timezone.now(),
                )
            )
        except OAuthAuthorizationGrant.DoesNotExist as error:
            raise TokenError(
                "invalid_grant", "Authorization code is invalid or expired."
            ) from error
        grant.used_at = timezone.now()
        grant.save(update_fields=["used_at"])
        token = self._issue_token_pair(
            client=grant.client,
            user=grant.user,
            workspace=grant.workspace,
            can_write=grant.can_write,
            scopes=grant.scopes,
            resource=grant.resource,
        )
        AccessEvent.objects.create(
            user=grant.user,
            channel=AccessEvent.Channel.MCP,
            client_name=grant.client.metadata.get("client_name") or grant.client_id,
        )
        return token

    async def load_refresh_token(self, client, refresh_token):
        return await sync_to_async(self._load_refresh_token)(client, refresh_token)

    def _load_refresh_token(self, client, refresh_token):
        prefix = _oauth_prefix(refresh_token)
        if not prefix:
            return None
        try:
            credential = OAuthCredential.objects.get(
                prefix=prefix,
                kind=OAuthCredential.Kind.REFRESH,
                client_id=client.client_id,
                revoked_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
        except OAuthCredential.DoesNotExist:
            return None
        if not credential.matches(refresh_token):
            return None
        return StoredRefreshToken(
            record_id=credential.id,
            token=refresh_token,
            client_id=credential.client_id,
            scopes=credential.scopes,
            expires_at=int(credential.expires_at.timestamp()),
            subject=str(credential.user_id),
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        return await sync_to_async(self._exchange_refresh_token)(client, refresh_token, scopes)

    @transaction.atomic
    def _exchange_refresh_token(self, client, refresh_token, scopes):
        try:
            credential = (
                OAuthCredential.objects.select_for_update()
                .select_related("client", "user", "workspace")
                .get(
                    id=refresh_token.record_id,
                    client_id=client.client_id,
                    kind=OAuthCredential.Kind.REFRESH,
                    revoked_at__isnull=True,
                    expires_at__gt=timezone.now(),
                )
            )
        except OAuthCredential.DoesNotExist as error:
            raise TokenError("invalid_grant", "Refresh token is invalid or expired.") from error
        credential.revoked_at = timezone.now()
        credential.save(update_fields=["revoked_at"])
        OAuthCredential.objects.filter(
            family_id=credential.family_id,
            kind=OAuthCredential.Kind.ACCESS,
            revoked_at__isnull=True,
        ).update(revoked_at=timezone.now())
        return self._issue_token_pair(
            client=credential.client,
            user=credential.user,
            workspace=credential.workspace,
            can_write=credential.can_write,
            scopes=scopes,
            resource=credential.resource,
            family_id=credential.family_id,
        )

    async def load_access_token(self, token):
        return await sync_to_async(self._load_access_token)(token)

    def _load_access_token(self, token):
        identity = resolve_inventory_token(token)
        if not identity:
            return None
        if isinstance(identity, ApiToken):
            return AccessToken(
                token=token,
                client_id=f"api-token:{identity.id}",
                scopes=["inventory"],
                resource=self.resource,
                subject=str(identity.user_id),
                claims={
                    "iss": self.issuer,
                    "workspace_id": str(identity.workspace_id),
                    "user_id": str(identity.user_id),
                    "can_write": identity.can_write,
                },
            )
        return StoredAccessToken(
            record_id=identity.id,
            token=token,
            client_id=identity.client_id,
            scopes=identity.scopes,
            expires_at=int(identity.expires_at.timestamp()),
            resource=identity.resource or self.resource,
            subject=str(identity.user_id),
            claims={
                "iss": self.issuer,
                "workspace_id": str(identity.workspace_id),
                "user_id": str(identity.user_id),
                "can_write": identity.can_write,
            },
        )

    async def revoke_token(self, token):
        await sync_to_async(self._revoke_token)(token)

    def _revoke_token(self, token):
        if not getattr(token, "record_id", None):
            return
        try:
            credential = OAuthCredential.objects.get(id=token.record_id)
        except OAuthCredential.DoesNotExist:
            return
        OAuthCredential.objects.filter(
            family_id=credential.family_id,
            revoked_at__isnull=True,
        ).update(revoked_at=timezone.now())

    def _issue_token_pair(
        self, *, client, user, workspace, can_write, scopes, resource, family_id=None
    ):
        family_id = family_id or uuid.uuid4()
        access, raw_access = OAuthCredential.issue(
            kind=OAuthCredential.Kind.ACCESS,
            client=client,
            user=user,
            workspace=workspace,
            can_write=can_write,
            family_id=family_id,
            scopes=scopes,
            resource=resource,
            expires_at=timezone.now() + ACCESS_TOKEN_TTL,
        )
        _, raw_refresh = OAuthCredential.issue(
            kind=OAuthCredential.Kind.REFRESH,
            client=client,
            user=user,
            workspace=workspace,
            can_write=can_write,
            family_id=family_id,
            scopes=scopes,
            resource=resource,
            expires_at=timezone.now() + REFRESH_TOKEN_TTL,
        )
        return OAuthToken(
            access_token=raw_access,
            token_type="Bearer",
            expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
            scope=" ".join(scopes),
            refresh_token=raw_refresh,
        )


def create_authorization_grant(*, authorization_request, user, workspace, can_write=True):
    membership = Membership.objects.filter(user=user, workspace=workspace).first()
    if not membership:
        raise ValueError("Workspace is not available to this user.")
    prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    raw_code = f"qoc_{prefix}_{secret}"
    OAuthAuthorizationGrant.objects.create(
        code_prefix=prefix,
        code_hash=hashlib.sha256(raw_code.encode()).hexdigest(),
        client=authorization_request.client,
        user=user,
        workspace=workspace,
        can_write=can_write and (membership.role == Membership.Role.OWNER or membership.can_write),
        scopes=authorization_request.scopes,
        code_challenge=authorization_request.code_challenge,
        redirect_uri=authorization_request.redirect_uri,
        redirect_uri_provided_explicitly=authorization_request.redirect_uri_provided_explicitly,
        resource=authorization_request.resource,
        expires_at=timezone.now() + AUTHORIZATION_TTL,
    )
    return raw_code
