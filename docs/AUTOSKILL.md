# AutoSkill

AutoSkill chooses at most one certified skill for an agent turn. Jev ranks the catalog. Ordinary code decides whether that ranking is allowed to reach the agent. A selection names a document. It does not authorize a tool, a file write, or a network call.

The catalog this design is built against is the skill tree in Everything Claude Code (ECC), the harness performance system published by the Anthropic x Forum Ventures hackathon winner. The screenshot under design shows that tree: a `skills/` directory of workflow documents (`tdd-workflow`, `verification-loop`, `security-review`), language and framework quartets (`django-patterns`, `django-security`, `django-tdd`, `django-verification`, and the same shape for Laravel, Spring Boot, and Quarkus), and cross-cutting procedures (`search-first`, `skill-stocktake`, `eval-harness`). ECC's own READMEs have quoted 56, 156, and 286 skills as the tree grew. AutoSkill pins one commit and one manifest. It does not follow a moving "latest".

Jev, from TypeSafe AI, is the decision API. One request sends a state and a map of typed questions and gets back typed answers. A `choice` question picks one of up to 255 labeled options and returns a probability for every option. A `noul` question returns a yes-probability from 0 to 1. Questions in one request are independent and share the state. The official call is `POST https://api.typesafe.ai/v1/systemone` with `Authorization: Bearer $TYPESAFE_API_KEY`. The published skill-suggestion cookbook used model `jev-1.12`. This design pins `jev-1.13.0` and treats every cookbook percentage as a result about a different roster, model, and harness.

SyberLabs already keeps model output on the far side of a process boundary: `CommandProvider` carries no SDK and makes no model call. AutoSkill follows that split. The selector is a pure function of a catalog, a turn, and a batch of Jev answers. The HTTP adapter is a separate command. This repository's note that bounded decisions are migrating from Jev to Kev stays true: Kev can replace the adapter only when it returns the same answer shape. Until then the adapter is Jev.

## What fails if the tree is pasted into the prompt

ECC activates skills by showing the agent an index and letting the model decide. On a roster the size of the screenshot, that fails in ways the TypeSafe skill-suggestion cookbook already measured on a different 182-skill catalog (Nous Research Hermes, 488 labeled turns, agent `claude-haiku-4-5`):

| | Wrong skill loaded | Skill loaded when none fits |
| --- | --- | --- |
| Agent with only its index | 16.8% | 9.8% |
| Agent plus one Jev suggestion line | 7.3% | 4.0% |
| Agent told the right skill (ceiling) | 2.5% | 1.2% |

Those numbers are the cookbook's, on Hermes, with `jev-1.12`. They are the reason for the two-request shape. They are not an AutoSkill result.

The cookbook also records why a directory like the screenshot is hard. Of 36 wrong first picks, 10 were another skill in the right category. `django-patterns` and `django-tdd` look alike in a 60-character index line. `frontend-slides` ("HTML slide decks and PPTX-to-web") and `investor-materials` ("pitch decks, one-pagers, memos") collide the same way the cookbook's `powerpoint` and `pptx-author` collided. A wide `choice` ranked the editor; the second request, which saw 700 characters of each `SKILL.md`, flipped the winner to the author. AutoSkill keeps that second read.

The cookbook's second request can still return a neighbor. "Post this to Mastodon" survived both gates and suggested the X/Twitter skill, because nothing in the roster posts to Mastodon and a `choice` must name one of its options. AutoSkill adds a coverage check so a named system with no certified skill ends the turn with no suggestion.

## Approaches

**Index in the prompt.** What ECC does today. The agent sees every name. Cheap to ship, and the failure mode above.

**Embedding retrieval, then a generative pick.** A vector index hides the catalog, then an unconstrained model writes a name. This repository already refuses embeddings for context scoring in the Build Thread. A generated name can fall outside the catalog, and there is no calibrated "none of these".

**Jev two-pass over a certified subset.** Hard filters run in code. Jev ranks what remains with a `choice` and answers "does this turn need a skill?" with `noul`s. A second request reads three skills closely and may reject all three. The winner is one line after the cache breakpoint. This is the design below.

The third approach is the one to build. Empty is a successful answer.

## Owners

