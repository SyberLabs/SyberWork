"""Plan: the files, the steps and the risks of one ticket, written before any code and checked by code.

A plan is the smallest thing a builder and a reviewer can both hold a change to: which existing files change,
which new files appear, a few steps, and the risks its author sees. A planner command writes it: a model behind
a process boundary that reads the ticket and a temporary checkout of the base commit. This module does not
believe it.

- ``check_plan`` refuses a plan that is not the shape below, names an existing file that does not exist or a new
  file that does, or carries a step or a note that holds text a reader would not see, or the shape of a
  credential or of an order to an AI agent (the screen intake puts a request through: a plan is text a model
  wrote after reading a request).
- ``assess`` computes from the plan's paths what the planner was not asked: the class floor of every path
  (``policy.json``'s shape: the highest-ranked rule a path matches), whether a path is a file an agent reads as
  its instructions, which components the paths belong to (a file that does not exist yet belongs to every
  component whose declared paths match it) and what depends on them, and whether a test is among them.
- ``plan_route`` is an ordered rule list, as intake's is, with two routes: ``planned`` and ``needs_human``. A
  plan that touches a path above the ``auto`` class, an instruction file or a sensitive component, that names a
  risk of a kind kept for people, or that is larger than the limit, waits for a person.

The plan's files become the mutable scope of the Build Thread that builds the ticket (``work.start``), so
"follow the plan" is the scope rule the kernel already enforces: a candidate that changes any other path is out
of scope and cannot be accepted.

Nothing here calls a model or reads a file: the caller hands in the tracked paths and the Space graph.
"""

from __future__ import annotations

from syberlabs.errors import Rejected
from syberlabs.factory import features, intake
from syberlabs.factory.space import glob_to_regex

PLAN_SCHEMA = "syberfactory/plan.v1"
PLANNER_PROTOCOL = "syberlabs.space/factory/plan/v1"
ROUTES = ("planned", "needs_human")
RISK_KINDS = ("auth", "secrets", "payments", "data", "release", "external_effect", "dependency", "other")
HELD_RISKS = ("auth", "secrets", "payments", "data", "release", "external_effect")  # a person reads a plan that names one
CLASSES = ("auto", "direction", "keys")  # lowest to highest; MasterMind's charter names (AGENTS.md, "three keys")
MAX_FILES_CEILING = 12                   # featuremap.MAX_FILES_CEILING: a plan a policy may allow at most
MAX_RISKS, NOTE_CHARS = 12, 300
DEFAULT_LIMITS = {"max_files": 6, "max_steps": 12, "step_chars": 240, "max_diff_bytes": 40_000, "listed_files": 2000}
RULES = ("planner.absent", "planner.refused", "plan.instruction_files", "plan.class_floor", "plan.sensitive_area",
         "plan.risk", "plan.too_large", "planned")
# Files an agent reads as instructions (docs/CLAUDE_CODE.md, "Trust"): a change to one changes what the next
# agent is told, so a plan that names one is never routed to an agent by code.
INSTRUCTION_NAMES = frozenset({"claude.md", "claude.local.md", "agents.md", ".mcp.json"})
INSTRUCTION_DIRECTORIES = frozenset({".claude"})
ALWAYS_EXCLUDED = (".git", ".syberlabs")  # the kernel keeps these out of every candidate (syberlabs/evolution.py)
# A new file's name may not hold what Windows refuses in a file name, nor a backtick: the path is shown in a
# Markdown code span, and a checkout of the accepted change has to work on every platform the suite runs on.
NEW_PATH_REFUSED = frozenset('<>:"|?*`')
TEST_MARKERS = ("tests/", "test/", "/test_", "_test.", "spec/")  # examples/critic_rules.py TEST_MARKERS
# The floors used when the operator passes no policy file: what gates a change, and what holds a key. The ``keys``
# paths are the ones this repository's policy.json listed on 2026-11-17. AGENTS.md is not among them (policy.json
# floors it at auto); ``is_instruction_file`` is the rule that holds a plan naming it, whatever the floors say.
DEFAULT_FLOORS = {"floors": [
    {"class": "direction", "why": "what gates a change: workflow definitions and the class policy itself",
     "paths": [".github/**", "policy.json"]},
    {"class": "keys", "why": "credentials, licensing, a production deploy, an agent's configuration",
     "paths": ["**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/secrets/**", ".github/workflows/*deploy*",
               ".github/workflows/*release*", ".github/workflows/*publish*", "LICENSE", "NOTICE", ".mcp.json",
               ".claude/**", "CLAUDE.md"]},
]}
_ALLOWED = frozenset({"files", "new_files", "steps", "risks", "model", "usage", "signal", "provider"})


# -- paths and floors ------------------------------------------------------------------------------------------------

