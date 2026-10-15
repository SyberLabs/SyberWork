"""Authored integration regressions against published, pinned dependencies.

Reference fixes validate the fixtures; they are never sent to the model.
These are not sampled upstream issues or a representative workload.
"""

from __future__ import annotations

import textwrap


def source(text: str) -> str:
    return textwrap.dedent(text).lstrip()


FIXTURES = {
    "httpx-asgi": {
        "package": "httpx", "version": "0.28.1",
        "source_url": "https://github.com/encode/httpx/releases/tag/0.28.0",
        "objective": (
            "Migrate src/integration.py to HTTPX 0.28.1. fetch_json must call the supplied ASGI app "
            "in process, never use a network server, pass headers and query strings, raise "
            "HTTPStatusError for error responses, return decoded JSON, and close its client. "
            "Keep its async signature and do not change dependency pins or checks."
        ),
        "broken": source('''
            import httpx


            async def fetch_json(app, path, headers=None):
                async with httpx.AsyncClient(app=app, base_url="http://test", headers=headers) as client:
                    response = await client.get(path)
                    response.raise_for_status()
                    return response.json()
        '''),
        "reference": source('''
            import httpx


            async def fetch_json(app, path, headers=None):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
                    response = await client.get(path)
                    response.raise_for_status()
                    return response.json()
        '''),
        "checks": source('''
            import asyncio
            import json
            import unittest
            import httpx
            from integration import fetch_json


            async def app(scope, receive, send):
                headers = dict(scope["headers"])
                status = 404 if scope["path"] == "/missing" else 200
                payload = {"path": scope["path"], "query": scope["query_string"].decode(),
                           "client": headers.get(b"x-client", b"").decode()}
                await send({"type": "http.response.start", "status": status,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": json.dumps(payload).encode()})


            class Behavior(unittest.TestCase):
                def test_in_process_request(self):
                    self.assertEqual(asyncio.run(fetch_json(app, "/health")),
                                     {"path": "/health", "query": "", "client": ""})

                def test_headers_query_and_repeated_calls(self):
                    for value in ("first", "second"):
                        self.assertEqual(asyncio.run(fetch_json(app, "/echo?q=two%20words", {"X-Client": value})),
                                         {"path": "/echo", "query": "q=two%20words", "client": value})

                def test_http_error_is_not_silenced(self):
                    with self.assertRaises(httpx.HTTPStatusError) as caught:
                        asyncio.run(fetch_json(app, "/missing"))
                    self.assertEqual(caught.exception.response.status_code, 404)


            if __name__ == "__main__":
                unittest.main()
        '''),
        "reads": [{"path": "httpx/_transports/asgi.py", "start_line": 63, "end_line": 180},
                  {"path": "httpx/_client.py", "start_line": 1307, "end_line": 1475}],
    },
    "pydantic-attributes": {
        "package": "pydantic", "version": "2.10.6",
        "source_url": "https://docs.pydantic.dev/2.10/migration/",
        "objective": (
            "Migrate src/integration.py to Pydantic 2.10.6. serialize_user must accept both attribute "
            "objects and mappings, accept display_name or its displayName alias, coerce valid string "
            "IDs to integers, reject missing fields, invalid IDs and extra mapping fields, and output "
            "exactly id and displayName. Use the supported v2 validation/serialization APIs; keep "
            "the function signature and dependency pins."
        ),
        "broken": source('''
            from pydantic import BaseModel, Field


            class UserView(BaseModel):
                id: int
                display_name: str = Field(alias="displayName")

                class Config:
                    orm_mode = True
                    allow_population_by_field_name = True
                    extra = "forbid"


            def serialize_user(value):
                return UserView.from_orm(value).dict(by_alias=True)
        '''),
        "reference": source('''
            from pydantic import BaseModel, ConfigDict, Field


            class UserView(BaseModel):
                model_config = ConfigDict(from_attributes=True, populate_by_name=True, extra="forbid")
                id: int
                display_name: str = Field(alias="displayName")


            def serialize_user(value):
                return UserView.model_validate(value).model_dump(by_alias=True)
        '''),
        "checks": source('''
            import unittest
            from types import SimpleNamespace
            from pydantic import ValidationError
            from integration import serialize_user


            class Behavior(unittest.TestCase):
                def test_attribute_object(self):
                    self.assertEqual(serialize_user(SimpleNamespace(id="7", display_name="Ada")),
                                     {"id": 7, "displayName": "Ada"})

                def test_mapping_alias_and_field_name(self):
                    for key in ("displayName", "display_name"):
                        self.assertEqual(serialize_user({"id": "9", key: "Lin"}), {"id": 9, "displayName": "Lin"})

                def test_rejects_invalid_and_extra_fields(self):
                    for value in ({"id": 1}, {"id": "bad", "displayName": "A"},
                                  {"id": 1, "displayName": "A", "admin": True}):
                        with self.subTest(value=value), self.assertRaises(ValidationError):
                            serialize_user(value)


            if __name__ == "__main__":
                unittest.main()
        '''),
        "reads": [{"path": "pydantic/config.py", "start_line": 320, "end_line": 400},
                  {"path": "pydantic/main.py", "start_line": 620, "end_line": 710}],
    },
    "sqlalchemy-execute": {
        "package": "SQLAlchemy", "version": "2.0.36",
        "source_url": "https://docs.sqlalchemy.org/en/20/changelog/migration_20.html",
        "objective": (
            "Migrate src/integration.py to SQLAlchemy 2.0.36. upsert_and_list must create its SQLite "
            "table, insert/update rows using bound parameters in one transaction, return plain dicts "
            "ordered by id, preserve prior rows across calls, accept an empty batch, and roll back "
            "the entire batch if a row violates NOT NULL. Keep the engine argument and function "
            "signature; use supported SQLAlchemy execution/result APIs and keep dependency pins."
        ),
        "broken": source('''
            def upsert_and_list(engine, records):
                with engine.begin() as connection:
                    connection.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
                    for record in records:
                        connection.execute("INSERT INTO items(id, name) VALUES (:id, :name) "
                                           "ON CONFLICT(id) DO UPDATE SET name=excluded.name", record)
                    rows = connection.execute("SELECT id, name FROM items ORDER BY id").fetchall()
                    return [dict(row) for row in rows]
        '''),
        "reference": source('''
            from sqlalchemy import text


            def upsert_and_list(engine, records):
                with engine.begin() as connection:
                    connection.execute(text("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"))
                    for record in records:
                        connection.execute(text("INSERT INTO items(id, name) VALUES (:id, :name) "
                                                "ON CONFLICT(id) DO UPDATE SET name=excluded.name"), record)
                    rows = connection.execute(text("SELECT id, name FROM items ORDER BY id")).mappings().all()
                    return [dict(row) for row in rows]
        '''),
        "checks": source('''
            import unittest
            from sqlalchemy import create_engine
            from sqlalchemy.exc import IntegrityError
            from integration import upsert_and_list


            class Behavior(unittest.TestCase):
                def setUp(self):
                    self.engine = create_engine("sqlite://")
                    self.addCleanup(self.engine.dispose)

                def test_insert_update_and_order(self):
                    rows = upsert_and_list(self.engine, [{"id": 2, "name": "B"}, {"id": 1, "name": "A"}])
                    self.assertEqual(rows, [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])
                    self.assertTrue(all(type(row) is dict for row in rows))
                    self.assertEqual(upsert_and_list(self.engine, [{"id": 2, "name": "new"}]),
                                     [{"id": 1, "name": "A"}, {"id": 2, "name": "new"}])

                def test_empty_and_bound_parameters(self):
                    self.assertEqual(upsert_and_list(self.engine, []), [])
                    dangerous = "Robert'); DROP TABLE items;--"
                    self.assertEqual(upsert_and_list(self.engine, [{"id": 3, "name": dangerous}]),
                                     [{"id": 3, "name": dangerous}])

                def test_batch_rolls_back(self):
                    upsert_and_list(self.engine, [{"id": 1, "name": "original"}])
                    with self.assertRaises(IntegrityError):
                        upsert_and_list(self.engine, [{"id": 1, "name": "changed"}, {"id": 2, "name": None}])
                    self.assertEqual(upsert_and_list(self.engine, []), [{"id": 1, "name": "original"}])


            if __name__ == "__main__":
                unittest.main()
        '''),
        "reads": [{"path": "lib/sqlalchemy/engine/base.py", "start_line": 1380, "end_line": 1440},
                  {"path": "lib/sqlalchemy/engine/result.py", "start_line": 1170, "end_line": 1210}],
    },
}

# Preserve the original default set and its published evidence identities.
LEGACY_FIXTURES = tuple(FIXTURES)
from benchmarks.githits.hard_fixtures import HARD_FIXTURES  # noqa: E402
FIXTURES.update(HARD_FIXTURES)