| Owner | Artifact | Changes independently |
| --- | --- | --- |
| Publisher | A git repo of `SKILL.md` files at one commit | Yes. AutoSkill stores the commit and will not move it. |
| Catalog editor | The manifest for that commit: family, role, systems, risk | Yes. A new manifest is a new catalog version. |
| Policy owner | Risk ceiling, harness, thresholds, composition | Yes. A running selector pins the policy version it started with. |
| Jev | Probabilities for the questions AutoSkill asked | Yes. AutoSkill stores the model id and the answers. |
| AutoSkill | Eligibility, composition, the suggestion line, the receipt | The receipt is append-only. |
| Harness | Whether to load the named document into the agent | Yes. The receipt does not load anything. |

## Catalog

A catalog version is one immutable JSON document. Publishing a second manifest for the same publisher commit creates a new version. Editing a published version is refused.

```json
{
  "id": "ecc",
  "version": 1,
  "publisher": "affaan-m/ecc",
  "commit": "f92dc544c4b025a456b853a7497750b5b3d6cbcd",
  "license": "MIT",
  "model_pin": "jev-1.13.0",
  "skills": [
    {
      "name": "django-tdd",
      "family": "django",
      "role": "tdd",
      "systems": ["django"],
      "languages": ["python"],
      "harnesses": ["any"],
      "risk": "write-repo",
      "index_description": "Django TDD workflow",
      "description_full": "Django TDD workflow",
      "excerpt": "first 700 characters of the skill body after frontmatter",
      "digest": "sha256:…",
      "path": "skills/django-tdd/SKILL.md",
      "status": "active"
    }
  ]
}
```

| Field | Rule |
| --- | --- |
| `name` | Stable id. Unique inside the version. The `choice` option key. |
| `family` | Skills the screenshot groups together. `django` holds the four Django documents. `cross` holds procedures that apply across stacks: `tdd-workflow`, `verification-loop`, `security-review`, `eval-harness`, `search-first`, `coding-standards`. |
| `role` | `patterns`, `security`, `tdd`, `verification`, `testing`, `workflow`, or `domain`. |
| `systems` | Products this skill actually operates. Empty for a cross-cutting procedure. |
| `index_description` | The only text the wide `choice` sees for this skill. At most 80 characters, cut on a word boundary. |
| `description_full` | The publisher's description, uncut. Used on the second request. |
| `excerpt` | The first 700 characters of the body. The cookbook's `EXCERPT_CHARS`. Stored in the manifest so a selection does not fetch the repo. |
| `digest` | SHA-256 of the file bytes at `commit` and `path`. |
| `risk` | `read`, `write-repo`, `exec`, or `external`, in that order. A policy ceiling admits a skill at or below it. |
| `status` | `active` or `revoked`. Revocation is a new catalog version that copies the previous manifest and flips status. |

`index_description` is allowed to be shorter than `description_full` and is never rewritten at request time. The wide ranking stays comparable across turns, which keeps a cached roster prefix stable.

### Certification

A skill enters `skills[]` only after the checks below have passed for that exact digest. Certification is a property of a manifest entry. It is not a claim that Anthropic, TypeSafe, or ECC certified the file.

1. The file is resolved from `publisher` at `commit` and `path`, and the digest matches.
2. The path is `skills/<name>/SKILL.md` or a single markdown file directly under `skills/`, which is the shape in the screenshot (`mle-reviewer.md`, `docs/examples/project-guidelines-template.md` stays out unless the manifest names it).
3. Frontmatter, when present, agrees with `name`.
4. A secret scan and an instruction-override scan over the file find nothing (embedded credentials, "ignore previous instructions", a pipe of remote content into a shell).
5. `family`, `role`, `systems`, and `risk` come from the manifest. A deterministic proposal may fill them for review: a name `django-tdd` proposes family `django` and role `tdd`; a name in the cross list proposes family `cross`. A skill whose role is still unset is omitted from the published version.
6. `risk` of `exec` or `external` needs a recorded reviewer on the manifest entry. `read` and `write-repo` from a publisher the catalog editor has already accepted can publish on the scans alone.
7. License on the catalog is the publisher's license. A file whose header states a different license is omitted.

The selector never sees a file that failed these checks. Jev is not asked to decide whether a skill is safe to include.

### Gap list

The manifest may carry a `gaps` array of system names this catalog does not operate (`"mastodon"` on a roster that can post to X and cannot post to Mastodon). A turn whose text matches a gap, and matches no `systems` entry, returns no suggestion and does not call Jev. The gap list is a precision list for known holes. Coverage of unnamed systems is the `same_system` noul in the second request.

## Turn

```json
{
  "request": "Add an Invoice model in Django and write the tests first.",
  "recent_context": "",
  "repo_signals": {"languages": ["python"], "frameworks": ["django"], "markers": ["manage.py"]},
  "harness": "cursor",
  "named_systems": ["django"]
}
```