def clean_path(value) -> str | None:
    """A relative POSIX path without traversal that the kernel would let a candidate change, or None."""
    if not isinstance(value, str) or not value or len(value) > 400 or "\\" in value or not value.isprintable():
        return None
    parts = value.split("/")
    if value.startswith("/") or any(part in ("", ".", "..") for part in parts):
        return None
    if parts[0].casefold() in ALWAYS_EXCLUDED:
        return None
    return value


def is_instruction_file(path: str) -> bool:
    parts = path.casefold().split("/")
    return parts[-1] in INSTRUCTION_NAMES or any(part in INSTRUCTION_DIRECTORIES for part in parts)


def is_test(path: str) -> bool:
    return any(marker in "/" + path for marker in TEST_MARKERS)


def check_floors(document: dict) -> dict:
    """A class-floor policy in ``policy.json``'s shape: ``{"floors": [{"class", "paths", ...}]}``. Other top-level
    fields (a comment) are left alone; a rule with an unknown class or without path globs is refused."""
    floors = document.get("floors") if isinstance(document, dict) else None
    if not isinstance(floors, list):
        raise Rejected("plan_floors", "a floor policy is {\"floors\": [{\"class\", \"paths\"}, ...]}")
    for rule in floors:
        if not isinstance(rule, dict) or rule.get("class") not in CLASSES or not isinstance(rule.get("paths"), list) \
                or not rule["paths"] or any(not isinstance(p, str) or not p.strip() for p in rule["paths"]):
            raise Rejected("plan_floors", f"a floor rule names a class from {CLASSES} and a list of path globs")
        if not isinstance(rule.get("requires", []), list):
            raise Rejected("plan_floors", "a floor rule's requires is a list")
    return document


def class_floor(paths: list[str], floors: dict) -> dict:
    """The class each path floors at: the highest-ranked rule it matches, ``auto`` when it matches none. Paths
    and globs are compared with case folded, so ``Claude.md`` is held as ``CLAUDE.md`` is. Returns ``{"class"``
    (the highest over all paths), ``"by_path"`` (every path above ``auto``), ``"requires"`` (what matched rules
    ask for, an evidence grade for example)``}``."""
    rules = [(glob_to_regex(pattern.casefold()), rule) for rule in floors["floors"] for pattern in rule["paths"]]
    by_path, requires, top = {}, set(), 0
    for path in paths:
        rank = 0
        for regex, rule in rules:
            if regex.match(path.casefold()):
                rank = max(rank, CLASSES.index(rule["class"]))
                requires.update(str(item) for item in rule.get("requires", []))
        if rank:
            by_path[path] = CLASSES[rank]
        top = max(top, rank)
    return {"class": CLASSES[top], "by_path": by_path, "requires": sorted(requires)}


def act_class(floor: str, requires: list[str]) -> str:
    """The Build Thread class of a floor, by the aliases AGENTS.md gives ("The charter's three keys"): auto is R,
    auto with an evidence grade is P, direction is C, keys is X. A thread's scope is the plan's paths, so no
    candidate on it can floor above the plan."""
    if floor == "auto":
        return "P" if "evidence" in requires else "R"
    return {"direction": "C", "keys": "X"}[floor]


# -- what a planner reads, and what it may answer -------------------------------------------------------------------------

def check_limits(limits: dict | None) -> dict:
    found = {**DEFAULT_LIMITS, **(limits or {})}
    if set(found) != set(DEFAULT_LIMITS) or any(type(value) is not int or value < 1 for value in found.values()):
        raise Rejected("plan_limits", f"limits are positive integers named {sorted(DEFAULT_LIMITS)}")
    if found["max_files"] > MAX_FILES_CEILING:
        raise Rejected("plan_limits", f"max_files is at most {MAX_FILES_CEILING}; a larger change is a feature map, not one plan")
    return found


def planner_request(ticket: dict, document: dict, *, base: str, tracked: list[str], space: dict | None, limits: dict) -> dict:
    """What a planner reads on stdin. The request's text is the snapshot's visible text, as a decider was sent it.
    ``repository.files`` lists tracked paths at ``base`` (the first ``listed_files`` of them); the planner's
    working directory is a temporary checkout of that commit, where it can read them."""
    components = (space or {}).get("components") or {}
    area = [c for c in ticket["footprint"]["components"] if c in components]
    return {"protocol": PLANNER_PROTOCOL, "role": "planner",
            "ticket": {"ticket_id": ticket["ticket_id"], "work": dict(ticket["work"]), "area": area},
            "request": {"source": document["source"]["kind"],
                        "title": intake.outbound(document["visible"]["title"], 300),
                        "body": intake.outbound(document["visible"]["body"], 20_000)},
            "repository": {"revision": base, "files": tracked[: limits["listed_files"]], "files_total": len(tracked),
                           "area_files": sorted(f for c in area for f in components[c]["files"])},
            "limits": {key: limits[key] for key in ("max_files", "max_steps", "step_chars")},
            "risk_kinds": list(RISK_KINDS),
            "answer": "{files: [existing paths to change], new_files: [paths to add], steps: [short lines], "
                      "risks: [{kind, note}]}"}


