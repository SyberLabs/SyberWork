"""FeatureMap: the typed task DAG between a promoted spec and its tickets (``syberfactory/feature-map.v1``).

A spec (``spec.py``, the one spec object, consolidation D2) is the objective. A feature map is the work: a DAG
of features, each with an objective a Build Thread can start from, a scope (the paths it may edit), the
acceptance criteria the builder is shown, the names of the hidden checks the host holds (names only; the
checks themselves never enter a worktree), the features it depends on, and the inputs of its risk score.
Every feature traces to at least one requirement of the spec (a requirement is a section, so coverage is
checkable), and every requirement is covered or listed as ``uncovered`` with a reason.

This module is code, not a model. ``mapcheck`` is the deterministic floor the plan-to-one P3 gate needs:
schema, spec digest, coverage, DAG (acyclic), scope conflicts (an intersection of two scopes must be
declared with an order that agrees with the DAG), act class, size ceilings, hidden-check names, and the
owner-approval digest. ``taskgraph`` gives the four standard measures (critical path, total work, their
ratio, Dilworth width with a maximum antichain) and the conflict graph. ``risk_plan`` standardizes the risk
scores and allocates extra verification as ``v_i = V * softmax(z_i / tau)`` above a hard floor that never
moves: host checks, hidden checks, critic and gate apply to every feature whatever its score.

Relation to the rest of the factory:

- D2: the map is derived from #154's spec object. ``spec_ref`` pins ``spec_id``, ``version`` and
  ``spec_digest``; feature ``req_ids`` are the spec's ``req_id``s. ``degenerate_map`` is the map
  ``ticket_from_spec`` implies: one feature per requirement, no dependencies.
- D3: this is the FeatureMap. ``features.py`` (hashed vectors) is the codemap, an index; it is not imported here.
- D5: ``compile_ticket`` turns one feature into one ticket in ``ticket_from_spec``'s shape: scope is the
  footprint, shown criteria are ``acceptance_criteria``, evidence kinds and hidden-check names are the exit
  criteria, and ``rubric_digest`` is the digest of the hidden-check names. A ticket's ``map_ref`` carries the
  map digest and feature id, so ``why`` can walk ticket -> map -> spec.

Nothing here has an opinion: a map that fails a check is refused with the findings, never repaired.
"""

from __future__ import annotations

import math
import re

from syberlabs.canonical import digest
from syberlabs.errors import Rejected
from syberlabs.factory import PROTOCOL
from syberlabs.factory import spec as specmod

SCHEMA = "syberfactory/feature-map.v1"
ACT_CLASSES = ("R", "C", "P", "X")  # syberlabs/build.py ACT_CLASSES; copied so the factory keeps its declared edges
SIZES = ("S", "M", "L")
DURATION = {"S": 1, "M": 2, "L": 4}  # abstract work units per size tier
ACT_RANK = {"R": 0, "C": 1, "P": 2, "X": 3}
INTEGRATIONS = ("await_merge", "stack")
MIN_FEATURES, MAX_FEATURES = 1, 40
MAX_CRITERIA = 8
MAX_FILES_CEILING, MAX_DIFF_BYTES_CEILING = 12, 40_000
FEATURE_FIELDS = frozenset({"id", "title", "objective", "req_ids", "depends_on", "scope", "overlaps", "act_class",
                            "criteria", "hidden_checks", "size", "risk_inputs"})
MAP_FIELDS = frozenset({"schema", "id", "spec_ref", "base", "author", "features", "uncovered", "integration", "approval"})
_FID = re.compile(r"^F[0-9]{1,3}$")
_CHECK = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")  # a contract check name (build._NAME)
_SECTION = re.compile(r"^##\s+(?P<id>[A-Za-z][A-Za-z0-9_-]*)\s*[:.]\s*(?P<text>.+?)\s*$")

# Risk: weights sum to 1 so r is in [0, 1]. From the theory report, section 4; unvalidated priors.
WEIGHTS = {"scope": 0.15, "act_class": 0.15, "new_deps": 0.10, "needs_lookup": 0.10, "disagreement": 0.20,
           "centrality": 0.20, "uncovered": 0.10}
ACT_RISK = {"R": 0.0, "C": 0.5, "P": 0.75, "X": 1.0}
FLOOR = ("host_checks", "hidden_checks", "critic", "safety_gate")  # every feature, every score; never loosened
LADDER = ("independent_replay_rerun", "extra_seeds_x2", "adversarial_review", "owner_blind_review")
TAU_MIN = 0.05  # keeps every propensity positive (the overlap condition for calibration)


