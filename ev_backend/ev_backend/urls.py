from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.conf.urls.static import static

from .graphql_view import HardenedGraphQLView
from . import health

urlpatterns = [
    path('admin/', admin.site.urls),
    # GraphiQL (interactive explorer) is enabled only in DEBUG; production
    # serves the endpoint without the browsable UI. Depth-limit validation and
    # safe error masking are applied by HardenedGraphQLView.
    path("graphql/", HardenedGraphQLView.as_view(graphiql=settings.DEBUG)),

    # Operational endpoints (Part 6/7).
    path("health/", health.health),
    path("live/", health.liveness),
    path("ready/", health.readiness),
    path("version/", health.version),
]

# Serve media files during development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