def _line(value, limit: int) -> str | None:
    """One printable line of at most ``limit`` characters, stripped; None for anything else."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text and len(text) <= limit and text.isprintable() else None


def _shape(text: str) -> str | None:
    """What a line of plan text may not carry into a builder's prompt or a pull request: ``credential``,
    ``hidden`` (markup or characters a reader of the rendered plan would not see: the person who approves a plan
    reads what the builder reads) or ``directive``, by the screen intake puts a request through."""
    if intake.credential_shapes(text):
        return "credential"
    if intake.visible_text(text) != text:
        return "hidden"
    return "directive" if intake.directive_shapes(text) else None


def check_plan(raw, *, ticket_id: str, request_digest: str, base: str, tracked: list[str], limits: dict) -> dict:
    """A planner's output as a plan, or ``plan_invalid`` with the part that failed; nothing is repaired.

    One strict JSON object with ``files``, ``new_files``, ``steps`` and ``risks`` (and nothing else but an
    adapter's ``model``, ``usage``, ``signal`` or ``provider``). Every path is a clean relative path the kernel
    would let a candidate change; a ``files`` entry is tracked at ``base``, a ``new_files`` entry is not (compared
    with case folded, since a checkout on Windows or macOS would land on the existing file); no path twice; at
    least one path. One to ``max_steps`` steps, each one printable line. Risks name a kind from ``RISK_KINDS``
    with a one-line note. A step or note that holds hidden text, or the shape of a credential or of an order to
    an AI agent, is refused. A new file's name holds none of ``NEW_PATH_REFUSED``."""
    def invalid(part: str):
        return Rejected("plan_invalid", part)

    if not isinstance(raw, dict):
        try:
            raw = intake.strict_loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise invalid("json") from None
    if not isinstance(raw, dict) or set(raw) - _ALLOWED:
        raise invalid("fields")
    known = {path.casefold() for path in tracked}
    exact = set(tracked)
    found: dict[str, list[str]] = {}
    for name in ("files", "new_files"):
        entries = raw.get(name, [])
        if not isinstance(entries, list) or len(entries) > 64:
            raise invalid(name)
        cleaned = []
        for index, entry in enumerate(entries):
            path = clean_path(entry)
            if path is None:
                raise invalid(f"{name}/{index}/path")
            if name == "files" and path not in exact:
                raise invalid(f"{name}/{index}/missing")
            if name == "new_files" and path.casefold() in known:
                raise invalid(f"{name}/{index}/exists")
            if name == "new_files" and NEW_PATH_REFUSED & set(path):
                raise invalid(f"{name}/{index}/path")
            cleaned.append(path)
        found[name] = cleaned
    every = found["files"] + found["new_files"]
    if not every or len({path.casefold() for path in every}) != len(every):
        raise invalid("paths")
    steps = raw.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= limits["max_steps"]:
        raise invalid("steps")
    lines = []
    for index, step in enumerate(steps):
        line = _line(step, limits["step_chars"])
        if line is None:
            raise invalid(f"steps/{index}")
        if _shape(line):
            raise invalid(f"steps/{index}/{_shape(line)}")
        lines.append(line)
    risks = raw.get("risks", [])
    if not isinstance(risks, list) or len(risks) > MAX_RISKS:
        raise invalid("risks")
    named = []
    for index, risk in enumerate(risks):
        note = _line(risk.get("note"), NOTE_CHARS) if isinstance(risk, dict) else None
        if not isinstance(risk, dict) or risk.get("kind") not in RISK_KINDS or note is None:
            raise invalid(f"risks/{index}")
        if _shape(note):
            raise invalid(f"risks/{index}/{_shape(note)}")
        named.append({"kind": risk["kind"], "note": note})
    served = raw.get("model")
    if served is not None and _line(served, 200) is None:
        raise invalid("model")
    return {"schema": PLAN_SCHEMA, "ticket_id": ticket_id, "request_digest": request_digest, "base": base,
            "files": sorted(found["files"]), "new_files": sorted(found["new_files"]), "steps": lines, "risks": named,
            "model": served.strip() if served else None}


# -- what code computes from the paths ---------------------------------------------------------------------------------------

def assess(plan: dict, *, floors: dict, space: dict | None, ticket: dict, sensitive_areas: list[str] | tuple = ()) -> dict:
    """What the plan's paths mean, computed here and never taken from the planner: the class floor, the
    instruction files, the components the paths belong to (``components_of``, new files included), every
    component that depends on those, the components outside the area the ticket named, the sensitive ones, and
    whether a test is among the paths."""
    paths = plan["files"] + plan["new_files"]
    floor = class_floor(paths, floors)
    components = sorted({name for path in paths for name in components_of(space, path)})
    known = (space or {}).get("components") or {}
    dependents = features.impact(space, [c for c in components if c in known])["dependents"] if space and components else []
    area = ticket["footprint"]["components"]
    return {"paths": len(paths), "class_floor": floor["class"], "floor_paths": floor["by_path"], "requires": floor["requires"],
            "instruction_files": sorted(path for path in paths if is_instruction_file(path)),
            "components": components, "dependents": dependents,
            "outside_area": sorted(set(components) - set(area)) if area else [],
            "sensitive_areas": sorted(set(components) & set(sensitive_areas)),
            "tests_planned": any(is_test(path) for path in paths),
            "risk_kinds": sorted({risk["kind"] for risk in plan["risks"]})}


def components_of(space: dict | None, path: str) -> list[str]:
    """The declared components ``path`` belongs to. A file the Space graph indexed has the one
    ``space.map_components`` gave it. Any other path (a file that does not exist yet, or one the graph does not
    index) belongs to every component whose declared path globs match it: more than one when globs overlap,
    which errs toward a person reading the plan."""
    record = ((space or {}).get("files") or {}).get(path)
    if record is not None:
        return [record["component"]] if record.get("component") else []
    return sorted(name for name, component in ((space or {}).get("components") or {}).items()
                  if any(glob_to_regex(pattern).match(path) for pattern in component.get("paths", [])))


def _decided(route: str, rule: str, reason: str) -> dict:
    return {"route": route, "rule": rule, "reason": reason}


def plan_route(planner: dict | None, assessment: dict | None, limits: dict) -> dict:
    """The rules over a planner's record and the assessment of its plan, in order; the first that applies decides.
    ``assessment`` is None when the planner gave no usable plan."""
    if planner is None:
        return _decided("needs_human", "planner.absent", "no_planner")
    if assessment is None:
        return _decided("needs_human", "planner.refused", f"planner:{planner['outcome']}")
    if assessment["instruction_files"]:
        return _decided("needs_human", "plan.instruction_files", "instruction_files")
    if assessment["class_floor"] != "auto":
        return _decided("needs_human", "plan.class_floor", f"class_floor:{assessment['class_floor']}")
    if assessment["sensitive_areas"]:
        return _decided("needs_human", "plan.sensitive_area", f"sensitive_area:{assessment['sensitive_areas'][0]}")
    held = [kind for kind in assessment["risk_kinds"] if kind in HELD_RISKS]
    if held:
        return _decided("needs_human", "plan.risk", f"risk:{held[0]}")
    if assessment["paths"] > limits["max_files"]:
        return _decided("needs_human", "plan.too_large", f"plan_too_large:{assessment['paths']}")
    return _decided("planned", "planned", "planned")


def text(plan: dict, assessment: dict, decided: dict) -> str:
    """The plan as Markdown a person reads before work starts, or posts where the request lives: the route, the
    files, the steps, the named risks and what code computed. The steps and notes are a model's words; the rest
    is not."""
    def listed(items: list[str]) -> list[str]:
        return items or ["- none"]

    floors = ", ".join(f"`{path}` {found}" for path, found in sorted(assessment["floor_paths"].items()))
    lines = [f"### Plan: {decided['route'].replace('_', '-')}", "", f"Rule `{decided['rule']}` decided: `{decided['reason']}`.", "",
             "**Files to change**", *listed([f"- `{path}`" for path in plan["files"]]), "",
             "**Files to add**", *listed([f"- `{path}`" for path in plan["new_files"]]), "",
             "**Steps** (the planner's words)", *[f"{number}. {step}" for number, step in enumerate(plan["steps"], 1)], "",
             "**Risks the planner named**", *listed([f"- {risk['kind']}: {risk['note']}" for risk in plan["risks"]]), "",
             "**Computed from the paths**",
             f"- class floor: {assessment['class_floor']}" + (f" ({floors})" if floors else ""),
             f"- components: {', '.join(assessment['components']) or 'none mapped'}"
             + (f"; depended on by {', '.join(assessment['dependents'])}" if assessment["dependents"] else ""),
             f"- a test is among the paths: {'yes' if assessment['tests_planned'] else 'no'}"]
    if assessment["outside_area"]:
        lines.append(f"- outside the area the ticket named: {', '.join(assessment['outside_area'])}")
    if assessment["instruction_files"]:
        lines.append("- files an agent reads as instructions: " + ", ".join(f"`{path}`" for path in assessment["instruction_files"]))
    return "\n".join(lines) + "\n"