# -- spec side: sectioned Markdown -> #154's spec object (owner decision 11, D2) ----------------------------------

def spec_from_markdown(text: str, spec_id: str, capability: str, footprint_hint: list[str], *,
                       author: dict | None = None, evidence_kinds: list[str] | None = None) -> dict:
    """A sectioned Markdown spec (``## R1: requirement text`` then ``- given ... / when ... / then ...`` lines)
    becomes a draft #154 spec with one requirement per section. Scenario bullets of the form
    ``- given X, when Y, then Z`` become scenarios; other bullets are kept as free text scenarios."""
    requirements, current = [], None
    for line in text.splitlines():
        found = _SECTION.match(line)
        if found:
            current = {"req_id": found.group("id"), "text": found.group("text"), "scenarios": [],
                       "evidence_kinds": list(evidence_kinds or ["tests"])}
            requirements.append(current)
            continue
        stripped = line.strip()
        if current is not None and stripped.startswith("- "):
            current["scenarios"].append(_scenario(stripped[2:]))
    if not requirements:
        raise Rejected("spec_invalid", "no '## <id>: <text>' section found")
    return specmod.new_spec(spec_id, capability, requirements, footprint_hint, author=author)


def _scenario(text: str) -> dict:
    parts = re.split(r",\s*(?=when\b|then\b)", text, flags=re.IGNORECASE)
    if len(parts) == 3 and parts[0].lower().startswith("given"):
        return {"given": parts[0][5:].strip(), "when": parts[1][4:].strip(), "then": parts[2][4:].strip()}
    return {"text": text}


# -- the map -------------------------------------------------------------------------------------------------------

def new_map(map_id: str, spec: dict, features: list[dict], *, base: str | None, author: dict,
            uncovered: list[dict] | None = None, integration: str = "await_merge") -> dict:
    """A map over ``spec`` as it is now. Validation is ``mapcheck``; this only assembles."""
    return {"schema": SCHEMA, "id": map_id,
            "spec_ref": {"spec_id": spec["spec_id"], "version": spec["version"], "spec_digest": specmod.spec_digest(spec)},
            "base": base, "author": dict(author), "features": [dict(f) for f in features],
            "uncovered": list(uncovered or []), "integration": integration, "approval": None}


def feature(fid: str, title: str, objective: str, *, req_ids: list[str], paths: list[str], depends_on: list[str] = (),
            criteria: list[dict] | None = None, hidden_checks: list[str] = (), act_class: str = "R", size: str = "S",
            overlaps: list[dict] = (), exclude: list[str] = (), max_files: int = 6, max_diff_bytes: int = 20_000,
            new_dependencies: int = 0, needs_lookup: bool = False, disagreement: float = 0.0) -> dict:
    return {"id": fid, "title": title, "objective": objective, "req_ids": list(req_ids), "depends_on": list(depends_on),
            "scope": {"paths": list(paths), "exclude": list(exclude), "max_files": max_files, "max_diff_bytes": max_diff_bytes},
            "overlaps": [dict(o) for o in overlaps], "act_class": act_class,
            "criteria": [dict(c) for c in (criteria or [])], "hidden_checks": list(hidden_checks), "size": size,
            "risk_inputs": {"new_dependencies": new_dependencies, "needs_lookup": needs_lookup, "disagreement": disagreement}}


def degenerate_map(spec: dict, *, base: str | None, author: dict) -> dict:
    """The map ``ticket_from_spec`` implies (D5): one feature per requirement, no dependencies, the spec's
    resolved footprint files as every feature's scope, scenarios as the shown criteria. Two requirements over
    one footprint therefore overlap; the degenerate map declares each overlap in requirement order."""
    files = (spec.get("footprint") or {}).get("files") or []
    if not files:
        raise Rejected("footprint_unresolved", "resolve the spec's footprint against a Space graph first")
    features = []
    for number, requirement in enumerate(spec["requirements"], 1):
        fid = f"F{number}"
        criteria = [{"id": f"{fid}.c{i}", "text": _criterion_text(s), "kind": "test"}
                    for i, s in enumerate(requirement["scenarios"], 1)] or [{"id": f"{fid}.c1", "text": requirement["text"], "kind": "judgment"}]
        overlaps = [{"with": f"F{other}", "paths": sorted(files), "order": f"F{min(number, other)}_first"}
                    for other in range(1, len(spec["requirements"]) + 1) if other != number]
        features.append(feature(fid, requirement["text"][:80], requirement["text"], req_ids=[requirement["req_id"]],
                                paths=sorted(files), criteria=criteria, overlaps=overlaps,
                                max_files=min(MAX_FILES_CEILING, max(1, len(files)))))
    return new_map(f"{spec['spec_id']}.degenerate", spec, features, base=base, author=author)


