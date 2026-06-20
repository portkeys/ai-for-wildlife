"""OpenRouter multimodal client + cross-model consensus."""
from __future__ import annotations

import asyncio
import json
import re
import time

import httpx

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Statuses worth retrying: rate limits (429) and transient upstream errors.
RETRYABLE_STATUS = {429, 500, 502, 503, 529}
MAX_RETRIES = 4              # total attempts = MAX_RETRIES
RETRY_BACKOFF = [2, 5, 10]   # seconds between attempts (last reused if needed)

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

SYSTEM_PROMPT = (
    "You are a wildlife biologist assistant analyzing camera-trap / field video. "
    "You are given an ordered, time-stamped sequence of still frames sampled from a "
    "single short video clip — they show the SAME scene as it changes over time. "
    "Identify the single most prominent animal and infer what it is doing by reasoning "
    "about how its position and posture CHANGE from frame to frame (motion between "
    "frames is your main evidence for behavior). If no animal is visible, say so. Be "
    "precise and use standard common + scientific names when confident."
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


def _video_content_part(model: str, data_uri: str, fmt: str) -> dict:
    """Build the provider-specific video content part.

    OpenRouter has no single video format: Google/Gemini take a `file` part, while
    most others (Qwen, GLM, Perceptron, …) take an OpenAI-style `video_url` part.
    """
    if fmt == "file":
        return {"type": "file", "file": {"filename": "clip.mp4", "file_data": data_uri}}
    return {"type": "video_url", "video_url": {"url": data_uri}}


def _preferred_video_format(model: str) -> str:
    m = model.lower()
    return "file" if ("google/" in m or "gemini" in m) else "video_url"


def _first_json_object(text: str) -> str | None:
    """Return the first complete, balanced {...} object (string- and escape-aware).

    Needed because some models (e.g. reasoning models) emit the JSON twice or wrap it
    in prose — a greedy regex would grab from the first { to the last } and fail.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model response."""
    if not text:
        return None
    text = text.strip()
    # Strip ```json fences if present.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fall back to the first complete balanced {...} object.
    blob = _first_json_object(text)
    if blob:
        try:
            return json.loads(blob)
        except Exception:
            return None
    return None


async def list_video_models(api_key: str) -> list[dict]:
    """Return OpenRouter models suited to this app: video input -> text output.

    This is a video-analysis tool, so the picker only offers models that natively
    accept video and produce text. (All such models also accept images, so our
    frame-based requests work with every one of them.)
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{OPENROUTER_BASE}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        r.raise_for_status()
        data = r.json().get("data", [])
    out = []
    for m in data:
        arch = m.get("architecture") or {}
        inputs = arch.get("input_modalities") or []
        outputs = arch.get("output_modalities") or ["text"]
        if "video" not in inputs or "text" not in outputs:
            continue
        pricing = m.get("pricing") or {}
        out.append({
            "id": m.get("id"),
            "name": m.get("name"),
            "context_length": m.get("context_length"),
            "prompt_price": pricing.get("prompt"),
            "completion_price": pricing.get("completion"),
            "image_price": pricing.get("image"),
            "modalities": inputs,
        })
    out.sort(key=lambda x: (x.get("name") or "").lower())
    return out


async def _post_with_retry(client: httpx.AsyncClient, payload: dict, headers: dict):
    """POST to chat/completions, retrying transient/rate-limit statuses with backoff."""
    r = None
    for attempt in range(MAX_RETRIES):
        r = await client.post(f"{OPENROUTER_BASE}/chat/completions", json=payload, headers=headers)
        if r.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES - 1:
            retry_after = r.headers.get("retry-after")
            try:
                wait = float(retry_after) if retry_after else RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            except ValueError:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            await asyncio.sleep(min(wait, 30))
            continue
        break
    return r


async def analyze_with_model(
    api_key: str, model: str, image_data_uris: list[str] | None = None,
    frame_labels: list[str] | None = None, video_data_uri: str | None = None,
    app_url: str = "http://localhost:8000",
) -> dict:
    """Run one model on a clip and return a normalized prediction dict.

    Pass `video_data_uri` to send the clip as NATIVE video (preferred for video
    models), or `image_data_uris` (+ optional `frame_labels`) to send sampled frames.
    """
    is_video = bool(video_data_uri)

    def build_content(video_fmt: str | None) -> list:
        if is_video:
            return [
                {"type": "text", "text": USER_PROMPT_VIDEO},
                _video_content_part(model, video_data_uri, video_fmt),
            ]
        content = [{"type": "text", "text": USER_PROMPT}]
        for i, uri in enumerate(image_data_uris or []):
            label = frame_labels[i] if frame_labels and i < len(frame_labels) else f"Frame {i + 1}"
            content.append({"type": "text", "text": label})
            content.append({"type": "image_url", "image_url": {"url": uri}})
        return content

    def make_payload(content: list) -> dict:
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "temperature": 0.2,
            # Structured extraction, not deep reasoning. Many models (GLM-4.6V, MiniMax,
            # Qwen "thinking", …) otherwise spend the whole budget on hidden reasoning and
            # return EMPTY content. Disable reasoning for direct, parseable JSON.
            "reasoning": {"enabled": False},
            "max_tokens": 2500,
            "usage": {"include": True},
        }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": app_url,
        "X-Title": "Conservation Video Classifier",
        "Content-Type": "application/json",
    }

    started = time.time()
    result: dict = {
        "model": model, "status": "error", "error": None,
        "species_common": None, "species_scientific": None,
        "behavior": None, "behavior_detail": None, "count": None,
        "confidence": None, "notes": None,
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cost_usd": 0.0, "latency_ms": 0, "raw_text": None,
    }

    # For video, OpenRouter's content format differs per provider. Try the preferred
    # format, and on a 400 (format mismatch) fall back to the alternate one.
    if is_video:
        first = _preferred_video_format(model)
        video_formats = [first, "video_url" if first == "file" else "file"]
    else:
        video_formats = [None]

    try:
        r = None
        async with httpx.AsyncClient(timeout=300) as client:
            for idx, fmt in enumerate(video_formats):
                r = await _post_with_retry(client, make_payload(build_content(fmt)), headers)
                if is_video and r is not None and r.status_code == 400 and idx < len(video_formats) - 1:
                    continue  # bad format → try the alternate
                break
        result["latency_ms"] = int((time.time() - started) * 1000)

        if r.status_code != 200:
            if r.status_code == 429:
                result["error"] = (
                    "Rate-limited by the provider. Free models have strict limits — "
                    "retry in a moment, or use the Auto models."
                )
            else:
                result["error"] = f"HTTP {r.status_code}: {r.text[:200]}"
            return result

        data = r.json()
        if data.get("error"):
            result["error"] = str(data["error"])[:300]
            return result

        usage = data.get("usage") or {}
        result["prompt_tokens"] = usage.get("prompt_tokens", 0) or 0
        result["completion_tokens"] = usage.get("completion_tokens", 0) or 0
        result["total_tokens"] = usage.get("total_tokens", 0) or 0
        # OpenRouter returns actual charged cost in usage.cost when requested.
        result["cost_usd"] = float(usage.get("cost") or 0.0)

        choices = data.get("choices") or []
        if not choices:
            result["error"] = "No choices returned"
            return result
        msg = choices[0].get("message", {}) or {}
        text = msg.get("content") or ""

        # Fallback: some reasoning models leave `content` empty and put the answer (or
        # the whole response) in a separate reasoning field — parse that if needed.
        if not _extract_json(text):
            for alt in (msg.get("reasoning"), msg.get("reasoning_content")):
                if alt and _extract_json(alt):
                    text = alt
                    break
        result["raw_text"] = text[:4000]

        parsed = _extract_json(text)
        if not parsed:
            finish = (choices[0].get("finish_reason") or "")
            if not text.strip() or finish == "length":
                result["error"] = (
                    "Model used all its tokens on internal reasoning and returned no "
                    "answer. Try a non-thinking model for this task."
                )
            else:
                result["error"] = "Could not parse JSON from model output"
            return result

        result["species_common"] = str(parsed.get("species_common") or "").strip() or None
        result["species_scientific"] = str(parsed.get("species_scientific") or "").strip() or None
        result["behavior"] = str(parsed.get("behavior") or "").strip() or None
        result["behavior_detail"] = str(parsed.get("behavior_detail") or "").strip() or None
        try:
            result["count"] = int(parsed.get("count")) if parsed.get("count") is not None else None
        except Exception:
            result["count"] = None
        try:
            conf = float(parsed.get("confidence")) if parsed.get("confidence") is not None else None
            result["confidence"] = max(0.0, min(1.0, conf)) if conf is not None else None
        except Exception:
            result["confidence"] = None
        result["notes"] = str(parsed.get("notes") or "").strip() or None
        result["status"] = "done"
        return result
    except httpx.TimeoutException:
        result["error"] = "Request timed out"
        result["latency_ms"] = int((time.time() - started) * 1000)
        return result
    except Exception as e:  # noqa: BLE001
        result["error"] = str(e)[:300]
        result["latency_ms"] = int((time.time() - started) * 1000)
        return result


# ---------------------------------------------------------------------------
# Consensus
# ---------------------------------------------------------------------------

def _norm(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\s+", " ", s)
    # Drop trivial qualifiers so "red fox" == "red foxes".
    return s.rstrip("s") if len(s) > 4 else s


def compute_consensus(predictions: list[dict], policy: str = "majority") -> dict:
    """Given per-model predictions, decide confidence level + whether human review
    is needed.

    policy: "majority" (auto-accept on a strict species majority) or "unanimous"
    (auto-accept only when all successful models agree).

    Returns dict with: level (high|medium|low|none), species_agreement,
    behavior_agreement, majority_species, majority_behavior, needs_review (bool),
    n_models, n_succeeded.
    """
    ok = [p for p in predictions if p.get("status") == "done" and p.get("species_common")]
    n_models = len(predictions)
    n_ok = len(ok)

    base = {
        "n_models": n_models, "n_succeeded": n_ok,
        "species_agreement": 0.0, "behavior_agreement": 0.0,
        "majority_species": None, "majority_behavior": None,
        "level": "none", "needs_review": True, "flagged": True,
        "species_unanimous": False, "behavior_unanimous": False,
    }
    if n_ok == 0:
        return base

    def tally(key):
        counts: dict[str, int] = {}
        display: dict[str, str] = {}
        for p in ok:
            k = _norm(p.get(key))
            if not k:
                continue
            counts[k] = counts.get(k, 0) + 1
            display.setdefault(k, p.get(key))
        if not counts:
            return None, 0, 0
        top = max(counts, key=counts.get)
        return display[top], counts[top], sum(counts.values())

    sp_disp, sp_top, sp_total = tally("species_common")
    bh_disp, bh_top, bh_total = tally("behavior")

    sp_agree = sp_top / sp_total if sp_total else 0.0
    bh_agree = bh_top / bh_total if bh_total else 0.0
    sp_unanimous = sp_total > 0 and sp_top == sp_total
    bh_unanimous = bh_total > 0 and bh_top == bh_total

    # Confidence is driven primarily by species agreement.
    if n_ok == 1:
        level = "low"          # only one model — no cross-check
    elif sp_unanimous:
        level = "high"
    elif sp_top * 2 > sp_total:  # strict majority agrees
        level = "medium"
    else:
        level = "low"

    # needs_review drives the human triage queue (the "20%"). Auto-accept a clip
    # (no review needed) when the models agree enough, per the chosen policy:
    #   "majority"  -> a strict majority agree on species   (lenient, ~80/20)
    #   "unanimous" -> all successful models agree           (strict)
    # A single successful model never auto-accepts (no cross-check to trust).
    if n_ok <= 1:
        needs_review = True
    elif policy == "unanimous":
        needs_review = not sp_unanimous
    else:
        needs_review = not (sp_top * 2 > sp_total)

    base.update({
        "species_agreement": round(sp_agree, 2),
        "behavior_agreement": round(bh_agree, 2),
        "majority_species": sp_disp,
        "majority_behavior": bh_disp,
        "species_unanimous": sp_unanimous,
        "behavior_unanimous": bh_unanimous,
        "level": level,
        "needs_review": needs_review,
        "flagged": needs_review,  # alias kept for compatibility
    })
    return base
