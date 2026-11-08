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

Bereits vorhandene Läufe (gleiches Modell, Run, Prompt-Version) werden
übersprungen — das Skript kann also jederzeit fortgesetzt werden.
"""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select  # noqa: E402

from database.base import create_db_and_tables, engine  # noqa: E402
from database.models import StudyItem, StudyModelRating  # noqa: E402
from services.llm_service import LLMService  # noqa: E402
from services.settings_resolver import get_effective_llm_config  # noqa: E402


def parse_models(spec: str, default_url: str) -> list[tuple[str, str]]:
    out = []
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        name, _, url = part.partition("=")
        out.append((name.strip(), (url or default_url).strip()))
    return out


async def run(args) -> int:
    create_db_and_tables()
    llm = LLMService()
    with Session(engine) as session:
        base = get_effective_llm_config(session, args.course)
        items = session.exec(select(StudyItem).where(StudyItem.course_id == args.course)).all()
        if not items:
            print(f"Kurs {args.course}: keine Studien-Antworten importiert.")
            return 1
        models = parse_models(args.models or base["model"], args.api_url or base["api_url"])
        api_key = args.api_key or os.getenv("STUDY_API_KEY") or base["api_key"]
        prompt = base.get("grading_prompt") if args.prompt_version == "default" else None
        if args.prompt_file:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8")

        total = len(items) * len(models) * args.runs
        done = 0
        for model, url in models:
            cfg = {"api_url": url, "api_key": api_key, "model": model, "timeout": args.timeout}
            for item in items:
                for run_no in range(1, args.runs + 1):
                    done += 1
                    exists = session.exec(
                        select(StudyModelRating)
                        .where(StudyModelRating.item_id == item.id)
                        .where(StudyModelRating.model == model)
                        .where(StudyModelRating.run == run_no)
                        .where(StudyModelRating.prompt_version == args.prompt_version)
                        .where(StudyModelRating.points != None)  # noqa: E711  (fehlgeschlagene neu versuchen)
                    ).first()
                    if exists:
                        continue
                    t0 = time.monotonic()
                    res = await llm.grade_text_task(
                        task_description=item.task_description,
                        model_solution=item.model_solution or "(Keine Musterloesung hinterlegt — bitte eigenstaendig bewerten)",
                        student_solution=item.answer,
                        max_points=int(item.max_points) if float(item.max_points).is_integer() else item.max_points,  # type: ignore[arg-type]
                        custom_prompt=prompt,
                        previous_feedback="",
                        config=cfg,
                    )
                    ms = int((time.monotonic() - t0) * 1000)
                    points, comment, error = None, "", None
                    if res.get("success"):
                        data = res.get("data") or {}
                        try:
                            points = max(0.0, min(float(data.get("points")), item.max_points))
                        except (TypeError, ValueError):
                            error = f"Keine gültige Punktzahl: {data.get('points')!r}"
                        comment = str(data.get("feedback") or "")
                    else:
                        error = str(res.get("error") or "unbekannter Fehler")
                    session.add(StudyModelRating(
                        item_id=item.id, model=model, run=run_no, prompt_version=args.prompt_version,  # type: ignore[arg-type]
                        points=points, comment=comment, error=error, latency_ms=ms,
                    ))
                    session.commit()
                    status = f"{points:g}/{item.max_points:g}" if points is not None else f"FEHLER: {error}"
                    print(f"[{done}/{total}] {model} · Item {item.id} · Lauf {run_no}: {status} ({ms} ms)")
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
