#!/usr/bin/env bash
#
# reset-prod.sh — wipe the AI for Wildlife library back to empty.
#
# Deletes EVERY video via the public delete endpoint (the same one the 🗑 trash
# button uses), which removes the DB rows (video + analyses + review) AND the
# stored files (upload, thumbnail, compressed clip, frames) in the GCS bucket.
# Idempotent: if the library is already empty it does nothing.
#
# Usage:
#   scripts/reset-prod.sh                              # prod, asks to confirm
#   scripts/reset-prod.sh -y                           # prod, no prompt
#   BASE=http://localhost:8000 scripts/reset-prod.sh   # target local dev instead
#
set -euo pipefail

BASE="${BASE:-https://ai-for-wildlife.threeportkeys.com}"

ASSUME_YES=0
[[ "${1:-}" == "-y" || "${1:-}" == "--yes" ]] && ASSUME_YES=1

list_ids() {
  curl -fsS --http2 "$BASE/api/videos" \
    | python3 -c "import sys,json; print('\n'.join(v['id'] for v in json.load(sys.stdin)['videos']))"
}

ids="$(list_ids)"
n="$(printf '%s' "$ids" | grep -c . || true)"

if [[ "$n" -eq 0 ]]; then
  echo "Library at $BASE is already empty — nothing to delete."
  exit 0
fi

echo "About to delete $n video(s) from $BASE:"
curl -fsS --http2 "$BASE/api/videos" \
  | python3 -c "import sys,json; [print(' -', v['id'], '·', v.get('original_name','')) for v in json.load(sys.stdin)['videos']]"

if [[ "$ASSUME_YES" -ne 1 ]]; then
  read -r -p "Delete all $n video(s)? This cannot be undone. [y/N] " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "Aborted."; exit 1; }
fi

while IFS= read -r id; do
  [[ -z "$id" ]] && continue
  echo -n "Deleting $id … "
  curl -fsS --http2 -X DELETE "$BASE/api/videos/$id" && echo
done <<< "$ids"

left="$(list_ids | grep -c . || true)"
echo "Done — $left video(s) left."
