from django.apps import AppConfig


class StationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'stations'

    def ready(self):
        # Invalidate the public station-list cache on any station/review write,
        # regardless of the code path (GraphQL, admin, shell, signals).
        from django.db.models.signals import post_save, post_delete
        from .models import Station, Review
        from .cache import invalidate_station_list

        def _invalidate(sender, **kwargs):
            invalidate_station_list()

        post_save.connect(_invalidate, sender=Station, dispatch_uid="station_cache_save")
        post_delete.connect(_invalidate, sender=Station, dispatch_uid="station_cache_delete")
        post_save.connect(_invalidate, sender=Review, dispatch_uid="review_cache_save")
        post_delete.connect(_invalidate, sender=Review, dispatch_uid="review_cache_delete")
