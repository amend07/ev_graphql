"""Coverage for ev_backend utility modules: the hardened view's error masking,
the query-depth validator's fragment handling, pagination edge cases, and the
CSRF-exemption middleware."""

from django.test import RequestFactory, TestCase, override_settings
from graphql import GraphQLError, parse, validate

from ev_backend.csrf_exempt import DisableCSRF
from ev_backend.errors import NotFound, ValidationError
from ev_backend.graphql_validation import depth_limit_validator
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
