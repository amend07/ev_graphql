"""Coverage for ev_backend utility modules: the hardened view's error masking,
the query-depth/complexity validators' fragment handling, introspection gating,
the endpoint-wide rate limit, pagination edge cases, and the CSRF-exemption
middleware."""

import json
import os
import subprocess
import sys

from django.core.cache import cache
from django.test import Client as DjangoClient
from django.test import RequestFactory, TestCase, override_settings
from graphql import GraphQLError, get_introspection_query, parse, validate

from ev_backend.csrf_exempt import DisableCSRF
from ev_backend.errors import NotFound, ValidationError
from ev_backend.graphql_validation import (
    complexity_limit_validator,
    depth_limit_validator,
    introspection_validator,
)
from ev_backend.graphql_view import _GENERIC_MESSAGE, HardenedGraphQLView
from ev_backend.pagination import clamp_offset, clamp_page_size
from ev_backend.schema import schema


class FormatErrorTests(TestCase):
    def test_internal_error_types_are_masked(self):
        # A KeyError escaping a resolver must never leak its message to a client.
        err = GraphQLError("secret detail", original_error=KeyError("column x"))
        formatted = HardenedGraphQLView.format_error(err)
        self.assertEqual(formatted["message"], _GENERIC_MESSAGE)
        self.assertNotIn("secret", str(formatted))

    def test_api_error_message_passes_through_with_a_code(self):
        err = GraphQLError("Station not found", original_error=NotFound("Station not found"))
        formatted = HardenedGraphQLView.format_error(err)
        self.assertEqual(formatted["message"], "Station not found")
        self.assertEqual(formatted["extensions"]["code"], "not_found")

    def test_validation_error_carries_its_code(self):
        err = GraphQLError("bad", original_error=ValidationError("bad"))
        formatted = HardenedGraphQLView.format_error(err)
        self.assertEqual(formatted["extensions"]["code"], "validation")

    def test_plain_graphql_error_without_original_is_untouched(self):
        err = GraphQLError("Cannot query field 'nope'.")
        formatted = HardenedGraphQLView.format_error(err)
        self.assertEqual(formatted["message"], "Cannot query field 'nope'.")


class DepthLimitFragmentTests(TestCase):
    def _errors(self, query, max_depth):
        return validate(
            schema.graphql_schema, parse(query), [depth_limit_validator(max_depth=max_depth)]
        )

    def test_named_fragment_spread_counts_toward_depth(self):
        query = """
        query { stationList { ...f } }
        fragment f on StationListType { name }
        """
        # stationList -> (fragment) name = depth 2.
        self.assertTrue(self._errors(query, max_depth=1))
        self.assertFalse(self._errors(query, max_depth=2))

    def test_inline_fragment_does_not_add_a_level(self):
        query = "{ stationById(stationId: \"1\") { ... on StationType { name } } }"
        # The inline fragment is transparent: stationById -> name = depth 2.
        self.assertFalse(self._errors(query, max_depth=2))

    def test_recursive_fragment_spread_is_guarded_against_cycles(self):
        # A fragment that spreads itself must not send the validator into a loop.
        query = """
        query { stationList { ...f } }
        fragment f on StationListType { name ...f }
        """
        # Terminates and simply reports (validation completes without hanging).
        self._errors(query, max_depth=5)


class PaginationEdgeTests(TestCase):
    def test_clamp_offset_falls_back_on_non_numeric(self):
        self.assertEqual(clamp_offset("not-a-number"), 0)
        self.assertEqual(clamp_offset(object()), 0)

    @override_settings(GRAPHQL_DEFAULT_PAGE_SIZE=20, GRAPHQL_MAX_PAGE_SIZE=100)
    def test_clamp_page_size_falls_back_on_non_numeric(self):
        self.assertEqual(clamp_page_size(object()), 20)


class DisableCSRFMiddlewareTests(TestCase):
    def test_graphql_path_is_exempted(self):
        mw = DisableCSRF(lambda r: None)
        request = RequestFactory().post("/graphql/")
        mw.process_request(request)
        self.assertTrue(getattr(request, "_dont_enforce_csrf_checks", False))

    def test_other_paths_are_not_exempted(self):
        mw = DisableCSRF(lambda r: None)
        request = RequestFactory().post("/other/")
        mw.process_request(request)
        self.assertFalse(getattr(request, "_dont_enforce_csrf_checks", False))


