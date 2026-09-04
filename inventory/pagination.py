from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class InventoryPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200

    def metadata(self):
        return {
            "count": self.page.paginator.count,
            "page": self.page.number,
            "page_size": self.get_page_size(self.request),
            "total_pages": self.page.paginator.num_pages,
            "next": self.get_next_link(),
            "previous": self.get_previous_link(),
        }

    def get_paginated_response(self, data):
        return Response({"pagination": self.metadata(), "results": data})


class PublicSearchPagination(InventoryPagination):
    page_size = 20
    max_page_size = 50

    def get_paginated_response_schema(self, schema):
        return {
            "type": "object",
            "required": ["pagination", "results"],
            "properties": {
                "pagination": {
                    "type": "object",
                    "required": [
                        "count",
                        "page",
                        "page_size",
                        "total_pages",
                        "next",
                        "previous",
                    ],
                    "properties": {
                        "count": {"type": "integer", "example": 123},
                        "page": {"type": "integer", "example": 2},
                        "page_size": {"type": "integer", "example": 50},
                        "total_pages": {"type": "integer", "example": 3},
                        "next": {"type": "string", "nullable": True, "format": "uri"},
                        "previous": {"type": "string", "nullable": True, "format": "uri"},
                    },
                },
                "results": schema,
            },
        }