`recent_context` is capped at 2000 characters. `repo_signals` are marker files and dependency names the harness already computed. `named_systems` is a dictionary match of the request against the catalog's `systems` plus `gaps`, not a model extraction. The state sent to Jev is `request`, `recent_context`, `repo_signals`, and `harness`. Skill bodies are not part of that state on the first request.

## Eligibility

Eligibility runs before any Jev call. Its output is the option set.

1. `status` is `active`, the digest is present, and `risk` is at or under the policy ceiling.
2. `harnesses` contains the turn's harness or `any`.
3. When `named_systems` hits one or more `systems` values, keep every skill that lists any named system, every skill whose family matches a repo framework, and every `cross` skill that survived steps 1 and 2. Naming Go inside a Django repo keeps both families.
4. When nothing is named and a repo framework matches a family, keep that family plus `cross`.
5. When nothing is named and a language matches, keep that language family plus `cross`.
6. Otherwise keep the full active set.

A family larger than the chunk cap is split in the chunk step. Eligibility does not drop a skill to make the `choice` fit.

## Jev requests

Constants, initial values taken from the cookbook where the cookbook defined them:

| Name | Value | Role |
| --- | --- | --- |
| `MODEL` | `jev-1.13.0` | Pinned. `jev-latest` is refused. |
| `CHUNK` | 200 | Options per `choice`. The API allows 255. |
| `SHORTLIST` | 3 | Skills reread on request 2. |
| `EXCERPT_CHARS` | 700 | Already applied when the manifest was built. |
| `GATE_THRESHOLD` | 0.30 | Mean of the oriented gate nouls. |
| `FITS_THRESHOLD` | 0.30 | The winner's own fit. |
| `SAME_SYSTEM_THRESHOLD` | 0.30 | The winner's own same-system noul. Starting value, not a measured one. |

Policy is a separate immutable version, not a field of the catalog. A turn pins one policy version the way a case pins a contract.

```json
{
  "version": 1,
  "ceiling": "write-repo",
  "composition": "strict",
  "placement": "selected",
  "thresholds": {"gate": 0.30, "fits": 0.30, "same_system": 0.30},
  "model": "jev-1.13.0"
}
```

`placement` is `selected` (no skill index in the prompt) or `indexed` (a stable index for this catalog version, with the block appended after the cache breakpoint). Neither placement inlines a skill body.

### Gate call

The three gate nouls go out alone, on the turn state, before any `choice`. Chunk order would otherwise change which skill descriptions sit beside the gate, and a refusal would still have paid for a 200-option ranking. A gate under `GATE_THRESHOLD` returns no suggestion. No chunk call is sent.

### Request 1, per chunk

Pack eligible skills into chunks of at most 200, ordered by family name so the packing is stable. Pack a whole family into one chunk when it fits. Split a family that does not, role by role.

Each chunk is one `systemone` call whose only question is `which`. The chunk calls are independent and may run together.

`which` instructions, used verbatim:

> Which of these skills, if any, is the right one to load to help with the user's latest request?

Criteria: `{skill.name: skill.index_description}` for the chunk.

Gate nouls, instructions used verbatim, sent as `gate::acts_on_user_system`, `gate::would_follow_documented_procedure`, and `gate::prose_suffices`:

| Key | Instruction | Orientation |
| --- | --- | --- |
| `acts_on_user_system` | Is the assistant being asked to act on the user's files, accounts, devices, or online services, rather than only to explain or advise? | yes favors a skill |
| `would_follow_documented_procedure` | Would a careful expert answering this consult a specific documented procedure or set of commands, rather than answering from general understanding? | yes favors a skill |
| `prose_suffices` | Could a knowledgeable generalist fully satisfy this request in prose, with no tools, no documentation, and no access to the user's files or accounts? | yes counts against a skill |

The gate is the mean of the three values after `prose_suffices` is replaced with `1 - noul`. The cookbook's advice on these questions holds: they ask whether an action is wanted. A question about subject matter cannot separate "explain a monad" from a request that needs `django-tdd`.

When there were two or more chunks and the gate passes, one more `choice` runs over the top two names from each chunk, using the same index descriptions and the same `which` instructions. That call's probabilities are the ranking. A merge set larger than 200 means eligibility is too wide for this catalog; the selector returns no suggestion with reason `merge_overflow` and does not recurse.

### Request 2

Take the top `SHORTLIST` names. One call:

`which` instructions, verbatim:

> Exactly one of these skills is the right one to load for the user's latest request. Which one? Read what each actually does, not just its name.

