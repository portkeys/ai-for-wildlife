"""Prompts for wildlife species + behavior identification.

This is the single source of truth for what we ask the models. It is split out into
its own module (rather than living inline in ``llm.py``) so the prompt is a first-class,
easy-to-find artifact you can tune without touching the OpenRouter client code.

Three pieces:
  - BEHAVIOR_CATEGORIES — the controlled ethogram (see CLAUDE.md). Models must copy one
    verbatim; this is what keeps behavior labels comparable across models.
  - SYSTEM_PROMPT — the role + the GEOGRAPHIC PRIOR (where the footage was captured).
  - USER_PROMPT / USER_PROMPT_VIDEO — the per-request task + required JSON output shape.
"""
from __future__ import annotations

# Controlled behavior vocabulary (ethogram). Forcing models to pick from a fixed list
# is the single biggest lever for making BEHAVIOR agree across models — otherwise one
# model says "walking away" and another "foraging" for the same clip.
BEHAVIOR_CATEGORIES = [
    "foraging/feeding",
    "walking",
    "running",
    "resting",
    "vigilance/alert",
    "grooming",
    "drinking",
    "climbing",
    "swimming",
    "flying",
    "social interaction",
    "scent-marking",
    "hunting/stalking",
    "playing",
    "fleeing",
    "not visible/no animal",
    "other",
]

# --- Geographic prior -------------------------------------------------------------
# Where CCF's footage is captured. Telling the model the capture region is the single
# strongest piece of context for fine-grained species ID: it narrows the candidate set
# to species that actually occur here and prevents cross-continent mix-ups of look-alike
# species (cheetah vs. leopard, gemsbok/oryx vs. Arabian oryx, black-backed jackal vs.
# coyote, caracal vs. lynx). This helps even with model "thinking" disabled, because the
# effect is a re-weighting of the output distribution (recall), not step-by-step
# reasoning. See the research notes in the commit / ROADMAP.
#
# Hard-coded for now (CCF works in Namibia). To change the region, edit this one line.
# To A/B TEST the location prior, set this to "" — that removes the location sentence
# entirely and restores the original region-blind prompt, so you can compare runs.
CAPTURE_LOCATION = "Namibia, southern Africa"

_LOCATION_LINE = (
    f" This footage was captured in {CAPTURE_LOCATION}. Use the region's wildlife as "
    "helpful prior context — prefer species that genuinely occur there over visually "
    "similar species from other regions — but always defer to the visual evidence: if "
    "the animal clearly is not a regional species, report what you actually see."
) if CAPTURE_LOCATION else ""

SYSTEM_PROMPT = (
    "You are a wildlife biologist assistant analyzing camera-trap / field video. "
    "You are given an ordered, time-stamped sequence of still frames sampled from a "
    "single short video clip — they show the SAME scene as it changes over time. "
    "Identify the single most prominent animal and infer what it is doing by reasoning "
    "about how its position and posture CHANGE from frame to frame (motion between "
    "frames is your main evidence for behavior). If no animal is visible, say so. Be "
    "precise and use standard common + scientific names when confident."
    + _LOCATION_LINE
)


def build_user_prompt(is_video: bool = False) -> str:
    cats = ", ".join(f'"{c}"' for c in BEHAVIOR_CATEGORIES)
    intro = (
        "This is ONE short wildlife video clip. Watch the motion across the clip to judge "
        "the behavior, "
        if is_video else
        "The images are frames from ONE wildlife clip, in chronological order with their "
        "approximate timestamps. Compare them as a sequence to judge motion and behavior, "
    )
    return (
        intro +
        "then respond with ONLY a JSON object (no markdown) in exactly this shape:\n"
        "{\n"
        '  "species_common": "common name e.g. \\"Red fox\\" (or \\"none\\" if no animal)",\n'
        '  "species_scientific": "scientific name e.g. \\"Vulpes vulpes\\" (or \\"unknown\\")",\n'
        '  "behavior": "EXACTLY ONE of this fixed list: ' + cats + '",\n'
        '  "behavior_detail": "one sentence describing the specific observed behavior",\n'
        '  "count": integer number of individuals visible (best estimate),\n'
        '  "confidence": number 0.0-1.0 (your confidence in the species ID),\n'
        '  "notes": "anything notable: habitat, uncertainty, distinguishing marks"\n'
        "}\n"
        'The "behavior" field MUST be copied verbatim from the fixed list above; put any '
        'nuance (e.g. "walking away from camera") in "behavior_detail".'
    )


USER_PROMPT = build_user_prompt(False)
USER_PROMPT_VIDEO = build_user_prompt(True)
