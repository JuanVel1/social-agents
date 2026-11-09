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

    p_redesign = sub.add_parser("rediseñar", help="Rehace las imágenes de una parrilla ya escrita con fotos "
                                                  "generadas con IA y reemplaza sus filas pendientes en Notion")
    p_redesign.add_argument("--client", required=True, help="Carpeta del cliente en clients/")
    p_redesign.add_argument("--month", required=True, help="Mes en formato AAAA-MM")
    p_redesign.add_argument("--dry-run", action="store_true", help="Solo genera y diseña; no toca Notion ni Postiz")

    p_photos = sub.add_parser("fotos", help="Actualiza el banco de fotos del cliente con sus publicaciones de Instagram")
    p_photos.add_argument("--client", required=True, help="Carpeta del cliente en clients/")

    p_char = sub.add_parser("personaje", help="Personaje 3D animado de la marca (Hugging Face)")
    char_sub = p_char.add_subparsers(dest="action", required=True)
    p_master = char_sub.add_parser("maestro", help="Convierte una foto real en personaje (varias variantes)")
    p_master.add_argument("--client", required=True, help="Carpeta del cliente en clients/")
    p_master.add_argument("--foto", required=True, help="Foto de la persona: de frente, buena luz, medio cuerpo")
    p_master.add_argument("--variantes", type=int, default=3, help="Cuántas variantes generar (máx. 5)")
    p_choose = char_sub.add_parser("elegir", help="Deja una variante como personaje oficial (maestro.png)")
    p_choose.add_argument("--client", required=True, help="Carpeta del cliente en clients/")
    p_choose.add_argument("--variante", required=True, help="Número de la variante (maestro-<n>.png)")
    p_poses = char_sub.add_parser("poses", help="Genera las poses que faltan en el banco del personaje")
    p_poses.add_argument("--client", required=True, help="Carpeta del cliente en clients/")
    p_poses.add_argument("--max", type=int, default=None, help="Máximo de poses en esta tanda")

    p_onboarding = sub.add_parser("onboarding", help="Formulario de clientes nuevos en Notion")
    onboarding_sub = p_onboarding.add_subparsers(dest="action", required=True)
    p_setup = onboarding_sub.add_parser("setup", help="Crea la base de datos del formulario en Notion")
    p_setup.add_argument("--parent", required=True, help="ID o enlace de la página de Notion donde crearla")
    onboarding_sub.add_parser("run", help="Rellena las filas nuevas con su manual en PDF y genera el "
                                          "brand.yaml de las aprobadas")

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
    elif args.command == "rediseñar":
        from .llm.base import NoQuotaError
        from .pipeline import redesign_month

        try:
            result = redesign_month(settings, args.client, args.month, args.dry_run)
        except NoQuotaError as exc:
            print(f"No se generaron imágenes: {exc}. No se cambió nada en Notion ni en Postiz.")
            return 1
        if "poses" in result:
            print(f"Piezas con el personaje ({result['poses']} poses en el banco) · filas reemplazadas en "
                  f"Notion: {result['reemplazadas']}"
                  + (f" · sin tocar: {', '.join(result['sin_tocar'])}" if result["sin_tocar"] else ""))
            return 0
        print(f"Fotos generadas: {result['generadas']} (reutilizadas {result['reutilizadas']}, con la original "
              f"{result['originales']}) · filas reemplazadas en Notion: {result['reemplazadas']}"
              + (f" · sin tocar: {', '.join(result['sin_tocar'])}" if result["sin_tocar"] else ""))
    elif args.command == "fotos":
        from .llm import get_provider
        from .models import Brand
        from .photos import refresh_bank

        result = refresh_bank(settings, Brand.load(settings.clients_dir, args.client), get_provider(settings))
        print(f"Recortes nuevos: {result['nuevas']} ({result['utiles_nuevas']} útiles) · "
              f"fotos útiles en el banco: {result['utiles']}")
    elif args.command == "personaje":
        from pathlib import Path

        from . import character
        from .llm.base import NoQuotaError
        from .models import Brand

        if args.action == "elegir":
            print(f"Personaje oficial: {character.choose_master(settings.clients_dir, args.client, args.variante)}")
            return 0
        brand = Brand.load(settings.clients_dir, args.client)
        editor = character.QwenEditor(settings.huggingface_token, settings.character_space)
        try:
            if args.action == "maestro":
                made = character.make_masters(settings.clients_dir, brand, Path(args.foto), editor,
                                              min(args.variantes, len(character.MASTER_SEEDS)))
                print("Variantes:")
                for path in made:
                    print(f"  {path}")
                print(f"Elige una con: social-agent personaje elegir --client {brand.slug} --variante <n>")
            else:
                from .llm import get_provider

                r = character.fill_poses(settings.clients_dir, brand, get_provider(settings), editor, limit=args.max)
                print(f"Poses nuevas: {r['nuevas']} (descartadas {r['descartadas']}) · listas: {r['listas']} · "
                      f"faltan: {r['faltan']}" + (" · sin cuota de GPU, sigue más tarde" if r["sin_cuota"] else ""))
        except NoQuotaError as exc:
            print(f"No se generó nada: {exc}")
            return 1
        except FileNotFoundError as exc:
            print(exc)
            return 1
    elif args.command == "onboarding":
        from .onboarding import OnboardingBoard, run

        board = OnboardingBoard(settings.notion_token, settings.notion_onboarding_database_id)
        if args.action == "setup":
            database = board.create_database(args.parent)
            print(f"Base creada: {database['url']}")
            print(f"Pon en .env: NOTION_ONBOARDING_DATABASE_ID={database['id'].replace('-', '')}")
            print("Luego, en Notion, crea una vista Formulario (ver README).")
        else:
            from .llm import get_provider

            result = run(board, settings.clients_dir, lambda: get_provider(settings))
            print(f"Filas listas para revisar: {result['por_revisar']} · Clientes activados: {result['activos']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
