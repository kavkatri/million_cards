# Plan — the variation factory

**Status: proposed. Nothing here is built yet.**

The idea, in one line: *sell one product to many different people by giving each
of them their own version of it.*

You upload a list of real products. For each one the system produces N styled
variations — cozy, practical, mass-market — where a variation is a distinct
vendor code, a distinct name, a distinct description, and its own set of
generated images shot in that style. One product becomes twenty listings, each
speaking to somebody different.

---

## 1. The structural claim

**The factory is not a new system. It is a content-generation stage bolted onto
the front of the reconciler that already exists.**

That matters more than it sounds, because it decides how much has to be built.

Today a line expands a grid into SKUs and the engine reconciles four aspects —
does the card exist, does it have its photos, is the price right, is the stock
set. A variation is *also* just a row that must exist with photos, a price, and
stock. The difference is only in where its content comes from: a size cell gets
its image from a template renderer, and a variation gets its image from a
generative model.

So the pipeline is:

```
reference list ──┐
                 ├─ expand ─→ variations ─→ generate copy ─→ generate images ─→ REVIEW ─→ approved
style presets ───┘                                                                            │
                                                                                              ▼
                                                    the existing reconciler: card · media · price · stock
```

Everything to the right of `approved` already works. Everything to the left is
what this plan is about.

Concretely, `ProductLine.grid_spec` already supports list axes, so a variation
line is:

```json
{"axes": [
  {"name": "product", "type": "list", "values": ["SKU-1041", "SKU-1042", "…"]},
  {"name": "style",   "type": "list", "values": ["cozy", "useful", "mass"]}
]}
```

with `vendor_code_template` of `"{product}_{style}"`. The grid machinery, the
duplicate-vendor-code guard, the quota accounting, the rate limiter, the task
queue, the dashboard — all of it applies unchanged.

**What genuinely has to be new:** a reference-list importer, style presets, two
generation stages, an asset store with provenance, and a human review queue.

---

## 2. What a style preset is

The preset is the most valuable object in the system. It is the thing you tune
once and reuse across every product, and it is what makes twenty listings feel
like twenty different shops rather than twenty copies.

```yaml
slug: cozy
name: Уютный
vendor_code_suffix: _cozy

audience: >
  Покупает для дома, а не для задачи. Ценит ощущение, а не характеристику.
  Читает описание целиком.

# ---- image direction ----
style_reference_images:          # what "cozy" looks like, 1-3 images
  - assets/style/cozy-01.jpg
  - assets/style/cozy-02.jpg
image_prompt_template: >
  {product_description}. Тёплый домашний свет, мягкие тени, деревянные
  поверхности, ткань в кадре. Спокойная композиция, без резких контрастов.
shot_list:                       # one entry per generated image
  - hero: product centred, three-quarter view
  - context: product in use in a lived-in room
  - detail: close crop on texture
  - scale: product beside a familiar household object

# ---- copy direction ----
title_style: "тёплый, короткий, без характеристик в заголовке"
description_style: >
  Два-три коротких абзаца. Сначала ощущение, потом польза. Без списков
  характеристик — они уходят в атрибуты карточки.
banned_words: ["premium", "уникальный", "лучший"]

# ---- commerce ----
price_multiplier: 1.0
characteristics_overrides: {}
```

Three things worth noticing:

- **`shot_list` is per-image, not per-style.** Four images that are four
  variations of the same framing look like a mistake. Four images that are hero
  / context / detail / scale look like a photoshoot.
- **`audience` is fed to the copy model**, not just documentation. It is the
  single biggest lever on whether the descriptions actually differ.
- **`banned_words`** exists because generated marketplace copy converges on the
  same dozen adjectives across every style unless you forbid them.

---

## 3. The reference list

A downloadable template on the import page, so the columns are never guessed.

