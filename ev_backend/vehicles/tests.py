"""My Vehicles: garage CRUD, ownership scoping, and the single-primary rule."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase
from graphene.test import Client

from ev_backend.schema import schema
from vehicles.models import Vehicle

User = get_user_model()

MY_VEHICLES = """
query($limit:Int,$offset:Int){
  myVehiclesPage(limit:$limit, offset:$offset){
    totalCount hasNext
    items { id make model year batteryCapacityKwh chargerType plateNumber isPrimary }
  }
}
"""
ADD = """
mutation($input:VehicleInput!){
  addVehicle(input:$input){ vehicle { id make chargerType isPrimary } }
}
"""
UPDATE = """
mutation($id:ID!,$input:VehicleInput!){
  updateVehicle(vehicleId:$id, input:$input){ vehicle { id model isPrimary } }
}
"""
SET_PRIMARY = """
mutation($id:ID!){ setPrimaryVehicle(vehicleId:$id){ vehicle { id isPrimary } } }
"""
DELETE = "mutation($id:ID!){ deleteVehicle(vehicleId:$id){ ok } }"


def make_user(username, is_active=True):
    return User.objects.create_user(
        username=username, email=f"{username}@x.com", password="123456",
        is_active=is_active,
    )


def valid_input(**over):
    data = dict(
        make="Nissan", model="Leaf", year=2021, batteryCapacityKwh=40.0,
        chargerType="chademo", plateNumber="AA-1", isPrimary=False,
    )
    data.update(over)
    return data


class VehicleBase(TestCase):
    def setUp(self):
        self.client = Client(schema)
        self.alice = make_user("alice")
        self.bob = make_user("bob")

    def ctx(self, user):
        r = RequestFactory().post("/graphql/")
        r.user = user
        return r

    def add(self, user, **over):
        res = self.client.execute(
            ADD, variables={"input": valid_input(**over)}, context=self.ctx(user)
        )
        self.assertIsNone(res.get("errors"), res.get("errors"))
        return res["data"]["addVehicle"]["vehicle"]


class VehicleCrudTests(VehicleBase):
    def test_add_normalizes_charger_type_and_first_is_primary(self):
        v = self.add(self.alice, chargerType="Type 2", plateNumber="AA-1")
        self.assertEqual(v["chargerType"], "type2")   # normalized
        self.assertTrue(v["isPrimary"])               # first car is primary

    def test_page_returns_only_my_vehicles(self):
        self.add(self.alice, plateNumber="AA-1")
        self.add(self.bob, plateNumber="BB-1")
        res = self.client.execute(MY_VEHICLES, context=self.ctx(self.alice))
        page = res["data"]["myVehiclesPage"]
        self.assertEqual(page["totalCount"], 1)
        self.assertEqual(page["items"][0]["plateNumber"], "AA-1")

    def test_duplicate_plate_for_same_owner_is_rejected(self):
        self.add(self.alice, plateNumber="AA-1")
        res = self.client.execute(
            ADD, variables={"input": valid_input(plateNumber="AA-1")},
            context=self.ctx(self.alice),
        )
        self.assertIsNotNone(res.get("errors"))
        self.assertEqual(Vehicle.objects.filter(owner=self.alice).count(), 1)

    def test_same_plate_different_owners_is_allowed(self):
        self.add(self.alice, plateNumber="AA-1")
        self.add(self.bob, plateNumber="AA-1")
        self.assertEqual(Vehicle.objects.count(), 2)

    def test_invalid_charger_type_is_rejected(self):
        res = self.client.execute(
            ADD, variables={"input": valid_input(chargerType="banana")},
            context=self.ctx(self.alice),
        )
        self.assertIsNotNone(res.get("errors"))

    def test_invalid_year_is_rejected(self):
        res = self.client.execute(
            ADD, variables={"input": valid_input(year=202)},
            context=self.ctx(self.alice),
        )
        self.assertIsNotNone(res.get("errors"))

    def test_delete_only_own_vehicle(self):
        v = self.add(self.alice, plateNumber="AA-1")
        res = self.client.execute(
            DELETE, variables={"id": v["id"]}, context=self.ctx(self.bob)
        )
        self.assertIsNotNone(res.get("errors"))  # not bob's
        self.assertTrue(Vehicle.objects.filter(id=v["id"]).exists())
        ok = self.client.execute(
            DELETE, variables={"id": v["id"]}, context=self.ctx(self.alice)
        )
        self.assertTrue(ok["data"]["deleteVehicle"]["ok"])
        self.assertFalse(Vehicle.objects.filter(id=v["id"]).exists())

    def test_requires_authentication(self):
        res = self.client.execute(MY_VEHICLES, context=self.ctx(AnonymousUser()))
        self.assertIsNotNone(res.get("errors"))


class VehiclePrimaryTests(VehicleBase):
    def test_set_primary_moves_the_flag(self):
        first = self.add(self.alice, plateNumber="AA-1")   # primary (first)
        second = self.add(self.alice, plateNumber="AA-2")  # not primary
        res = self.client.execute(
            SET_PRIMARY, variables={"id": second["id"]}, context=self.ctx(self.alice)
        )
        self.assertIsNone(res.get("errors"), res.get("errors"))
        self.assertTrue(res["data"]["setPrimaryVehicle"]["vehicle"]["isPrimary"])
        self.assertFalse(Vehicle.objects.get(id=first["id"]).is_primary)
        self.assertEqual(
            Vehicle.objects.filter(owner=self.alice, is_primary=True).count(), 1)

    def test_adding_a_primary_demotes_the_previous_one(self):
        first = self.add(self.alice, plateNumber="AA-1")   # primary (first)
        self.add(self.alice, plateNumber="AA-2", isPrimary=True)
        self.assertFalse(Vehicle.objects.get(id=first["id"]).is_primary)
        self.assertEqual(
            Vehicle.objects.filter(owner=self.alice, is_primary=True).count(), 1)

    def test_update_can_promote_to_primary(self):
        first = self.add(self.alice, plateNumber="AA-1")
        second = self.add(self.alice, plateNumber="AA-2")
        res = self.client.execute(
            UPDATE,
            variables={"id": second["id"], "input": valid_input(
                plateNumber="AA-2", isPrimary=True)},
            context=self.ctx(self.alice),
        )
        self.assertIsNone(res.get("errors"), res.get("errors"))
        self.assertTrue(res["data"]["updateVehicle"]["vehicle"]["isPrimary"])
        self.assertFalse(Vehicle.objects.get(id=first["id"]).is_primary)


class VehicleValidationBranchTests(VehicleBase):
    def _add_err(self, **over):
        res = self.client.execute(
            ADD, variables={"input": valid_input(**over)}, context=self.ctx(self.alice))
        self.assertIsNotNone(res.get("errors"))

    def test_required_fields_rejected_when_blank(self):
        self._add_err(make="   ")
        self._add_err(model="")
        self._add_err(plateNumber="   ")

    def test_battery_capacity_bounds(self):
        self._add_err(batteryCapacityKwh=0.0)     # must be > 0
        self._add_err(batteryCapacityKwh=9999.0)  # exceeds max

    def test_update_nonexistent_vehicle(self):
        res = self.client.execute(
            UPDATE, variables={"id": "999999", "input": valid_input(plateNumber="ZZ-9")},
            context=self.ctx(self.alice))
        self.assertIsNotNone(res.get("errors"))

    def test_update_invalid_input_rejected(self):
        v = self.add(self.alice, plateNumber="AA-1")
        res = self.client.execute(
            UPDATE, variables={"id": v["id"], "input": valid_input(year=5)},
            context=self.ctx(self.alice))
        self.assertIsNotNone(res.get("errors"))

    def test_update_to_duplicate_plate_is_rejected(self):
        self.add(self.alice, plateNumber="AA-1")
        second = self.add(self.alice, plateNumber="AA-2")
        res = self.client.execute(
            UPDATE, variables={"id": second["id"], "input": valid_input(plateNumber="AA-1")},
            context=self.ctx(self.alice))
        self.assertIsNotNone(res.get("errors"))

    def test_set_primary_nonexistent_vehicle(self):
        res = self.client.execute(
            SET_PRIMARY, variables={"id": "999999"}, context=self.ctx(self.alice))
        self.assertIsNotNone(res.get("errors"))