def _criterion_text(scenario: dict) -> str:
    if "text" in scenario:
        return scenario["text"]
    return f"given {scenario.get('given', '')}, when {scenario.get('when', '')}, then {scenario.get('then', '')}"


def map_digest(document: dict) -> str:
    """The map without its approval, so approval is over exactly this digest."""
    return digest({k: v for k, v in document.items() if k != "approval"})


def approve(document: dict, actor: dict) -> dict:
    """The owner's approval (G1): a human actor over the map's digest. Any later edit of the map leaves the
    approval stale, and ``approved`` says so. This records; it does not decide that the map is good."""
    if not isinstance(actor, dict) or actor.get("kind") != "human" or not actor.get("id"):
        raise Rejected("approval_actor", "a map is approved by a human actor {kind: human, id: ...}, never a model")
    document["approval"] = {"digest": map_digest(document), "actor": {"kind": "human", "id": str(actor["id"])}}
    return document["approval"]


def approved(document: dict) -> bool:
    approval = document.get("approval")
    return (isinstance(approval, dict) and approval.get("digest") == map_digest(document)
            and isinstance(approval.get("actor"), dict) and approval["actor"].get("kind") == "human"
            and bool(approval["actor"].get("id")))


# -- mapcheck ------------------------------------------------------------------------------------------------------

def normalize_path(path: str) -> str:
    """A scope entry as compared: backslashes as slashes, leading ``./`` segments and the trailing slash
    removed, case folded (the kernel's scope check runs on case-insensitive filesystems too)."""
    text = path.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/").casefold()


def path_intersects(a: str, b: str) -> bool:
    """Prefix intersection of two scope entries at directory boundaries, after :func:`normalize_path`.
    ``"."`` or empty is the whole tree."""
    ad, bd = normalize_path(a), normalize_path(b)
    if ad in ("", ".") or bd in ("", "."):
        return True
    return ad == bd or bd.startswith(ad + "/") or ad.startswith(bd + "/")


def scope_intersection(f: dict, g: dict) -> list[str]:
    out = set()
    for p in f["scope"]["paths"]:
        for q in g["scope"]["paths"]:
            if path_intersects(p, q):
                out.add(p if len(p.rstrip("/")) >= len(q.rstrip("/")) else q)
    return sorted(out)


