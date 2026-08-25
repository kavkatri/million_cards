# Design — MillionCards

A locked design system for this app. Every page reads this file before emitting
code. Do not regenerate per page — extend or amend this file when the system
needs to grow.

This is a **workspace**, not a marketing site. Nobody is being persuaded here;
someone is working. The design's job is to stay calm while numbers move, make
progress legible at a glance, and confirm success without interrupting.

## Genre

modern-minimal — dashboard / platform / internal tool.

## Macrostructure family

There is only one family. Every route is an app page.

- **App pages** · Workbench — persistent left rail, single working column, content
  in bordered panels. Variation knobs: panel count, whether the page carries a
  live region, whether the primary action is destructive.
- Marketing pages · none exist.
- Content pages · none exist.

**Nav** · N3 side-rail. **Footer** · none — a workspace has a status line, not a
footer. Enrichment is banned on every page: function carries this product.

## Theme

Custom (tuned). Anchor: peach, hue ~45°. Paper carries the peach; the accent is
a deeper version of the same hue, so the whole surface reads as one warm family
rather than a neutral page with a colour bolted on.

```
--color-paper      oklch(97.5% 0.016 55)   soft light peach
--color-paper-2    oklch(95%   0.020 52)   panels, cards
--color-paper-3    oklch(92%   0.026 50)   hover, inset wells
--color-ink        oklch(24%   0.020 45)   primary text, warm near-black
--color-ink-2      oklch(43%   0.018 45)   secondary text
--color-muted      oklch(58%   0.014 45)   de-emphasised
--color-rule       oklch(88%   0.016 50)   hairlines
--color-rule-2     oklch(92.5% 0.014 52)   lighter hairlines
--color-accent     oklch(54%   0.155 45)   primary action, active state
--color-accent-ink oklch(98%   0.012 55)   text on accent
--color-accent-sub oklch(93%   0.045 48)   accent wash: active nav, badges
--color-focus      oklch(58%   0.20  45)   focus ring only
```

Status colours are **semantic, not decorative**, and never used as the accent:

```
--color-ok         oklch(58%  0.155 150)   success, completion, the pulse
--color-ok-sub     oklch(93%  0.055 150)   pulse halo, success wash
--color-warn       oklch(68%  0.135 75)    quota capped, skipped run
--color-danger     oklch(52%  0.19  22)    failure
```

Accent hue (45°) and danger hue (22°) sit close enough that colour alone must
never carry meaning — every status pairs colour with an icon or a word.

## Typography

- **Display** · Lora, 600, roman. Headings and the wordmark. Never italic.
- **Body** · Plus Jakarta Sans, 400 / 500 / 600. All UI text.
- **Outlier** · JetBrains Mono, 400 / 500. Vendor codes, counts, cron
  expressions, quota figures — anything that must align in a column.

Three families is the ceiling and this is at it. Do not add a fourth.

`font-variant-numeric: tabular-nums` on every figure that changes. A count that
jitters as it increments reads as broken.

- Display tracking: `-0.011em`
- Type scale: perfect fourth (1.333) from a 0.9375rem base
- Measure: `65ch` max on prose, `none` on tables

## Spacing

4-point named scale in `tokens.css`. Pages use named tokens
(`var(--space-md)`), never raw values.

## Motion

Three primitives. Not four.

1. **`success-pulse`** — the signature. A one-shot green ring + wash on the exact
   element that changed, 900 ms, `--ease-out`. Fires per completed task event
   arriving over SSE. It does not loop and it does not block.
2. **`progress-fill`** — meter fills animate `transform: scaleX()`, never
   `width`. 420 ms `--ease-out`.
3. **`stagger-reveal`** — one orchestrated page entrance, 60 ms per index,
   capped at 480 ms total. One-shot; never re-fires on scroll.

Button hover and press are ordinary state transitions at `--dur-micro`, not a
fourth primitive.

- Easings: `--ease-out` `cubic-bezier(0.16, 1, 0.3, 1)`, `--ease-in`
  `cubic-bezier(0.7, 0, 0.84, 0)`, `--ease-in-out` `cubic-bezier(0.65, 0, 0.35, 1)`
- Reduced motion: spatial motion collapses to a ≤150 ms opacity change. The
  success pulse becomes a static ring that fades. Meters jump to position.
  Functional feedback survives; only the movement goes.

## Microinteractions stance

- **Silent success, visible result.** No "Done!" toast, ever. The pulse *is* the
  confirmation, and it happens on the thing that changed — the row, the meter,
  the counter — not in a corner of the screen.
- Failures get a persistent inline row, not a toast that disappears before it is
  read.
- Hover delay 800 ms on tooltips; focus delay 0 ms.
- Focus rings appear instantly. Never animate a focus ring in.
- Spinners wait 400 ms before appearing, so fast responses never flash one.
- Destructive and irreversible actions confirm. Reversible ones do not.

## CTA voice

- **Primary** · accent fill, `--radius-control`, 600 weight, `translateY(-1px)`
  on hover, `translateY(0)` at 60 ms on press.
- **Secondary** · paper-2 fill with a `--color-rule` border, same geometry.
- **Quiet** · text-only with an underline on hover. Used for anything
  destructive, so it never looks inviting.
- Copy is a verb and its object: «Запустить», «Сохранить линейку»,
  «Предпросмотр». Never «Submit», never «Click here».

## What pages MUST share

- The wordmark: `MillionCards`, Lora 600, with the accent dot after it.
- The side rail, its width, and its active-item treatment.
- Accent placement — active nav item, primary button, focus ring, meter fill.
  Never a background flood; ≤ 5 % of any viewport.
- Panel geometry: 1 px `--color-rule` border, `--radius-panel`, `--space-lg`
  padding. Panels never nest inside panels.
- The three motion primitives and their durations.

## What pages MAY differ on

- Panel count and column split within Workbench.
- Whether the page carries a live region (dashboard does; the editors do not).
- Whether a page uses the meter component at all.

## Per-page allowances

- No enrichment on any page. No illustration, no hero art, no abstract
  background. This is a tool.
- The dashboard is the only page permitted a live region and the success pulse.
- Editors are permitted a preview panel; it is a `<figure>`, not fake chrome.

## Exports

### tokens.css

Canonical. Lives at `app/web/static/tokens.css` and is imported first by
`app.css`. Every colour and font in the app references a token by name; no page
stylesheet may inline an OKLCH value or a `font-family` string.
