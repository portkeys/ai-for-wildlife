"""One-off A/B: does the geographic-prior system prompt change the result?

Runs the real production pipeline (compress -> native video -> Auto models) twice on
the SAME compressed clip, varying ONLY the system prompt's location line. Anecdotal
(one clip, temperature=0.2 so some run-to-run noise) — a sanity check, not a benchmark.

    python scripts/ab_location_test.py videos/IMG_0008.MP4
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend import llm, media, prompts  # noqa: E402


def _auto_models() -> list[str]:
    """Read AUTO_MODELS from app.py without importing it (importing triggers a DB
    connection that needs psycopg, which local dev may not have)."""
    tree = ast.parse((ROOT / "backend" / "app.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "AUTO_MODELS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError("AUTO_MODELS not found in app.py")


load_dotenv()
API_KEY = os.environ["OPENROUTER_API_KEY"]
AUTO_MODELS = _auto_models()

# The region-blind prompt = the live system prompt with the location line removed.
WITH_LOCATION = prompts.SYSTEM_PROMPT
WITHOUT_LOCATION = prompts.SYSTEM_PROMPT.replace(prompts._LOCATION_LINE, "")


def fmt(res: dict) -> str:
    if res["status"] != "done":
        return f"  {res['model']:<28} ERROR: {res['error']}"
    return (
        f"  {res['model']:<28} {str(res['species_common']):<22} "
        f"({str(res['species_scientific'])})  | {res['behavior']:<18} "
        f"conf={res['confidence']}  ${res['cost_usd']:.4f}"
    )


async def run_condition(label: str, system_prompt: str, video_uri: str) -> list[dict]:
    llm.SYSTEM_PROMPT = system_prompt  # swap the only variable
    print(f"\n===== {label} =====")
    print(f"(system prompt {len(system_prompt)} chars)")
    results = await asyncio.gather(
        *(llm.analyze_with_model(API_KEY, m, video_data_uri=video_uri) for m in AUTO_MODELS)
    )
    for r in results:
        print(fmt(r))
    cons = llm.compute_consensus(results, "majority")
    print(f"  --> consensus species: {cons['majority_species']!r}  "
          f"level={cons['level']}  agreement={cons['species_agreement']}  "
          f"needs_review={cons['needs_review']}")
    return results


async def main(src: Path):
    print(f"Compressing {src} ...")
    comp = await asyncio.to_thread(
        media.compress_for_upload, src, Path("/tmp/ab_clip.mp4")
    )
    if not comp:
        print("compress failed"); return
    size_kb = comp.stat().st_size / 1024
    print(f"compressed -> {size_kb:.0f} KB")
    video_uri = media.video_as_data_uri(comp)

    await run_condition("WITH location (Namibia)", WITH_LOCATION, video_uri)
    await run_condition("WITHOUT location (region-blind)", WITHOUT_LOCATION, video_uri)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