| column | required | notes |
|---|---|---|
| `ref_code` | yes | your internal id; becomes the vendor-code stem |
| `title` | yes | the real product name, used as generation context |
| `description` | yes | the more real detail here, the better every downstream stage works |
| `subject_id` | yes | marketplace category |
| `brand` | yes | |
| `base_price` | yes | style multipliers apply on top |
| `image_url` or `image_file` | yes | the reference photo of the actual product |
| `dimensions_cm` | no | |
| `characteristics` | no | JSON, merged with preset overrides |

Import is a **dry run by default**: parse, validate every row, show what would
be created and what it would cost, and only then let you commit. A bad column
mapping across 100 rows × 20 styles is 2 000 wrong listings.

---

## 4. Generation stages

Each stage is a task type in the existing queue: idempotent, resumable,
retryable, and visible on the dashboard with the same green pulse.

### 4.1 `copy` — name and description

One LLM call per variation. Input: product title + description + preset
`audience`, `title_style`, `description_style`, `banned_words`. Output: a
structured `{title, description, keywords}`.

Constraints that must be enforced in code, not hoped for in the prompt:
marketplace title length, banned words, no invented specifications. **A model
must never state a measurement, material, or certification that was not in the
source row** — that is a product-liability problem, not a copy problem.

### 4.2 `prompt` — image prompt

One LLM call per variation (not per image). Input: the product description and
the preset's `image_prompt_template` plus `shot_list`. Output: N prompts, one
per shot.

Kept separate from the image call on purpose: prompts are cheap, images are not.
A bad prompt should be caught and regenerated for fractions of a cent.

### 4.3 `image` — generation

N calls per variation. Inputs per your design:

1. the **product reference image** (what the thing actually is),
2. the **style reference image(s)** (what this style looks like),
3. the **generated prompt** for this shot.

The provider is behind a seam, exactly like the marketplace adapter:

```python
class ImageProvider(Protocol):
    name: str
    async def generate(self, spec: ImageSpec) -> GeneratedImage: ...
```

You named `gpt-image-2`. I have not verified its request shape, so the adapter
should be written against this narrow interface and the concrete provider
configured per environment — the same discipline that keeps the WB adapter from
leaking into the engine. A second provider then costs one file, not a refactor.

**Every generated asset stores its full provenance**: model id and version,
every input asset hash, the exact prompt, the seed, the timestamp, and the cost.
Without that you cannot reproduce a good result, diagnose a bad one, or
regenerate image 3 of 4 without redoing the other three.

---

## 5. The review queue — the part that cannot be skipped

Generated images go to `pending_review`. Nothing reaches the marketplace until
a person approves it.

This is not caution for its own sake. A wrong-looking image on a storefront
costs conversion on every impression, and marketplaces reject or suppress
listings whose images misrepresent the product. At 2 000 variations, a 5 %
bad-generation rate is 100 bad listings — enough to matter, small enough that
nobody notices until it has been live for a week.

The review screen should be built for speed, because volume is the whole point:

- A grid of the four images per variation with the product reference pinned
  alongside for comparison.
- Keyboard-first: `A` approve · `R` reject · `G` regenerate this one shot ·
  `→` next. Reviewing 2 000 variations by mouse is not a real workflow.
- **Batch approve by style.** Once you have seen thirty good `cozy` results, the
  thirty-first is very likely fine; the reviewer's job shifts from checking each
  to spot-checking the style.
- Rejection captures a reason, and reasons aggregate per preset. Five rejections
  saying "wrong background" is a preset bug, not five image bugs — that feedback
  loop is how presets get good.

---

## 6. Cost and rate control

100 products × 20 styles × 4 images = **8 000 image generations per batch**.
Even at a few cents each that is a real invoice, and it is spent before you know
whether the style preset was any good.

Non-negotiable controls:

1. **Estimate before commit.** Every import and every run shows projected
   generations and projected cost, and requires an explicit confirmation. The
   existing dry-run pattern already does this for marketplace writes; extend it
   to spend.
2. **Hard budget ceiling** per line and per day, stored in the database and
   enforced in the planner. When it is hit, the run stops and says so.
3. **Pilot first, always.** Generate 3 products × 1 style before generating
   100 × 20. The pilot is not optional; it is the default, and going wide is an
   explicit second action.
