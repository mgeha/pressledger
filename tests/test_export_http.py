"""HTTP integration tests for export routing, authentication and validation."""

import sqlite3
import unittest

from fastapi.testclient import TestClient

from pressledger.db import SCHEMA_SQL, register_functions
from pressledger.web.app import app
from pressledger.web.deps import db
from tests.support import MACHINE, configured
from tests.test_export import JOB_SQL, LOG_SQL, MEDIA_SQL

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(configured(api_token=TOKEN))
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        register_functions(self.conn)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            JOB_SQL, (MACHINE.id, "2026-08-11", 1, "2026-08-11", "105774_Customer.pdf", 10)
        )
        self.conn.execute(MEDIA_SQL, (MACHINE.id, "2026-08-11", 1, 10))
        self.conn.execute(LOG_SQL, (MACHINE.id, "2026-08-11", "CSV", "day.csv"))
        self.conn.execute(
            JOB_SQL, (MACHINE.id, "2026-08-12", 7, "2026-08-12", "106001_Today.pdf", 3)
        )
        self.conn.execute(LOG_SQL, (MACHINE.id, "2026-08-12", "ACL", "today.acl"))
        self.conn.commit()

        app.dependency_overrides[db] = lambda: self.conn
        self.addCleanup(app.dependency_overrides.clear)
        # Skip the lifespan so tests do not start the scheduler or access the printer.
        self.client = TestClient(app)


class WiringTests(ApiTestCase):
    def test_the_three_routes_answer(self):
        for path in ("/api/v1/machines", "/api/v1/days", "/api/v1/runs"):
            with self.subTest(path=path):
                response = self.client.get(path, headers=AUTH)
                self.assertEqual(response.status_code, 200)
                self.assertIn("count", response.json())

    def test_a_run_arrives_whole(self):
        body = self.client.get("/api/v1/runs", headers=AUTH).json()
        self.assertEqual(body["count"], 1)  # the ACL day is filtered out
        run = body["runs"][0]
        self.assertEqual(run["machine_id"], MACHINE.id)
        self.assertEqual(run["clicks"], 10)
        self.assertEqual(run["sheets"], 10)
        self.assertEqual(run["runtime_s"], 600)
        self.assertEqual(run["completed_date"], "2026-08-11")
        self.assertEqual(len(run["media"]), 1)
        self.assertNotIn("job_key", run)

    def test_query_booleans_are_parsed_not_taken_as_strings(self):
        """`final_only=false` left as a string would be truthy and silently keep
        the default."""
        without = self.client.get("/api/v1/runs", headers=AUTH).json()
        with_acl = self.client.get("/api/v1/runs?final_only=false", headers=AUTH).json()
        self.assertEqual(without["count"], 1)
        self.assertEqual(with_acl["count"], 2)
        self.assertFalse(with_acl["filter"]["final_only"])

        no_media = self.client.get("/api/v1/runs?media=0", headers=AUTH).json()
        self.assertNotIn("media", no_media["runs"][0])
        raw = self.client.get("/api/v1/runs?raw=1", headers=AUTH).json()
        self.assertEqual(raw["runs"][0]["raw"]["nofbinds"], "3")

    def test_paging_works_across_the_wire(self):
        first = self.client.get("/api/v1/runs?final_only=false&limit=1", headers=AUTH).json()
        self.assertEqual(first["count"], 1)
        self.assertIsNotNone(first["next"])
        second = self.client.get(
            "/api/v1/runs",
            params={"final_only": "false", "limit": 1, "after": first["next"]},
            headers=AUTH,
        ).json()
        self.assertEqual(second["count"], 1)
        self.assertNotEqual(first["runs"][0]["jobid"], second["runs"][0]["jobid"])
        self.assertIsNone(second["next"])

    def test_the_filter_echo_survives_serialisation(self):
        body = self.client.get("/api/v1/runs?jobtype=ip&date_from=2026-08-01", headers=AUTH).json()
        self.assertEqual(body["filter"]["jobtype"], "IP")
        self.assertEqual(body["filter"]["date_from"], "2026-08-01")
        self.assertIsNone(body["filter"]["machine"])

    def test_machines_lists_the_configured_press(self):
        body = self.client.get("/api/v1/machines", headers=AUTH).json()
        self.assertEqual(
            body["machines"], [{"id": MACHINE.id, "name": MACHINE.name, "configured": True}]
        )

    def test_days_names_the_running_day(self):
        body = self.client.get("/api/v1/days", headers=AUTH).json()
        self.assertEqual(
            [(d["source_date"], d["final"]) for d in body["days"]],
            [("2026-08-11", True), ("2026-08-12", False)],
        )


class ErrorTranslationTests(ApiTestCase):
    def test_a_bad_request_is_a_400_not_a_500(self):
        cases = {
            "/api/v1/runs?machine=v1000-99": "Unknown machine",
            "/api/v1/runs?date_from=2026-13-45": "date_from",
            "/api/v1/runs?date_field=readydate": "date_field",
            "/api/v1/runs?after=kaputt": "cursor",
            "/api/v1/days?machine=v1000-99": "Unknown machine",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                response = self.client.get(path, headers=AUTH)
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected, response.json()["detail"])

    def test_a_limit_below_one_is_refused_by_the_route(self):
        """Query(ge=1) rejects it before export.clamp_limit sees it: 422."""
        self.assertEqual(self.client.get("/api/v1/runs?limit=0", headers=AUTH).status_code, 422)


class GuardOverHttpTests(ApiTestCase):
    def test_no_token_no_data(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
            with self.subTest(headers=headers):
                response = self.client.get("/api/v1/runs", headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

    def test_a_non_ascii_header_is_a_401(self):
        """Header bytes arrive latin-1 decoded, so non-ASCII reaches the guard."""
        headers = {"Authorization": "Bearer tökén".encode("latin-1")}
        response = self.client.get("/api/v1/runs", headers=headers)
        self.assertEqual(response.status_code, 401)

    def test_every_route_is_guarded(self):
        for path in ("/api/v1/machines", "/api/v1/days", "/api/v1/runs"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_an_unconfigured_token_closes_the_interface(self):
        with configured(api_token=""):
            response = self.client.get("/api/v1/runs", headers=AUTH)
        self.assertEqual(response.status_code, 503)

    def test_the_pages_stay_open(self):
        """The guard must not have leaked onto the rest of the app."""
        self.assertEqual(self.client.get("/api/status").status_code, 200)


if __name__ == "__main__":
    unittest.main()
