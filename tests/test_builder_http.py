"""Builder HTTP routes and the development SSE tail."""

import hashlib
import json
import threading
import unittest
import urllib.error
import urllib.request

from syberwork.server import make_server
from tests.builder_fixtures import POLICY, CellCase, architecture, descriptor

class HttpCases(CellCase):
    def setUp(self):
        super().setUp()
        users = {
            name: {"hash": hashlib.sha256(name.encode()).hexdigest(), "roles": roles, "sources": []}
            for name, roles in {
                "operator": ["operator"],
                "engineer": ["engineer"],
                "intended": ["intended_user"],
                "manager": ["manager"],
                "observer": ["observer"],
            }.items()
        }
        self.server = make_server(self.work, users, port=0)
        self.server.RequestHandlerClass.log_message = lambda *args: None
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def call(self, who, path, data=None, headers=None):
        head = {"Authorization": "Bearer " + who}
        if data is not None:
            head["Content-Type"] = "application/json"
        if headers:
            head.update(headers)
        request = urllib.request.Request(
            self.base + path,
            data=None if data is None else json.dumps(data).encode(),
            headers=head,
            method="GET" if data is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                content = response.headers.get("Content-Type", "")
                if content.startswith("text/event-stream"):
                    return response.status, raw.decode()
                return response.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_routes_stay_compatible_and_the_stream_replays(self):
        status, missing = self.call("operator", "/api/builder/work/" + self.case)
        self.assertEqual(status, 200)
        self.assertEqual(missing["objective"], None)
        status, created = self.call("operator", "/api/cases", {"contract_id": "repo-change", "version": 1, "inputs": {"objective": "b"}})
        self.assertEqual(status, 200)
        self.assertIn("id", created)
        status, listed = self.call("operator", "/api/cases")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(listed), 1)
        denied = urllib.request.Request(self.base + "/api/builder/work/" + self.case)
        with self.assertRaises(urllib.error.HTTPError) as blocked:
            urllib.request.urlopen(denied, timeout=5)
        self.assertEqual(blocked.exception.code, 401)
        for route in (
            "policies", "work/x", "generations", "generations/g/approaches", "generations/g/seal",
            "generations/g/launch", "generations/g/close", "generations/g/candidates", "generations/g/selection",
            "approaches/a/revisions", "agents", "agents/a/commands", "agents/a/activity", "architecture",
            "prototypes", "prototypes/p/state", "worlds", "nope",
        ):
            status, body = self.call("observer", "/api/builder/" + route, {})
            self.assertEqual((status, body["error"]), (409, "forbidden"), route)
        self.assertEqual(self.call("operator", "/api/builder/policies", POLICY)[0], 200)
        status, missing_bind = self.call("operator", "/api/builder/generations/g/bindings", {"event_hash": "ab" * 32})
        self.assertEqual((status, missing_bind["error"]), (409, "not_found"))
        self.assertEqual(self.call("operator", f"/api/builder/work/{self.case}", {"objective": "Ship a reviewable export"})[0], 200)
        status, generation = self.call("operator", "/api/builder/generations", {
            "case_id": self.case,
            "objective": "One approach is enough for the route test",
            "base_revision": "abc123",
            "mode": "explore",
            "isolation": "aware",
            "diversity_threshold": 0.3,
            "min_approaches": 1,
            "selection_policy_id": "review",
            "selection_policy_version": 1,
        })
        self.assertEqual(status, 200)
        status, stream = self.call("operator", f"/api/builder/work/{self.case}/stream?once=1")
        self.assertEqual(status, 200)
        self.assertIn(": heartbeat\n", stream)
        first = _sse(stream)
        self.assertEqual(first[0][1]["kind"], "generation_created")
        self.assertEqual(self.call("operator", f"/api/builder/generations/{generation['id']}/approaches", {
            "descriptor": descriptor("spa_local"),
        })[0], 200)
        status, replay = self.call(
            "operator",
            f"/api/builder/work/{self.case}/stream?once=1",
            headers={"Last-Event-ID": str(first[-1][0])},
        )
        self.assertEqual(status, 200)
        second = _sse(replay)
        self.assertEqual([item[1]["kind"] for item in second], ["approach_registered"])
        self.assertGreater(second[0][0], first[-1][0])
        self.assertEqual(self.call("operator", f"/api/builder/generations/{generation['id']}/seal", {})[0], 200)
        status, agent = self.call("operator", "/api/builder/agents", {
            "generation_id": generation["id"],
            "approach_id": self.store.generation_view(generation["id"])["approaches"][0]["id"],
            "principal": "agent-a",
            "role": "implementer",
            "hypothesis_summary": "private to engineers",
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.call("operator", f"/api/builder/generations/{generation['id']}/launch", {})[0], 200)
        self.assertEqual(self.call("operator", f"/api/builder/agents/{agent['id']}/activity", {"activity": "inspect"})[0], 200)
        self.assertEqual(self.call("intended", "/api/builder/feedback", {
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "generation",
            "target_id": generation["id"],
            "kind": "preference",
            "text": "keep it visible",
        })[1]["authorities"], ["advisory"])
        engineer = _sse(self.call("engineer", f"/api/builder/work/{self.case}/stream?once=1")[1])
        intended = _sse(self.call("intended", f"/api/builder/work/{self.case}/stream?once=1")[1])
        self.assertIn("agent_activity_recorded", [item[1]["kind"] for item in engineer])
        self.assertNotIn("agent_activity_recorded", [item[1]["kind"] for item in intended])
        self.assertIn("feedback_recorded", [item[1]["kind"] for item in intended])
        self.assertEqual([item[0] for item in engineer], sorted(item[0] for item in engineer))
        status, world = self.call("intended", f"/api/builder/work/{self.case}")
        self.assertEqual(status, 200)
        self.assertFalse(world["authoritative"])
        self.assertEqual(world["lens"], ["intended_user"])
        status, candidates = self.call("engineer", f"/api/builder/generations/{generation['id']}/candidates")
        self.assertEqual(status, 200)
        self.assertEqual(candidates["kind"], "CandidateProjection")
        self.assertEqual(self.call("engineer", f"/api/builder/agents/{agent['id']}")[1]["kind"], "ActorProjection")
        self.assertNotIn("hypothesis_summary", self.call("intended", f"/api/builder/agents/{agent['id']}")[1]["agent"])
        snapshot = self.call("operator", "/api/builder/architecture", architecture(self.case, generation["id"]))[1]
        self.assertEqual(self.call("engineer", f"/api/builder/architecture/{snapshot['id']}")[1]["snapshot"]["id"], snapshot["id"])
        self.assertEqual(self.call("engineer", "/api/builder/integrity", {
            "case_id": self.case,
            "generation_id": generation["id"],
            "target_kind": "generation",
            "target_id": generation["id"],
            "claim": "read the diff",
            "source": "review",
            "verifier": "engineer",
            "independence": "human_reviewed",
            "result": "concern",
        })[0], 200)
        sign = self.call("engineer", f"/api/builder/integrity/{generation['id']}?case_id={self.case}&generation_id={generation['id']}&target_kind=generation")[1]
        self.assertEqual(sign["kind"], "IntegrityProjection")
        self.assertEqual(sign["unverified_claims"][0]["independence_claim"], "human_reviewed")
        self.assertEqual(sign["independent_evidence"], [])
        self.assertIsNone(sign["unverified_claims"][0]["verified_independence"])
        self.assertEqual(self._authority(), self.authority)


def _sse(text: str):
    found = []
    for block in text.split("\n\n"):
        if not block.startswith("id:"):
            continue
        seq = None
        data = None
        for line in block.split("\n"):
            if line.startswith("id:"):
                seq = int(line.split(":", 1)[1].strip())
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
        found.append((seq, data))
    return found