4. **Provider rate limits get their own token buckets** in the existing limiter,
   keyed `(provider, account)`. The mechanism is already built.
5. **Never regenerate an approved asset.** The reconciler rule applies to spend:
   the state of the asset store decides what to generate, never a checkpoint or
   a re-run.

Marketplace throughput is the other ceiling: at 1 000 cards/day/account, 2 000
variations is a two-day publish even if every image is ready.

---

## 7. Data model additions

```
reference_product   ref_code, title, description, subject_id, brand,
                    base_price, image_asset_id, characteristics, source_batch

style_preset        slug, name, vendor_code_suffix, audience,
                    style_reference_asset_ids, image_prompt_template, shot_list,
                    title_style, description_style, banned_words,
                    price_multiplier, characteristics_overrides

variation           line_id, reference_product_id, style_preset_id, vendor_code,
                    state (draft|copy_done|images_done|approved|rejected|published),
                    generated_title, generated_description, generated_keywords,
                    review_note, reviewed_by, reviewed_at
                    → becomes / links to a Sku once approved

generated_asset     variation_id, shot_key, provider, model_version, prompt,
                    seed, input_asset_hashes, cost, file_path,
                    state (pending_review|approved|rejected)

generation_budget   line_id, day, spent, ceiling
```

`variation` is deliberately a sibling of `sku`, not a replacement: once approved
it produces exactly the SKU row the existing engine already knows how to
reconcile.

---

## 8. Build order

| Phase | Ships | Why first |
|---|---|---|
| **P0** | Reference import + template download + style preset CRUD + expansion preview | Costs nothing to run, and it makes the shape of the thing real. You can see 2 000 vendor codes before spending a rouble. |
| **P1** | `copy` stage + review of text only | Text is ~100× cheaper than images. Get the audience/voice separation working here, where mistakes are free. |
| **P2** | `prompt` + `image` stages, provider seam, asset store with provenance, budget ceiling | The expensive part, gated behind controls built in P0–P1. |
| **P3** | Review queue with keyboard flow and batch-approve | Becomes the bottleneck the moment P2 works. |
| **P4** | Publish bridge: approved variation → SKU → existing reconciler | Small, because the reconciler already exists. |
| **P5** | Feedback loop: rejection reasons per preset, per-style conversion once listings are live | Turns the factory from a generator into something that improves. |

The ordering is deliberately cheapest-risk-first: every phase before P2 is free
to run and tells you whether the idea works.

---

## 9. Risks worth naming now

- **Marketplace duplicate detection.** Twenty listings of one product with the
  same category and near-identical attributes may be treated as duplicates and
  suppressed. The variations must differ in substance — images, copy,
  positioning — not just a vendor-code suffix. Worth testing with 3 styles on
  1 product in the sandbox before building anything.
- **Cannibalisation.** Twenty of your own listings competing for the same query
  can split your own ranking rather than add to it. Measure per-style
  conversion; the answer may be that five good styles beat twenty mediocre ones.
- **Generated-image rejection.** Marketplaces have image content rules. Build
  the rejection reason back into the preset feedback loop.
- **Copy that invents facts.** The hardest failure to catch by eye, because
  invented specifications read fluently. Enforce in code that no measurement or
  material appears in generated copy unless it appears in the source row.
- **Provider drift.** Model versions change output character without warning.
  This is exactly why every asset stores its model version — so a sudden style
  shift is diagnosable rather than mysterious.

---

## 10. Open questions

1. How many styles realistically — 20, or 5 good ones? What are the actual style
   names and who is each for?
2. Do variations go on one seller account or spread across several? Spreading
   multiplies the daily card quota but splits the storefront.
3. Is the reference list a one-off import or a living catalogue that gains
   products over time? (Living changes the importer from a batch job into a sync.)
4. Who reviews? A queue only works if someone owns it.
5. What is the acceptable per-batch spend, so the ceiling has a real number?
6. Should rejected variations be retried automatically with a new seed, or held
   for a preset fix?
