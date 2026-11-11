"""Blind-Grading-Studie: alle Studien-Antworten eines Kurses von mehreren LLMs bewerten lassen.

Nutzt denselben Grading-Prompt wie AICampus (Textaufgaben), aber OHNE
vorheriges Feedback — jeder Lauf ist unabhängig. Ergebnisse landen in
study_model_ratings (nicht in echten Feedbacks/Noten).

Beispiele:
  # Modelle über den Endpoint aus .env / Admin-Konsole / Kurs-Settings:
  python scripts/study_run_models.py --course 3 --models Qwen3-32B --runs 3

  # Lokales Ollama (OpenAI-kompatibel), mehrere Modelle:
  python scripts/study_run_models.py --course 3 --runs 3 \\
      --api-url http://localhost:11434/v1 --api-key ollama \\
      --models qwen3:8b,llama3.1:8b,mistral:7b

  # Unterschiedliche Endpoints je Modell: name=url (Key aus --api-key)
  python scripts/study_run_models.py --course 3 \\
      --models "Qwen3-32B=http://gpu1:8000/v1,qwen3:8b=http://localhost:11434/v1"

Ohne --models/--api-url werden die Modelle genommen, die im Tab
„Blind-Bewertung“ eingetragen und aktiv sind.

Bereits vorhandene Läufe (gleiches Modell, Run, Prompt-Version) werden
übersprungen — das Skript kann also jederzeit fortgesetzt werden.
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select  # noqa: E402

from database.base import create_db_and_tables, engine  # noqa: E402
from database.models import StudyItem, StudyModelEndpoint  # noqa: E402
from services import blind_grading_service as bgs  # noqa: E402
from services.settings_resolver import get_effective_llm_config  # noqa: E402


def parse_models(spec: str, default_url: str) -> list[tuple[str, str]]:
    out = []
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        name, _, url = part.partition("=")
        out.append((name.strip(), (url or default_url).strip()))
    return out


async def run(args) -> int:
    create_db_and_tables()
    with Session(engine) as session:
        if not session.exec(select(StudyItem.id).where(StudyItem.course_id == args.course)).first():
            print(f"Kurs {args.course}: keine Studien-Antworten importiert.")
            return 1
        base = get_effective_llm_config(session, args.course)
        api_key = args.api_key or os.getenv("STUDY_API_KEY") or base["api_key"]
        ui_eps = session.exec(
            select(StudyModelEndpoint).where(StudyModelEndpoint.course_id == args.course)
            .where(StudyModelEndpoint.enabled == True)  # noqa: E712
        ).all()
        if args.models or args.api_url or not ui_eps:
            # Modelle von der Kommandozeile (bzw. Fallback: Modell aus den Settings)
            endpoints = [(name, {"api_url": url, "api_key": api_key, "model": name, "timeout": args.timeout})
                         for name, url in parse_models(args.models or base["model"], args.api_url or base["api_url"])]
        else:
            # Modelle, die in der Oberfläche (Tab „Blind-Bewertung“) eingetragen und aktiv sind
            endpoints = [(e.name, bgs.endpoint_config(e, args.timeout)) for e in ui_eps]
        prompt = base.get("grading_prompt") if args.prompt_version == "default" else None
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    print("Modelle:", ", ".join(n for n, _ in endpoints))
    job = await bgs.run_models(args.course, endpoints, args.runs, args.prompt_version, prompt)
    print(job.message)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--course", type=int, required=True, help="Kurs-ID mit importierten Studien-Antworten")
    p.add_argument("--models", default="", help="Komma-Liste, optional name=url (Default: Kurs-/Global-Modell)")
    p.add_argument("--runs", type=int, default=3, help="Wiederholungen pro Antwort und Modell (Default 3)")
    p.add_argument("--api-url", default="", help="OpenAI-kompatibler Endpoint (Default: aus Settings)")
    p.add_argument("--api-key", default="", help="API-Key (oder Env STUDY_API_KEY; Default: aus Settings)")
    p.add_argument("--prompt-version", default="default", help="Label für den verwendeten Prompt")
    p.add_argument("--prompt-file", default="", help="Eigener Grading-Prompt (gleiche Platzhalter wie in prompts/grading_prompt.py)")
    p.add_argument("--timeout", type=int, default=300, help="Timeout pro Aufruf in Sekunden")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
