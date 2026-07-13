"""Adds security headers Django has no built-in setting for (Sprint 6, Part 2).

Currently sets ``Permissions-Policy``. HSTS, nosniff, referrer-policy and
X-Frame-Options are handled by Django's SecurityMiddleware via settings.
"""

from django.conf import settings


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.permissions_policy = getattr(settings, "PERMISSIONS_POLICY", "")

    def __call__(self, request):
        response = self.get_response(request)
        if self.permissions_policy:
            response.setdefault("Permissions-Policy", self.permissions_policy)
        return response