def mapcheck(document: dict, spec: dict) -> list[str]:
    """Every finding, as strings. Empty means the map may be compiled. Order: shape, then graph, then coverage,
    then conflicts, then approval. A shape finding stops the deeper checks, which would only cascade."""
    findings: list[str] = []
    note = findings.append
    if not isinstance(document, dict):
        return ["map: not a JSON object"]
    missing, extra = MAP_FIELDS - set(document), set(document) - MAP_FIELDS
    if missing or extra:
        return [f"map: missing {sorted(missing)} unknown {sorted(extra)}"]
    if document["schema"] != SCHEMA:
        note(f"map.schema must be {SCHEMA!r}")
    if not isinstance(document["id"], str) or not document["id"].strip():
        note("map.id must be a non-empty string")
    ref = document["spec_ref"]
    if not (isinstance(ref, dict) and set(ref) == {"spec_id", "version", "spec_digest"}):
        note("map.spec_ref must be {spec_id, version, spec_digest}")
    else:
        if ref["spec_id"] != spec["spec_id"] or ref["version"] != spec["version"]:
            note(f"map.spec_ref names {ref['spec_id']} v{ref['version']}, the spec is {spec['spec_id']} v{spec['version']}")
        if ref["spec_digest"] != specmod.spec_digest(spec):
            note("map.spec_ref.spec_digest does not equal the spec's digest (the spec changed after the map was written)")
    if document["base"] is not None and not (isinstance(document["base"], str) and document["base"]):
        note("map.base must be a commit string or null")
    author = document["author"]
    if not (isinstance(author, dict) and author.get("kind") in ("human", "model", "command") and author.get("id")):
        note("map.author must be {kind: human|model|command, id}")
    if document["integration"] not in INTEGRATIONS:
        note(f"map.integration must be one of {INTEGRATIONS}")
    features = document["features"]
    if not (isinstance(features, list) and MIN_FEATURES <= len(features) <= MAX_FEATURES):
        note(f"features must be a list of {MIN_FEATURES}..{MAX_FEATURES}")
        return findings
    req_ids = [r["req_id"] for r in spec["requirements"]]
    ids: list[str] = []
    check_names: dict[str, str] = {}
    for index, item in enumerate(features):
        where = f"feature[{index}]"
        if not isinstance(item, dict):
            note(f"{where}: not an object")
            continue
        missing, extra = FEATURE_FIELDS - set(item), set(item) - FEATURE_FIELDS
        if missing or extra:
            note(f"{where}: missing {sorted(missing)} unknown {sorted(extra)}")
            continue
        fid = item["id"]
        if not (isinstance(fid, str) and _FID.match(fid)):
            note(f"{where}: bad id {fid!r} (F1..F999)")
            continue
        if fid in ids:
            note(f"{where}: duplicate id {fid}")
        ids.append(fid)
        where = fid
        if not (isinstance(item["title"], str) and item["title"].strip() and isinstance(item["objective"], str) and item["objective"].strip()):
            note(f"{where}: title and objective must be non-empty strings")
        if not (isinstance(item["req_ids"], list) and item["req_ids"]):
            note(f"{where}: req_ids must name at least one spec requirement (every feature traces to a section)")
        else:
            for rid in item["req_ids"]:
                if rid not in req_ids:
                    note(f"{where}: req_ids names unknown requirement {rid!r}")
        if not (isinstance(item["depends_on"], list) and all(isinstance(x, str) for x in item["depends_on"])):
            note(f"{where}: depends_on must be a list of feature ids")
        elif len(set(item["depends_on"])) != len(item["depends_on"]) or fid in item["depends_on"]:
            note(f"{where}: depends_on repeats an id or names itself")
        scope = item["scope"]
        if not (isinstance(scope, dict) and set(scope) == {"paths", "exclude", "max_files", "max_diff_bytes"}):
            note(f"{where}: scope must be exactly {{paths, exclude, max_files, max_diff_bytes}}")
        else:
            paths = scope["paths"]
            if not (isinstance(paths, list) and paths and all(isinstance(p, str) and p.strip() for p in paths)):
                note(f"{where}: scope.paths must be a non-empty list of strings")
            elif any(p.startswith("/") or ".." in p.split("/") or p.strip() in (".", "./") for p in paths):
                note(f"{where}: scope.paths must be repository-relative, without '..', and not the whole tree")
            if not (isinstance(scope["exclude"], list) and all(isinstance(p, str) for p in scope["exclude"])):
                note(f"{where}: scope.exclude must be a list of strings")
            for key, ceiling in (("max_files", MAX_FILES_CEILING), ("max_diff_bytes", MAX_DIFF_BYTES_CEILING)):
                value = scope[key]
                if type(value) is not int or not 1 <= value <= ceiling:
                    note(f"{where}: scope.{key} must be an int 1..{ceiling} (size check)")
        if not isinstance(item["overlaps"], list):
            note(f"{where}: overlaps must be a list")
        else:
            for overlap in item["overlaps"]:
                if not (isinstance(overlap, dict) and set(overlap) == {"with", "paths", "order"} and isinstance(overlap["with"], str)
                        and isinstance(overlap["paths"], list) and isinstance(overlap["order"], str)):
                    note(f"{where}: overlap entries must be exactly {{with, paths, order}}")
                elif overlap["order"] not in (f"{fid}_first", f"{overlap['with']}_first"):
                    note(f"{where}: overlap with {overlap['with']} has order {overlap['order']!r}; it must be '{fid}_first' or '{overlap['with']}_first'")
        if item["act_class"] not in ACT_CLASSES:
            note(f"{where}: act_class must be one of {ACT_CLASSES}")
        criteria = item["criteria"]
        if not (isinstance(criteria, list) and 1 <= len(criteria) <= MAX_CRITERIA):
            note(f"{where}: criteria must be a list of 1..{MAX_CRITERIA} (what the builder is shown)")
        else:
            seen = set()
            for criterion in criteria:
                if not (isinstance(criterion, dict) and set(criterion) == {"id", "text", "kind"}):
                    note(f"{where}: criterion fields must be exactly id, text, kind")
                    continue
                if not (isinstance(criterion["id"], str) and re.match(rf"^{re.escape(fid)}\.c[0-9]+$", criterion["id"])) or criterion["id"] in seen:
                    note(f"{where}: bad or duplicate criterion id {criterion.get('id')!r}")
                seen.add(criterion["id"])
                if not (isinstance(criterion["text"], str) and criterion["text"].strip()):
                    note(f"{where}: criterion {criterion['id']} has empty text")
                if criterion["kind"] not in ("test", "judgment"):
                    note(f"{where}: criterion {criterion['id']} kind must be test or judgment")
        hidden = item["hidden_checks"]
        if not (isinstance(hidden, list) and all(isinstance(h, str) and _CHECK.match(h) for h in hidden)):
            note(f"{where}: hidden_checks must be a list of check names (names only; the checks stay with the host)")
        else:
            for name in hidden:
                if name in check_names and check_names[name] != fid:
                    note(f"{where}: hidden check {name!r} is also named by {check_names[name]}")
                check_names.setdefault(name, fid)
        if item["size"] not in SIZES:
            note(f"{where}: size must be S, M or L")
        risk = item["risk_inputs"]
        if not (isinstance(risk, dict) and set(risk) == {"new_dependencies", "needs_lookup", "disagreement"}
                and type(risk["new_dependencies"]) is int and risk["new_dependencies"] >= 0
                and isinstance(risk["needs_lookup"], bool)
                and isinstance(risk["disagreement"], (int, float)) and not isinstance(risk["disagreement"], bool)
                and math.isfinite(risk["disagreement"]) and 0 <= risk["disagreement"] <= 1):
            note(f"{where}: risk_inputs must be {{new_dependencies: int >= 0, needs_lookup: bool, disagreement: 0..1}}")
    if findings:
        return findings
    by_id = {f["id"]: f for f in features}
    for item in features:
        for dep in item["depends_on"]:
            if dep not in by_id:
                note(f"{item['id']}: depends_on names unknown feature {dep!r}")
        for overlap in item["overlaps"]:
            if overlap["with"] not in by_id or overlap["with"] == item["id"]:
                note(f"{item['id']}: overlap names unknown feature {overlap['with']!r}")
    if findings:
        return findings
    deps = {f["id"]: list(f["depends_on"]) for f in features}
    cycle = find_cycle(deps)
    if cycle:
        note("depends_on has a cycle: " + " -> ".join(cycle))
    # coverage: every requirement is traced by a feature or listed as uncovered with a reason, never both
    uncovered = document["uncovered"]
    listed = set()
    if not isinstance(uncovered, list):
        note("uncovered must be a list")
    else:
        for entry in uncovered:
            if not (isinstance(entry, dict) and set(entry) == {"req_id", "reason"} and entry["req_id"] in req_ids
                    and isinstance(entry["reason"], str) and entry["reason"].strip()):
                note(f"uncovered entry {entry!r} must be {{req_id: <spec requirement>, reason: <non-empty>}}")
            else:
                listed.add(entry["req_id"])
    covered = {rid: sorted(f["id"] for f in features if rid in f["req_ids"]) for rid in req_ids}
    for rid in req_ids:
        if not covered[rid] and rid not in listed:
            note(f"requirement {rid} is covered by no feature and is not listed as uncovered")
        if covered[rid] and rid in listed:
            note(f"requirement {rid} is both covered ({covered[rid]}) and listed as uncovered")
    # conflicts: an intersection must be declared, with one order that does not contradict the DAG
    ancestors_ = ancestors(deps) if not cycle else {}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            intersection = scope_intersection(by_id[a], by_id[b])
            declared = ([o for o in by_id[a]["overlaps"] if o["with"] == b] + [o for o in by_id[b]["overlaps"] if o["with"] == a])
            if intersection and not declared:
                note(f"scope conflict: {a} and {b} both claim {intersection} and declare no overlap")
            if declared and not intersection:
                note(f"{a} and {b} declare an overlap but their scopes do not intersect")
            orders = {o["order"] for o in declared}
            if len(orders) > 1:
                note(f"{a} and {b} declare contradictory overlap orders {sorted(orders)}")
            elif orders and ancestors_:
                first = next(iter(orders))[: -len("_first")]
                second = b if first == a else a
                if second in ancestors_.get(first, set()):
                    note(f"overlap order {first}_first contradicts depends_on ({first} depends on {second})")
    approval = document["approval"]
    if approval is not None and not approved(document):
        note("approval is present but stale or not by a human: it does not cover this map's digest")
    return findings


