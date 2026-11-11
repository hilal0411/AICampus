# Blind-Bewertung (Studie: Tutor:innen vs. LLMs)

Projektgruppe Visual Computing, WS 2026/27. Ziel: vergleichen, wie stark
Bewertungen von Tutor:innen und verschiedenen LLMs bei denselben Antworten
streuen.

## Grundprinzip

- **Blind:** Tutor:innen sehen nur Aufgabe, Musterlösung und Antwort. Keine
  LLM-Bewertung, keine Bewertungen anderer Tutor:innen, kein Vorschlagswert im
  Punktefeld (kein Anker-Effekt).
- **Getrennt vom Kursbetrieb:** Studien-Antworten liegen in eigenen Tabellen
  (`study_items`, `study_ratings`, `study_model_ratings`). Sie tauchen nicht in
  Aufgaben, Übersicht oder Statistik auf, ändern keine Noten und lösen keine
  Benachrichtigungen aus.
- **Keine echten Abgaben nötig:** gedacht für synthetische oder freigegebene
  Antworten (Datenschutz).
- **Mehrere Bewertungen pro Antwort:** eine je Tutor:in, damit die Streuung
  zwischen Menschen messbar ist.
- **Zufällige Reihenfolge je Tutor:in** (fest pro Person), gegen
  Reihenfolge-Effekte.

## Einrichtung (lokal)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
bash scripts/fetch_vendor.sh      # JS-Bibliotheken (Markdown/LaTeX-Rendering), nicht im Repo
uvicorn main:app --reload         # → http://localhost:8000, Login admin/admin
```

## Ablauf

1. **Kurs anlegen** (Admin-Konsole) und Tutor:innen als `TUTOR` hinzufügen.
2. **Antworten importieren:** Kurs → Tab *🙈 Blind-Bewertung* → *Studie
   verwalten* (PROF/Admin) → CSV oder JSON hochladen.
3. **Tutor:innen bewerten** im selben Tab: Antwort lesen, Punkte und optional
   eine Begründung eingeben, *Speichern & nächste Antwort*. Eigene Bewertungen
   lassen sich über *Meine Bewertungen* nachträglich ändern.
4. **Modelle bewerten lassen:** im selben Bereich unter *🤖 Modelle* (siehe unten)
   oder per Skript.
5. **Exportieren:** *Alle Bewertungen als CSV exportieren* (PROF/Admin).

## Import-Format

CSV (Komma oder Semikolon, UTF-8) oder JSON-Liste mit diesen Feldern.
Beispiel: [`blind-grading-example.csv`](blind-grading-example.csv)

| Feld | Pflicht | Bedeutung |
|---|---|---|
| `task_key` | ja | Gruppiert Antworten derselben Aufgabe, z. B. `swt-pflichtenheft` |
| `task_title` | ja | Titel der Aufgabe |
| `task_description` | ja | Aufgabenstellung (Markdown + LaTeX) |
| `model_solution` | nein | Musterlösung (Markdown + LaTeX) |
| `max_points` | ja | Maximale Punktzahl |
| `answer` | ja | Die zu bewertende Antwort |
| `intended_points` | nein | Beabsichtigte Punktzahl beim Erstellen. **Tutor:innen sehen sie nie.** |
| `error_type` | nein | z. B. `korrekt`, `kleiner-fehler`, `fehlkonzept`, `off-topic`. **Nie sichtbar.** |
| `source` | nein | z. B. `handgeschrieben`, `llm:qwen3`. **Nie sichtbar.** |

## Modelle in der Oberfläche

Unter *Studie verwalten → 🤖 Modelle* (PROF/Admin):

- **Modell hinzufügen:** Modellname, API-URL, API-Key, optional ein eigener
  Name (= Label in den Ergebnissen). Vorlagen für Ollama, OpenRouter, Groq.
- **testen:** bewertet die erste importierte Antwort einmal und zeigt Punkte,
  Dauer oder die Fehlermeldung. Speichert nichts.
- **Aktiv-Häkchen:** nur aktive Modelle laufen mit. **entfernen** löscht den
  Endpoint, aber keine bisherigen Bewertungen.
- **▶ Alle Antworten … bewerten lassen:** läuft im Hintergrund mit
  Fortschrittsbalken, abbrechbar; bereits erfolgreiche Läufe werden
  übersprungen.

| Anbieter | API-URL | Beispiel-Modell |
|---|---|---|
| Ollama (lokal) | `http://localhost:11434/v1` (Key: `ollama`) | `qwen3:4b`, `qwen2.5:3b` |
| OpenRouter | `https://openrouter.ai/api/v1` | Modelle mit `:free` |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.1-8b-instant` |
| vLLM (GPU-Server) | `http://<server>:8000/v1` | `Qwen3-32B` |

API-Keys werden in der lokalen Datenbank gespeichert und in der Oberfläche nur
maskiert angezeigt.

## Modelle per Skript bewerten lassen

`scripts/study_run_models.py` schickt jede Antwort mit dem normalen
AICampus-Grading-Prompt an ein oder mehrere OpenAI-kompatible Endpoints,
mehrmals pro Antwort (Selbst-Konsistenz), ohne vorheriges Feedback.

```bash
# lokales Ollama
python scripts/study_run_models.py --course 3 --runs 3 \
    --api-url http://localhost:11434/v1 --api-key ollama \
    --models qwen3:8b,llama3.1:8b

# verschiedene Endpoints je Modell (name=url)
python scripts/study_run_models.py --course 3 \
    --models "Qwen3-32B=http://gpu1:8000/v1,qwen3:8b=http://localhost:11434/v1"
```

Ohne `--models`/`--api-url` werden die in der Oberfläche eingetragenen,
aktiven Modelle genommen (sonst das Modell aus Kurs-/Admin-Settings bzw. `.env`). Bereits erfolgreiche Läufe werden übersprungen, das Skript
kann also abgebrochen und fortgesetzt werden. Mit `--prompt-file` und
`--prompt-version` lassen sich Prompt-Varianten vergleichen.

## Export (Long-Format)

Eine Zeile pro Bewertung:

`item_id, task_key, max_points, intended_points, error_type, source,
rater_type (human|model), rater (T1, T2, … bzw. Modellname), run,
prompt_version, points, seconds_spent, comment`

Tutor:innen sind pseudonymisiert (`T1`, `T2`, …). Damit lassen sich z. B.
berechnen: Streuung je Antwort zwischen Tutor:innen, Abweichung jedes Modells
vom Tutor-Mittel und von `intended_points`, Selbst-Konsistenz je Modell über
die Läufe, Übereinstimmungsmaße (ICC, Krippendorffs α).

## Code

| Datei | Inhalt |
|---|---|
| `database/models.py` (unten) | `StudyItem`, `StudyRating`, `StudyModelRating`, `StudyModelEndpoint` |
| `services/blind_grading_service.py` | Import, Reihenfolge, Export, Übersicht, Modell-Läufe (Hintergrund-Job) |
| `api/blind_grading.py` | REST unter `/api/blind/…` |
| `templates/course/blind_grading.html` | Tab *Blind-Bewertung* |
| `main.py` | Seite `/courses/{id}/blind-grading` + Tab |
| `scripts/study_run_models.py` | LLM-Läufe per Kommandozeile |
| `scripts/fetch_vendor.sh` | lädt fehlende JS-Bibliotheken nach `static/vendor/` |
