# Roadmap / future development

## Adaptive model selection via multi-armed bandit (planned)

**Goal:** learn which model is most often *correct* (most in line with human review) and
preferentially route to it, while still cross-checking with others for diversity.

**Reward signal (already being captured):** every saved human review stores the final
species/behavior plus a derived `decision`:
- `approved` — human kept the models' consensus (models were right)
- `corrected` — human changed it (models were wrong)

From the stored per-model predictions + the human's final answer we can compute, per
model, a running **hit rate**: did `analyses.species_common` (and `behavior`) match the
final reviewed label? That per-model correctness is the bandit reward.

**Policy:** treat each model as an arm.
- **Exploit ~80%:** pick the model with the best posterior accuracy most of the time.
- **Explore ~20%:** sample the other models so we keep a diversity counter-check and keep
  updating their estimates (avoids locking onto a model that later drifts).
- Candidate algorithms: ε-greedy (ε≈0.2, simple) or **Thompson sampling** on a Beta(hits,
  misses) per model (handles uncertainty better, self-tunes exploration).

### Auto vs Custom (clarified product vision)

**Auto mode = adaptive.** Keeps a set of 3 active models that *evolves*:
- **Keep the human's pick.** When a reviewer agrees with a model (clicks "↳ Use this" and
  saves, or keeps that model's answer), that model is rewarded and should be re-used next
  time ("definitely use that model").
- **Explore the rest, cost-stratified.** Fill the remaining 1–2 slots by sampling other
  multimodal models with a deliberate spread — **one cheaper, one more expensive** — and
  across **different providers / open- vs closed-source families**. This (a) preserves
  error diversity so consensus stays meaningful, (b) tests whether a cheap model is "good
  enough", and (c) tests whether an expensive model is actually worth its price.
- **Evolves with the catalog.** As new models appear on OpenRouter they become candidate
  arms automatically.

**Custom mode = static.** The user picks an exact fixed list to test — selection never
changes on its own. **But it still logs the same reward signal** (which model the human
agreed with), so Custom sessions also contribute to per-model scoring; they just don't
auto-adjust the active set.

**Reward signal (both modes):** the per-model hit = did this model's species/behavior match
the human-final answer. "Use this" + Save is the explicit human-agreement event.

**Refinements to consider:**
- **Contextual bandit:** condition on context (species group, day/night/IR, habitat) — the
  best model for nocturnal canids may differ from the best for birds.
- **Keep the multi-model consensus for flagged/low-confidence clips** even when exploiting,
  since disagreement is the whole point of the review queue.
- **Cost-aware reward:** factor $/clip and latency so a marginally-better but 5× pricier
  model isn't always chosen.

**Implementation sketch:**
1. Add a `model_scores` table (model, context_key, hits, misses, updated_at).
2. On each review save, update scores by comparing every model's prediction to the final.
3. Add a `select_models()` policy used by Auto mode instead of the fixed trio.
4. Surface per-model accuracy in the UI (e.g. "Gemini: 87% match over 124 reviews").

## Model-selection philosophy & how to decide "which version"

**Diversity principle (confirmed):** prefer models from *different providers* and a mix of
*open- and closed-source* families. Consensus is only a useful confidence signal when the
models' errors are **independent** — same-family models share training data and failure
modes, so their agreement is correlated and over-confident. Cross-provider disagreement is
the honest "flag for review" signal.

**On "is Gemini 2.5 vs 3.5 Flash / Qwen3-VL vs 3.6-Plus better?" — don't pick on faith.**
Public benchmarks do not cover nocturnal IR African camera-trap footage, and newer/ bigger
is *not* guaranteed better on this niche, fine-grained task. The app itself is the right
benchmark because it has **ground truth (human review)**:
- Run a **calibration set** (~50–100 representative clips spanning species, day/night/IR,
  habitats) through each candidate version, have an expert review, and compute per-model
  **species-match** and **behavior-match** rates. Let the data rank versions — this is the
  same reward signal the bandit uses.
- Track **cost per correct ID**, not just accuracy: a model that's 2% more accurate for 5×
  the price usually isn't worth it at camera-trap volume.

**Interim heuristics (until calibration data exists):**
- Tier matters more than version: *Pro/Plus* > *Flash* > *Lite* on hard visual
  disambiguation (e.g. coyote vs. red fox at night), at higher cost/latency.
- Dedicated **vision** models (Qwen3-**VL**) are image-tuned; general "Plus/Flash" models
  also accept images but should be validated on frames specifically.
- **Thinking/reasoning** variants help *behavior* (temporal), but cost more and are slower.