def find_cycle(deps: dict[str, list[str]]) -> list[str] | None:
    white, grey, black = 0, 1, 2
    colour = {k: white for k in deps}
    stack: list[str] = []

    def visit(node):
        colour[node] = grey
        stack.append(node)
        for other in deps.get(node, []):
            if other not in colour:
                continue
            if colour[other] == grey:
                return stack[stack.index(other):] + [other]
            if colour[other] == white:
                found = visit(other)
                if found:
                    return found
        stack.pop()
        colour[node] = black
        return None

    for node in deps:
        if colour[node] == white:
            found = visit(node)
            if found:
                return found
    return None


def ancestors(deps: dict[str, list[str]]) -> dict[str, set[str]]:
    """Transitive dependencies (what each feature waits for)."""
    memo: dict[str, set[str]] = {}

    def go(node):
        if node not in memo:
            memo[node] = set()
            for dep in deps.get(node, []):
                memo[node] |= {dep} | go(dep)
        return memo[node]

    for node in deps:
        go(node)
    return memo


# -- task graph -----------------------------------------------------------------------------------------------------

def taskgraph(document: dict) -> dict:
    """Critical path (forward and backward pass over size durations), total work and the work/critical-path
    ratio (the most concurrency that can pay), Dilworth width with one maximum antichain (bipartite matching
    and Koenig's construction), topological levels, slack, and the conflict graph of scope intersections."""
    features = document["features"]
    ids = [f["id"] for f in features]
    order = {fid: i for i, fid in enumerate(ids)}
    deps = {f["id"]: list(f["depends_on"]) for f in features}
    if find_cycle(deps):
        raise Rejected("map_cyclic", "the task graph has a cycle; run mapcheck first")
    duration = {f["id"]: DURATION[f["size"]] for f in features}
    anc = ancestors(deps)
    children = {fid: [g for g in ids if fid in deps[g]] for fid in ids}
    topo = _topological(ids, deps, children, order)
    est, eft = {}, {}
    for node in topo:
        est[node] = max((eft[d] for d in deps[node]), default=0)
        eft[node] = est[node] + duration[node]
    length = max(eft.values()) if eft else 0
    bottom = {}
    for node in reversed(topo):
        bottom[node] = duration[node] + max((bottom[c] for c in children[node]), default=0)
    slack = {node: length - (est[node] + bottom[node]) for node in ids}
    path, current = [], None
    heads = [n for n in ids if not deps[n] and slack[n] == 0]
    current = min(heads, key=order.get) if heads else None
    while current:
        path.append(current)
        nxt = [c for c in children[current] if slack[c] == 0 and est[c] == eft[current]]
        current = min(nxt, key=order.get) if nxt else None
    level = {}
    for node in topo:
        level[node] = 0 if not deps[node] else 1 + max(level[d] for d in deps[node])
    antichain = _max_antichain(ids, anc)
    conflicts = []
    by_id = {f["id"]: f for f in features}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            intersection = scope_intersection(by_id[a], by_id[b])
            if intersection:
                conflicts.append({"a": a, "b": b, "paths": intersection, "ordered_by_dag": a in anc[b] or b in anc[a]})
    total = sum(duration.values())
    return {"features": len(ids), "edges": sum(len(v) for v in deps.values()), "durations": duration,
            "topological_order": topo, "topo_level": level, "earliest_start": est, "bottom_level": bottom, "slack": slack,
            "critical_path": path, "critical_path_length": length, "total_work": total,
            "work_critical_ratio": round(total / length, 3) if length else 0.0,
            "width": len(antichain), "max_antichain": antichain, "conflicts": conflicts,
            "descendants": {fid: sum(1 for g in ids if fid in anc[g]) for fid in ids}}


