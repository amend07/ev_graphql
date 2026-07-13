from django.contrib import admin
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from graphene_file_upload.django import FileUploadGraphQLView
from graphene_django.views import GraphQLView
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    # GraphiQL (interactive explorer) is enabled only in DEBUG; production
    # serves the endpoint without the browsable UI.
    path("graphql/", FileUploadGraphQLView.as_view(graphiql=settings.DEBUG)),
]

# Serve media files during development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)