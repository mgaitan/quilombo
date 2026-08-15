from drf_spectacular.extensions import OpenApiAuthenticationExtension


class ApiTokenAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "inventory.authentication.ApiTokenAuthentication"
    name = "bearerAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "Quilombo workspace token",
        }