class ProductionCacheGuardTests(TestCase):
    """The fail-fast prod check must REFUSE to boot on a per-process cache.

    Rate-limit/lockout counters live in the default cache; on LocMemCache across
    N Gunicorn workers the effective login/OTP limits become ~Nx, defeating
    brute-force protection. The guard runs at settings-import time (skipped under
    the test runner), so we boot a real subprocess with a full production env.
    """

    #: A complete, valid production env EXCEPT the cache — so only the cache
    #: check can fire. Overridden per-case with/without REDIS_URL.
    def _prod_env(self, **overrides):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('DJANGO_', 'JWT_', 'DATABASE_', 'REDIS_',
                                     'EMAIL_', 'SMS_', 'CORS_', 'CSRF_'))}
        env.update({
            'DJANGO_SETTINGS_MODULE': 'ev_backend.settings',
            'DJANGO_DEBUG': 'False',
            'DJANGO_SECRET_KEY': 'a-strong-unique-production-secret-value-123456',
            'DJANGO_ALLOWED_HOSTS': 'example.com',
            'DATABASE_URL': 'postgres://u:p@db:5432/app',
            'EMAIL_BACKEND': 'django.core.mail.backends.console.EmailBackend',
            'SMS_BACKEND': 'disabled',
        })
        env.update(overrides)
        return env

    def _boot(self, env):
        return subprocess.run(
            [sys.executable, '-c', 'import django; django.setup()'],
            env=env, capture_output=True, text=True, cwd=os.getcwd(),
        )

    def test_locmemcache_is_rejected_in_production(self):
        # No REDIS_URL -> LocMemCache -> must refuse to start.
        result = self._boot(self._prod_env())
        self.assertNotEqual(result.returncode, 0,
                            f"settings booted on LocMemCache: {result.stderr}")
        self.assertIn('SHARED cache', result.stderr)
        self.assertIn('REDIS_URL', result.stderr)

    def test_redis_cache_is_accepted_in_production(self):
        # With a shared cache and otherwise-valid config, settings must import.
        result = self._boot(self._prod_env(REDIS_URL='redis://cache:6379/0'))
        self.assertEqual(result.returncode, 0,
                         f"settings refused a valid prod config: {result.stderr}")


class ComplexityLimitTests(TestCase):
    """The breadth/cost rule bounds field and alias counts — the shallow-but-wide
    axis the depth rule can't see."""

    def _errors(self, query, *, max_fields=1000, max_aliases=1000):
        return validate(
            schema.graphql_schema,
            parse(query),
            [complexity_limit_validator(max_fields=max_fields, max_aliases=max_aliases)],
        )

    def test_wide_query_over_the_field_limit_is_rejected(self):
        # A flat, depth-2 selection of many fields — cheap on depth, costly overall.
        query = "{ stationList { id name location latitude longitude } }"
        # stationList + 5 leaves = 6 fields.
        self.assertTrue(self._errors(query, max_fields=5))
        self.assertFalse(self._errors(query, max_fields=6))

    def test_real_app_sized_query_passes_the_default(self):
        # The largest real client query is ~24 fields; the production default
        # (GRAPHQL_MAX_FIELDS=200) must comfortably allow one.
        query = """
        query StationById {
          stationById(stationId: "1") {
            id name location latitude longitude availability chargerType
            numOfCharger powerOutputKw pricePerKwh averageRating reviewCount
            reviews { id rating comment user { id username } }
          }
        }
        """
        with override_settings(GRAPHQL_MAX_FIELDS=200, GRAPHQL_MAX_ALIASES=50):
            self.assertFalse(
                validate(schema.graphql_schema, parse(query), [complexity_limit_validator()])
            )

    def test_fields_inside_fragments_are_counted(self):
        # Hiding fields behind a fragment must not evade the cap.
        query = """
        query { stationList { ...f } }
        fragment f on StationListType { id name location latitude }
        """
        # stationList + 4 fragment leaves = 5 fields.
        self.assertTrue(self._errors(query, max_fields=4))
        self.assertFalse(self._errors(query, max_fields=5))

    def test_recursive_fragment_is_cycle_safe(self):
        # A self-spreading fragment must not loop the counter forever.
        query = """
        query { stationList { ...f } }
        fragment f on StationListType { name ...f }
        """
        self._errors(query, max_fields=5)  # terminates and simply reports

    def test_aliases_are_counted_against_the_alias_limit(self):
        # Re-selecting the same field under many aliases trips the alias cap even
        # when each individual field is cheap.
        query = "{ a: stationList { id } b: stationList { id } c: stationList { id } }"
        self.assertTrue(self._errors(query, max_aliases=2))
        self.assertFalse(self._errors(query, max_aliases=3))

    def test_introspection_is_not_counted(self):
        # The (large) standard introspection query must pass even a tiny field cap
        # — its meta-field subtree is exempt, mirroring the depth rule.
        document = parse(get_introspection_query())
        self.assertFalse(
            validate(schema.graphql_schema, document, [complexity_limit_validator(max_fields=1)])
        )


