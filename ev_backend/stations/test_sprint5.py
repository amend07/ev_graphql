"""Scalability/reliability tests (Sprint 5): pagination, caching, health,
query-depth limiting."""

from django.test import TestCase, RequestFactory, override_settings
from django.test import Client as DjangoClient
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from graphene.test import Client
from graphql import parse, validate

from ev_backend.schema import schema
from ev_backend.graphql_validation import depth_limit_validator
from ev_backend.pagination import clamp_page_size, clamp_offset
from stations.models import Station

User = get_user_model()


def make_user(username, role="station_owner"):
    return User.objects.create_user(username=username, email=f"{username}@x.com",
                                    password="123456", role=role)


def make_station(owner, name="S", location="L"):
    return Station.objects.create(
        owner=owner, name=name, location=location, latitude=1.0, longitude=1.0,
        availability="24/7", charger_type="CCS", num_of_charger=1,
        power_output_kw=22.0, price_per_kwh=5,
    )


def anon_ctx():
    r = RequestFactory().post("/graphql/")
    r.user = AnonymousUser()
    return r


class PaginationHelpers(TestCase):
    @override_settings(GRAPHQL_DEFAULT_PAGE_SIZE=20, GRAPHQL_MAX_PAGE_SIZE=100)
    def test_clamp_page_size(self):
        self.assertEqual(clamp_page_size(None), 20)
        self.assertEqual(clamp_page_size(5), 5)
        self.assertEqual(clamp_page_size(9999), 100)   # clamped to max
        self.assertEqual(clamp_page_size(0), 1)         # min 1
        self.assertEqual(clamp_page_size("bad"), 20)    # falls back to default

    def test_clamp_offset(self):
        self.assertEqual(clamp_offset(None), 0)
        self.assertEqual(clamp_offset(-5), 0)
        self.assertEqual(clamp_offset(7), 7)


class StationPagination(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client(schema)
        self.owner = make_user("owner")
        for i in range(3):
            make_station(self.owner, name=f"S{i}", location=f"L{i}")

    @override_settings(GRAPHQL_MAX_PAGE_SIZE=2)
    def test_stations_page_totalcount_and_clamp(self):
        q = ("query($l:Int){ stationsPage(limit:$l){ items { name } totalCount hasNext } }")
        res = self.client.execute(q, variables={"l": 100}, context=anon_ctx())
        page = res["data"]["stationsPage"]
        self.assertEqual(page["totalCount"], 3)
        self.assertEqual(len(page["items"]), 2)   # clamped to max page size
        self.assertTrue(page["hasNext"])

    def test_station_list_limit_offset(self):
        q = "query($l:Int,$o:Int){ stationList(limit:$l, offset:$o){ name } }"
        res = self.client.execute(q, variables={"l": 1, "o": 1}, context=anon_ctx())
        self.assertEqual(len(res["data"]["stationList"]), 1)

    def test_station_reviews_paginated(self):
        q = ("query($id:ID!){ stationReviews(stationId:$id, limit:5){ items { rating } "
             "totalCount hasNext } }")
        station = Station.objects.first()
        res = self.client.execute(q, variables={"id": str(station.id)}, context=anon_ctx())
        self.assertEqual(res["data"]["stationReviews"]["totalCount"], 0)


class StationListCaching(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client(schema)
        self.owner = make_user("owner")

    def _count(self):
        res = self.client.execute("{ stationList { name } }", context=anon_ctx())
        return len(res["data"]["stationList"])

    def test_cache_invalidates_on_station_create(self):
        make_station(self.owner, name="A", location="L1")
        first = self._count()
        self.assertEqual(first, 1)
        make_station(self.owner, name="B", location="L2")  # signal bumps cache version
        self.assertEqual(self._count(), first + 1)


class HealthEndpoints(TestCase):
    def test_health(self):
        res = DjangoClient().get("/health/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "ok")

    def test_liveness(self):
        res = DjangoClient().get("/live/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "ok")

    def test_readiness_db_and_cache_ok(self):
        res = DjangoClient().get("/ready/")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["database"], "ok")
        self.assertEqual(body["cache"], "ok")

    def test_version(self):
        res = DjangoClient().get("/version/")
        self.assertIn("version", res.json())


class QueryDepthLimit(TestCase):
    def test_deep_query_rejected_and_shallow_allowed(self):
        query = "{ stationList { name } }"  # operation -> stationList -> name = depth 2
        document = parse(query)
        gql_schema = schema.graphql_schema
        too_strict = validate(gql_schema, document, [depth_limit_validator(max_depth=1)])
        self.assertTrue(too_strict)  # exceeds depth 1
        ok = validate(gql_schema, document, [depth_limit_validator(max_depth=5)])
        self.assertFalse(ok)         # within depth 5

    def test_introspection_query_is_exempt_from_the_depth_limit(self):
        # The standard introspection query is depth 13 — the exact one GraphiQL
        # and graphql-codegen send. Before the exemption it 400'd against the
        # default max of 12, breaking schema fetch. Introspection is a fixed,
        # bounded shape, not the nested-data abuse the guard targets, so even a
        # very tight limit must not reject it.
        from graphql import get_introspection_query

        document = parse(get_introspection_query())
        errors = validate(
            schema.graphql_schema, document, [depth_limit_validator(max_depth=1)]
        )
        self.assertFalse(errors, f"introspection was blocked: {errors}")

    def test_data_fields_beneath_introspection_still_count(self):
        # The exemption is for the introspection meta-field itself, not a licence
        # to hide a deep data traversal beside a __typename sibling.
        query = "{ __typename stationList { reviews { user { id } } } }"
        document = parse(query)  # stationList->reviews->user->id = depth 4
        self.assertTrue(
            validate(schema.graphql_schema, document, [depth_limit_validator(max_depth=3)])
        )
        self.assertFalse(
            validate(schema.graphql_schema, document, [depth_limit_validator(max_depth=4)])
        )
