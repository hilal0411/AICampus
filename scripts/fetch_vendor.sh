#!/usr/bin/env bash
# Lädt die JS-Bibliotheken nach static/vendor/, die per .gitignore nicht im
# Repo liegen (static/vendor/*.js). Ohne sie rendert die Oberfläche auf einem
# frischen Clone kein Markdown/LaTeX (marked), keine Diagramme (mermaid) und
# keine Code-Hervorhebung (highlight.js).
#
# Versionen = die in templates/base.html dokumentierten Stände.
# Quelle: cdn.jsdelivr.net (npm-Pakete); Fallback: `npm pack`.
#
# Aufruf (vom Projekt-Root):   bash scripts/fetch_vendor.sh
#
# Nicht enthalten: reveal.js (Slides) — wird aus static/vendor/reveal/reveal-build
# selbst gebaut und ist für die Blind-Bewertung nicht nötig.
set -euo pipefail

cd "$(dirname "$0")/.."
DEST="static/vendor"
mkdir -p "$DEST"

# Zieldatei | npm-Paket@Version | Pfad im Paket
FILES=(
  "marked.min.js|marked@14.1.0|marked.min.js"
  "dompurify.min.js|dompurify@3.2.5|dist/purify.min.js"
  "highlight.min.js|@highlightjs/cdn-assets@11.11.1|highlight.min.js"
  "mermaid.min.js|mermaid@12.2.0|dist/mermaid.min.js"
  "qrcode.min.js|qrcodejs@1.0.0|qrcode.min.js"
)

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for entry in "${FILES[@]}"; do
  IFS='|' read -r target pkg path <<< "$entry"
  if [[ -s "$DEST/$target" ]]; then
    echo "✓ $target (schon vorhanden)"
    continue
  fi
  if curl -fsSL --max-time 60 -o "$TMP/$target" "https://cdn.jsdelivr.net/npm/$pkg/$path" 2>/dev/null; then
    :
  elif command -v npm >/dev/null 2>&1; then
    (cd "$TMP" && npm pack "$pkg" --silent >/dev/null)
    tgz="$(ls -t "$TMP"/*.tgz | head -1)"
    tar xzf "$tgz" -C "$TMP" "package/$path"
    mv "$TMP/package/$path" "$TMP/$target"
    rm -rf "$TMP/package" "$tgz"
  else
    echo "✗ $target konnte nicht geladen werden (weder curl noch npm verfügbar)" >&2
    exit 1
  fi
  mv "$TMP/$target" "$DEST/$target"
  echo "✓ $target ($pkg)"
done

echo "Fertig. Seite im Browser neu laden (ggf. Cmd+Shift+R)."