def _topological(ids, deps, children, order) -> list[str]:
    indegree = {n: len(deps[n]) for n in ids}
    ready = sorted([n for n in ids if indegree[n] == 0], key=order.get)
    out = []
    while ready:
        node = ready.pop(0)
        out.append(node)
        for child in children[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort(key=order.get)
    return out


def _max_antichain(ids: list[str], anc: dict[str, set[str]]) -> list[str]:
    """Dilworth: width = n - |maximum matching| over the strict order a < b; Koenig recovers an antichain."""
    adjacent = {a: [b for b in ids if a in anc[b]] for a in ids}
    match_right: dict[str, str] = {}

    def augment(u, seen):
        for v in adjacent[u]:
            if v in seen:
                continue
            seen.add(v)
            if v not in match_right or augment(match_right[v], seen):
                match_right[v] = u
                return True
        return False

    for u in ids:
        augment(u, set())
    match_left = {u: v for v, u in match_right.items()}
    left_reach, right_reach = set(u for u in ids if u not in match_left), set()
    stack = list(left_reach)
    while stack:
        u = stack.pop()
        for v in adjacent[u]:
            if v not in right_reach:
                right_reach.add(v)
                w = match_right.get(v)
                if w is not None and w not in left_reach:
                    left_reach.add(w)
                    stack.append(w)
    cover_left, cover_right = set(ids) - left_reach, right_reach
    antichain = [x for x in ids if x not in cover_left and x not in cover_right]
    if len(antichain) != len(ids) - len(match_right):
        raise Rejected("taskgraph_internal", "Koenig construction mismatch")
    return antichain


# -- risk and the verification plan ---------------------------------------------------------------------------------

def risk_inputs(item: dict, graph: dict) -> dict:
    """The seven standardized inputs in [0, 1], all computable by code from the map and the graph."""
    scope = item["scope"]
    n = graph["features"]
    criteria = item["criteria"]
    mechanical = sum(1 for c in criteria if c["kind"] == "test")
    return {"scope": round(0.5 * scope["max_files"] / MAX_FILES_CEILING + 0.5 * scope["max_diff_bytes"] / MAX_DIFF_BYTES_CEILING, 4),
            "act_class": ACT_RISK[item["act_class"]],
            "new_deps": min(1.0, item["risk_inputs"]["new_dependencies"] / 2),
            "needs_lookup": 1.0 if item["risk_inputs"]["needs_lookup"] else 0.0,
            "disagreement": float(item["risk_inputs"]["disagreement"]),
            "centrality": round(graph["descendants"][item["id"]] / max(1, n - 1), 4),
            "uncovered": round(1 - mechanical / len(criteria), 4) if criteria else 1.0}


def risk_score(inputs: dict, weights: dict = WEIGHTS) -> float:
    return round(sum(weights[k] * inputs[k] for k in weights), 4)


def softmax(values: list[float], tau: float) -> list[float]:
    top = max(values)
    exps = [math.exp((v - top) / tau) for v in values]
    total = sum(exps)
    return [e / total for e in exps]


def integer_units(shares: list[float], budget: int) -> list[int]:
    """Largest-remainder rounding of ``budget * shares`` to integers that sum to ``budget``."""
    raw = [budget * s for s in shares]
    base = [math.floor(x) for x in raw]
    remainder = budget - sum(base)
    order = sorted(range(len(shares)), key=lambda i: (-(raw[i] - base[i]), i))
    for i in order[:remainder]:
        base[i] += 1
    return base


def ladder(units: int) -> list[str]:
    rungs = list(LADDER[: min(units, len(LADDER))])
    if units > len(LADDER):
        rungs.append(f"extra_seeds_x{2 * (units - len(LADDER))}")
    return rungs


def risk_plan(document: dict, graph: dict | None = None, *, budget: int | None = None, tau: float = 1.0,
              weights: dict = WEIGHTS) -> dict:
    """Standardized risk scores and the softmax allocation of ``budget`` extra verification units above the floor.

    ``z_i = (r_i - mean) / sd`` so tau is in standard deviations of this map's spread (a map whose raw scores
    spread over 0.14 would otherwise be allocated uniformly at any stated tau); with one feature or zero
    spread the allocation is uniform. ``share`` is the propensity each unit had of landing on a feature and is
    recorded with the plan, so later calibration can reweight. The floor is listed beside every allocation and is
    the same for every feature: it is what a low score can never buy its way out of."""
    graph = graph or taskgraph(document)
    if tau < TAU_MIN:
        raise Rejected("risk_tau", f"tau must be at least {TAU_MIN} so every feature keeps a positive propensity")
    if not isinstance(weights, dict) or set(weights) != set(WEIGHTS):
        raise Rejected("risk_weights", f"weights must name exactly the inputs {sorted(WEIGHTS)}")
    if any(isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or not 0 <= w <= 1 for w in weights.values()):
        raise Rejected("risk_weights", "each weight is a finite number in [0, 1]")
    if abs(sum(weights.values()) - 1.0) > 1e-6:
        raise Rejected("risk_weights", f"weights sum to {sum(weights.values()):.4f}, not 1, so r would leave [0, 1]")
    features = document["features"]
    ids = [f["id"] for f in features]
    budget = len(ids) if budget is None else budget
    if type(budget) is not int or budget < 0:
        raise Rejected("risk_budget", "the verification budget is a non-negative integer number of units")
    inputs = {f["id"]: risk_inputs(f, graph) for f in features}
    scores = [risk_score(inputs[fid], weights) for fid in ids]
    mean = sum(scores) / len(scores)
    spread = math.sqrt(sum((s - mean) ** 2 for s in scores) / len(scores))
    z = [((s - mean) / spread) if spread > 0 else 0.0 for s in scores]
    shares = softmax(z, tau)
    units = integer_units(shares, budget)
    return {"schema": "syberfactory/verification-plan.v1", "map_digest": map_digest(document), "weights": dict(weights),
            "tau": tau, "budget": budget, "standardized": spread > 0,
            "inputs": inputs, "r": dict(zip(ids, scores)), "z": {fid: round(v, 4) for fid, v in zip(ids, z)},
            "share": {fid: round(s, 4) for fid, s in zip(ids, shares)}, "units": dict(zip(ids, units)),
            "floor": list(FLOOR), "buys": {fid: list(FLOOR) + ladder(u) for fid, u in zip(ids, units)}}


# -- compile: one feature -> one ticket (D5) -------------------------------------------------------------------------

def compile_ticket(document: dict, item: dict, spec: dict, *, human_owner: str, baseline_run: str | None = None,
                   space: dict | None = None) -> dict:
    """The work order of one feature in ``ticket_from_spec``'s shape. ``human_owner`` is mandatory: agents are
    delegated to, a person stays accountable. ``rubric_digest`` covers the hidden-check names the host holds;
    the checks' content is never in the ticket, which a builder may read. With a Space graph the footprint's
    components are the owners of the scope's files; without one no component is claimed."""
    if not isinstance(human_owner, str) or not human_owner.strip():
        raise Rejected("ticket_owner", "a ticket names the accountable human; none given")
    requirements = {r["req_id"]: r for r in spec["requirements"]}
    kinds: list[str] = []
    for rid in item["req_ids"]:
        for kind in requirements[rid]["evidence_kinds"]:
            if kind not in kinds:
                kinds.append(kind)
    components = sorted({record["component"] for path, record in (space or {}).get("files", {}).items()
                         if record.get("component") and any(path_intersects(p, path) for p in item["scope"]["paths"])})
    sha = map_digest(document)
    return {
        "protocol": PROTOCOL, "kind": "ticket",
        "ticket_id": digest({"map": sha, "feature": item["id"]})[:16],
        "title": item["title"][:120],
        "spec_ref": {"spec_id": spec["spec_id"], "version": spec["version"], "req_ids": list(item["req_ids"])},
        "map_ref": {"map_id": document["id"], "map_digest": sha, "feature_id": item["id"]},
        "objective": item["objective"],
        "acceptance_criteria": [dict(c) for c in item["criteria"]],
        "footprint": {"components": components, "files": sorted(item["scope"]["paths"]),
                      "revision": document["base"], "space_digest": None},
        "scope": {k: (list(v) if isinstance(v, list) else v) for k, v in item["scope"].items()},
        "depends_on": list(item["depends_on"]), "act_class": item["act_class"], "size": item["size"],
        "repro": {"baseline_run": baseline_run},
        "exit_criteria": [f"evidence:{kind}" for kind in kinds] + [f"hidden:{name}" for name in item["hidden_checks"]],
        "rubric_digest": digest(sorted(item["hidden_checks"])) if item["hidden_checks"] else None,
        "delegate": None, "human_owner": human_owner.strip(),
        "state": "open", "candidates": [], "verdicts": [], "evidence_links": [],
    }


def compile_tickets(document: dict, spec: dict, *, human_owner: str, baseline_run: str | None = None,
                    space: dict | None = None) -> list[dict]:
    """Every feature, in map order. Refuses a map that fails ``mapcheck``."""
    findings = mapcheck(document, spec)
    if findings:
        raise Rejected("map_invalid", "; ".join(findings))
    return [compile_ticket(document, item, spec, human_owner=human_owner, baseline_run=baseline_run, space=space)
            for item in document["features"]]