class IntrospectionGateTests(TestCase):
    """`__schema`/`__type` are refused unless introspection is explicitly allowed
    (DEBUG or GRAPHQL_ALLOW_INTROSPECTION)."""

    SCHEMA_QUERY = "{ __schema { types { name } } }"

    def setUp(self):
        # Isolate from any rate-limit counters/lockout left by other tests, since
        # the endpoint cases below make real /graphql/ posts through the limiter.
        cache.clear()

    def _errors(self, query, allow):
        return validate(
            schema.graphql_schema, parse(query), [introspection_validator(allow=allow)]
        )

    def test_schema_probe_rejected_when_disallowed(self):
        self.assertTrue(self._errors(self.SCHEMA_QUERY, allow=False))

    def test_type_probe_rejected_when_disallowed(self):
        self.assertTrue(self._errors('{ __type(name: "UserType") { name } }', allow=False))

    def test_schema_probe_allowed_when_permitted(self):
        self.assertFalse(self._errors(self.SCHEMA_QUERY, allow=True))

    def test_typename_is_always_allowed(self):
        # __typename leaks nothing and appears in real queries — never refused.
        self.assertFalse(self._errors("{ __typename }", allow=False))

    @override_settings(DEBUG=False, GRAPHQL_ALLOW_INTROSPECTION=False)
    def test_endpoint_rejects_introspection_in_production(self):
        res = DjangoClient().post(
            "/graphql/",
            data=json.dumps({"query": self.SCHEMA_QUERY}),
            content_type="application/json",
        )
        body = res.json()
        self.assertIn("errors", body)
        self.assertIn("Introspection is disabled", str(body["errors"]))

    @override_settings(DEBUG=False, GRAPHQL_ALLOW_INTROSPECTION=True)
    def test_endpoint_allows_introspection_when_setting_is_on(self):
        res = DjangoClient().post(
            "/graphql/",
            data=json.dumps({"query": self.SCHEMA_QUERY}),
            content_type="application/json",
        )
        body = res.json()
        self.assertNotIn("errors", body)
        self.assertIn("__schema", body["data"])


class GraphQLRateLimitTests(TestCase):
    """The endpoint-wide throttle rejects a burst over the configured limit with a
    clean 429 and leaks no internals."""

    QUERY = "{ __typename }"  # cheapest legal request; introspection-exempt

    def setUp(self):
        # Counters live in the shared cache; isolate each test.
        cache.clear()

    def _post(self):
        return DjangoClient().post(
            "/graphql/",
            data=json.dumps({"query": self.QUERY}),
            content_type="application/json",
        )

    @override_settings(
        DEBUG=False,
        GRAPHQL_RATELIMIT={"limit": 3, "window": 60, "lockout": 60},
    )
    def test_requests_over_the_limit_are_rejected(self):
        for _ in range(3):
            self.assertEqual(self._post().status_code, 200)
        blocked = self._post()
        self.assertEqual(blocked.status_code, 429)
        body = blocked.json()
        self.assertEqual(body["errors"][0]["extensions"]["code"], "rate_limited")
        # No internals leaked.
        self.assertNotIn("Traceback", str(body))

    @override_settings(
        DEBUG=False,
        GRAPHQL_RATELIMIT={"limit": 100, "window": 60, "lockout": 60},
    )
    def test_normal_traffic_under_the_limit_is_unaffected(self):
        for _ in range(5):
            self.assertEqual(self._post().status_code, 200)
