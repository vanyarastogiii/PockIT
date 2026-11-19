from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from accounts.views import RecipientLookupView

admin.site.site_header = "PockIt admin"
admin.site.site_title = "PockIt admin"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("accounts.urls")),
    path("api/users/lookup", RecipientLookupView.as_view(), name="user-lookup"),
    path("api/", include("wallets.urls")),
    path("api/", include("merchants.urls")),
    path("shop/", include("demoshop.urls")),
    # API docs: /api/docs (interactive) and /api/schema (OpenAPI file)
    path("api/schema", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]
# Everything else (/, /dashboard.html, /css/..., /js/...) is the front end,
# served by WhiteNoise from the ../frontend folder.