Criteria: `"{description_full} — {excerpt}"` per name.

For each name, two nouls:

- `fits::{name}`: Does the skill '{name}' do the specific thing the user's request asks for? It is described as: {description_full}
- `same_system::{name}`: Does the skill '{name}' operate the same product, framework, file type, or system the user named, rather than a neighboring one? It is described as: {description_full}. If the user named no product or framework, answer whether it performs the kind of work they asked for.

`fits` and `same_system` are absolute. They can all come back low, which is what lets request 2 reject the whole shortlist. They do not have to agree with `which`. On the cookbook's pitch-deck turn, the nouls scored the editing skill higher while the `choice` picked the authoring skill. AutoSkill treats that disagreement as a refusal, not as a license to inject the `choice`.

## Composition

Default composition, `strict`:

1. No eligible skill yields `none`, reason `empty_roster`. A gap-list hit yields `none`, reason `gap`, and skips Jev.
2. Gate under `GATE_THRESHOLD` yields `none`, reason `gate`.
3. Request 2's `choice` must be one of the three shortlist names. Any other key yields `none`, reason `choice_outside_shortlist`.
4. `fits` of that winner under `FITS_THRESHOLD` yields `none`, reason `winner_fits`.
5. `same_system` of that winner under `SAME_SYSTEM_THRESHOLD` yields `none`, reason `same_system`.
6. Otherwise `suggest`, reason `cleared`, and the payload is that one name.

A shortlist member with a high fit does not replace a winner who failed step 4 or 5. Substituting would mix the `choice`, which picks among options, with the nouls, which answer whether to speak.

The cookbook's composition is implemented beside this one under the name `cookbook` and is off unless the policy sets it. It returns the `choice` whenever the maximum `fits` on the shortlist clears `FITS_THRESHOLD`, including when the winner's own fit is lower. The Hermes numbers used that rule. AutoSkill's default is stricter because a certified library is full of lookalikes, and a confident wrong line is how the cookbook broke 7 covered turns while fixing 37. Both compositions are recorded on the receipt so an evaluation can compare them on ECC turns. Threshold changes are policy changes. They are not edits to a published receipt.

