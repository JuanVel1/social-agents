from __future__ import annotations

import argparse
import logging
import sys

from .config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="social-agent", description="Agente de redes sociales")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="Genera, diseña y envía a aprobación la parrilla de un mes")
    p_plan.add_argument("--client", required=True, help="Carpeta del cliente en clients/")
    p_plan.add_argument("--month", required=True, help="Mes en formato AAAA-MM")
    p_plan.add_argument("--dry-run", action="store_true", help="No sube nada a Notion ni a Postiz")

    p_publish = sub.add_parser("publish", help="Programa en Postiz las piezas aprobadas en Notion")
    p_publish.add_argument("--now", action="store_true",
                           help="Programa en 10 minutos en vez de la fecha del calendario (para pruebas)")
    sub.add_parser("serve", help="Deja el agente corriendo con sus tareas programadas")
    sub.add_parser("models", help="Lista los modelos de Gemini disponibles para tu API key")
    sub.add_parser("integrations", help="Lista los canales conectados en Postiz (para brand.yaml)")

    args = parser.parse_args(argv)
    # La consola de Windows usa cp1252 por defecto y falla con algunos caracteres.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()

    if args.command == "plan":
        from .pipeline import run_month

        plan = run_month(settings, args.client, args.month, args.dry_run)
        counts = ", ".join(f"{t}={plan.count(t)}" for t in ("post", "carousel", "story", "reel_script"))
        print(f"Parrilla {args.client} {args.month}: {counts} → {settings.out_dir / args.client / args.month}")
    elif args.command == "publish":
        from .pipeline import publish_approved

        print(f"Piezas programadas: {publish_approved(settings, now=args.now)}")
    elif args.command == "serve":
        from .jobs import serve

        serve(settings)
    elif args.command == "models":
        from .llm.gemini import GeminiProvider

        for name in GeminiProvider(settings).list_models():
            print(name)
    elif args.command == "integrations":
        from .publish import PostizClient

        for item in PostizClient(settings.postiz_url, settings.postiz_api_key).integrations():
            print(f"{item.get('identifier', '?'):12} {item.get('id')}  {item.get('name', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