**Live pricing snapshot (per 1M tokens, in/out — verify in-app):**
- Gemini: `2.5-flash-lite` $0.10/$0.40 · `2.5-flash` $0.30/$2.50 · `3.1-flash-lite`
  $0.25/$1.50 · `3-flash-preview` $0.50/$3.00 · **`3.5-flash` $1.50/$9.00** (≈5× 2.5-flash).
- Qwen: `qwen3.5-flash` $0.065/$0.26 · `qwen3-vl-235b-a22b-instruct` $0.20/$0.88 ·
  `qwen3.5-plus` $0.26/$1.56 · `qwen3.6-plus` $0.33/$1.95 · `qwen3.7-plus` $0.32/$1.28.

**Suggested starting triad** (cross-provider, cost-diverse, open+closed): closed mid-tier
`google/gemini-2.5-flash`, open vision-specialist `qwen/qwen3-vl-235b-a22b-instruct`, and a
third lineage `minimax/minimax-m3`. Exploration then samples a cheap arm (e.g.
`qwen3.5-flash` / `gemini-2.5-flash-lite`) and a pricey arm (e.g. `gemini-3.5-flash` /
`qwen3.7-plus`) to see if either changes the verdict.

## Task-specialized models (planned)

The scoreboard already shows that the best model for **species** may not be the best for
**behavior** (e.g. one model nails the ID while another reads motion/behavior better). So
instead of one model answering both, route each sub-task to its own best model with its own
prompt:
- **Species pass** — model A, prompt focused purely on fine-grained ID (a few sharp frames).
- **Behavior pass** — model B, prompt focused on temporal/motion reasoning (denser frames,
  thinking-capable model).
Pick A and B per-task from the same scoreboard data (and, later, the bandit). Costs ~2 calls
but each is cheaper/sharper; combine into one result row. Consensus/agreement is then computed
per sub-task.

## Review persistence (phased)

- **Now (experimentation phase):** each analyze is a *fresh run* — re-analyzing a clip clears
  its prior human review, because the team is still trying model combos and doesn't want stale
  "reviewed" verdicts carried over.
- **Later (production phase):** human review feedback becomes durable — stored/append-only in a
  DB, kept across re-analyses, and used as the long-term ground-truth + bandit reward. Add a
  toggle ("keep reviews across re-analysis") to switch phases.

## Reasoning / "thinking" strategy (planned)

We currently disable thinking globally — for a cheap, fast, reliably-parseable batch
pipeline, not because thinking is useless. Thinking helps most on *multi-step* visual
reasoning (behavior/temporal inference, counting, occlusion, look-alike disambiguation) and
least on shallow single-animal recognition. Better than always-on or always-off:
- **"Deep analysis" toggle** — re-enable reasoning with adequate token budget (the original
  bug was truncation: thinking + too-low max_tokens, not thinking itself).
- **Escalate-on-uncertainty** — cheap non-thinking first pass; only re-run *flagged / low-
  agreement* clips with thinking. Spends reasoning compute exactly where it pays off.
- **Task-split** — thinking for the behavior pass, non-thinking for species (ties to the
  task-specialized-models idea above).
- **Measure it** — run thinking vs non-thinking as separate arms on the scoreboard and
  compare accuracy delta against the extra cost/latency.

## Other ideas
- Auto-analyze on upload (optional toggle).
- Species autocomplete from a regional checklist to speed corrections.
- Revisit **data export** once the team specifies the schema (see below).

## Done: native video input
Resolved the "frames vs. video" question — the app now sends the **actual (compressed)
clip** as native video by default (Settings → "How the clip is sent"), using each model's
real video understanding. Frame-sampling is kept as a fallback mode. Notes:
- The clip is transcoded to ~480p/10fps H.264 so it fits inline as base64 (a ~40MB clip →
  ~200KB) — no request-size issues.
- OpenRouter has no single video format: Google/Gemini needs a `file` part, others need
  `video_url`. We route by provider and fall back to the other format on a 400.
- Cost is higher than frames (e.g. GLM-4.6V tokenizes video heavily — ~32k tokens/clip vs
  a few hundred for frames). Worth watching at scale; the scoreboard tracks $/clip.

## Open question: data export
In-app triage now covers the *workflow* views (filter tabs: Needs review = work queue,
Reviewed = finished labels, Confident = AI-accepted). What's still open is exporting the
data *out* of the app. The CSV export button was removed pending team input. Original
intent: export **all reviewed clips** as a labeled dataset — one row per video with the *human-final* species/
behavior + notes + consensus + cost (not raw per-model notes, to avoid noise). Re-add once
the team confirms what fields/format they want (e.g. per-video summary vs. per-model rows,
CSV vs. Darwin Core / camera-trap standards like Camtrap DP).