Companions (another skill in the winner's family, for example attaching `django-verification` beside `django-tdd`) stay out of the default. The measured gain on Hermes was one line. A family attachment can be added later as a third request with its own fit noul, capped at one extra name, default off.

## What the agent sees

The block is a fixed string. Wording is part of the measurement, as in the cookbook: changing a word changes the cache key of every graded turn.

When the decision is `suggest`:

```
<skill_relevance>
Relevant to the current request: django-tdd. Ignore this if it does not fit what the user actually asked for.
</skill_relevance>
```

When the decision is `none`:

```
<skill_relevance>
No skill in the certified roster appears relevant to this request.
</skill_relevance>
```

The block is always emitted. Silence leaves a harness instruction of "load something" unopposed.

With `placement` set to `selected`, the agent prompt does not contain the skill index. It contains the block, after the cache breakpoint. The harness loads the body later, by digest, when the agent asks for that skill. `indexed` keeps a stable index in the prompt and appends the block, for a harness that already ships an index. The index bytes are a function of the catalog version alone.

The "ignore this if it does not fit" sentence stays. A harder instruction would also raise compliance on the turns the suggestion gets wrong.

## Receipt

Every decision writes one receipt, including the refusals. Jev probabilities are stored as the provider's signal. They are not evidence that the skill is the right one. The same rule the Build Thread uses for a provider's "tests passed" claim applies here.

```json
{
  "catalog_id": "ecc",
  "catalog_version": 1,
  "policy_version": 1,
  "composition": "strict",
  "model": "jev-1.13.0",
  "eligible_count": 11,
  "chunks": 1,
  "gate": 0.74,
  "gate_values": {"acts_on_user_system": 0.80, "would_follow_documented_procedure": 0.70, "prose_suffices": 0.28},
  "ranked": [["django-tdd", 0.61], ["tdd-workflow", 0.22], ["django-patterns", 0.11]],
  "shortlist": ["django-tdd", "tdd-workflow", "django-patterns"],
  "fits": {"django-tdd": 0.71, "tdd-workflow": 0.44, "django-patterns": 0.28},
  "same_system": {"django-tdd": 0.86, "tdd-workflow": 0.40, "django-patterns": 0.63},
  "decision": "suggest",
  "skill": "django-tdd",
  "digest": "sha256:…",
  "reason": "cleared"
}
```

`reason` on a suggestion is `cleared`. On a refusal it is the check that stopped the turn.

Loading the body rechecks `status` and `digest` against the catalog version on the receipt. A revoked skill or a digest mismatch loads nothing and appends a second receipt with reason `digest_mismatch` or `revoked`. The selector does not accept a URL, a path, or a skill name from the agent or from Jev outside the option keys it sent.

On a Build Thread, the receipt is the natural observation to attach before `propose`: the provider context includes the suggestion block, and the journal stores the receipt. The provider still cannot accept a change, and a skill's instructions are not an admission rule.

## Adapter

```text
selector(turn, catalog, policy, answers) -> receipt
```

`answers` is the `answers` object from one or more `systemone` responses. The pure function does not open a socket. Tests feed it recorded JSON.

The HTTP command reads `TYPESAFE_API_KEY` from the environment, posts to `https://api.typesafe.ai/v1/systemone`, times out at 120 seconds, and prints the response JSON. The key never enters the receipt, the catalog, or a log line. A non-200, a missing `answers` object, or a `choice` whose probabilities omit a sent option becomes reason `jev_unavailable` and decision `none`. The hosted proxy at `https://jevtypesafeai.com/api/v1/decide` speaks the same question types and is not the adapter this design pins.

Token budget stays inside the published limits: state plus questions at most about 64k tokens, and state plus the longest single question at most about 32k. The 80-character index line and the 200-option chunk exist so a 286-skill tree does not depend on being under 255 by luck. The three gate nouls share one call. Each chunk's `choice` is its own call, and request 2 batches its `choice` with the per-skill nouls. A separate round trip per noul pays the state cost again and throws away the parallelism the API gives one request.

## Worked turns on the screenshot tree

**Django tests first.** Request names Django. Eligibility keeps the Django quartet, `python-patterns`, `python-testing`, and the cross procedures. Request 1 ranks `django-tdd` above `tdd-workflow` and `django-patterns`. Request 2 reads the three bodies. `django-tdd` clears fit and same-system. The block names `django-tdd`.

**Pitch deck skeleton.** No repo framework. The active set is chunked. The wide ranking surfaces `frontend-slides` and `investor-materials`, the same collision as `powerpoint` against `pptx-author`. Request 2 sees the excerpts: HTML decks and PPTX-to-web, versus pitch decks, one-pagers, and memos. The winner is whichever `choice` returns, and only if that winner's own fit and same-system nouls clear. If they disagree, the block says nothing in the roster fits.

**Explain a monad.** `prose_suffices` comes back high, the oriented gate falls under 0.30, and request 2 does not run. The block says nothing in the roster fits.

**Post this to Mastodon.** `mastodon` is on the gap list and on no `systems` entry. Decision `none`, reason `gap`, zero Jev calls.

## Evaluation

Build a labeled set in the cookbook's shape before tuning thresholds: one covered request per active skill, written from that skill's body, plus uncovered requests (ordinary prose, technical questions no skill serves, and requests that name a system on the gap list). Grade the harness's first load.

| Metric | Meaning |
| --- | --- |
| Wrong load | Covered request whose first load is not the covering skill. Loading nothing counts. |
| Needless load | Uncovered request that loads any skill. |
| Lookalike | Wrong load whose skill shares the covering skill's family. |
| Regressions | Covered requests the suggestion breaks that the unassisted agent had right, counted beside the ones it fixes. |

Run `strict` and `cookbook` on the same answers. Report the model id, the catalog version, and both directions. The Hermes table is the prior, not the result.

## First slice

1. Manifest schema and certification checks, including the deterministic family proposal and the requirement that an unset role is unpublished.
2. Eligibility, chunk packing, and both compositions as pure functions over recorded `systemone` JSON.
3. The suggestion block, byte-stable for a given decision and name.
4. Fixture tests that replay a gate refusal, a lookalike flip, a `choice`/`fits` disagreement, a gap hit, and a digest mismatch on load. No network.

The HTTP command, a pinned ECC manifest for the screenshot tree, the labeled evaluation, and the Build Thread observation come after that slice. Each one consumes the receipt. None of them changes the composition.

## Invariants

- A suggestion names one skill, or it names none.
- Every option key was an eligible, active manifest entry on the catalog version in the receipt.
- The winner's body is loaded only when its digest still matches that entry.
- Jev's probabilities do not admit an action, pass a check, or raise a risk ceiling.
- The agent, the planner, and Jev cannot supply a skill path or a destination URL.
- A published catalog version is immutable. Revocation publishes a new version.
- `jev-latest` is not a legal model id on a receipt.
