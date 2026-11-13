from __future__ import annotations

import argparse
import contextlib
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
    p_plan.add_argument("--reemplazar", action="store_true",
                        help="Archiva en Notion las filas de ese mes que siguen en Pendiente antes de subir las nuevas")

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
    p_photos.add_argument("--importar", nargs="*", default=None, metavar="RUTA",
                          help="En vez de leer Instagram, añade fotos reales de la persona desde archivos o "
                               "carpetas; sin rutas, las imágenes sueltas de clients/<cliente>/fotos/")

    p_mood = sub.add_parser("moodboard", help="Lee el moodboard de Pinterest del cliente y actualiza su "
                                              "dirección visual")
    p_mood.add_argument("--client", required=True, help="Carpeta del cliente en clients/")
    p_mood.add_argument("--forzar", action="store_true", help="Analiza aunque el tablero no haya cambiado")
    p_mood.add_argument("--referencia", action="append", default=[], metavar="ARCHIVO",
                        help="Imagen de referencia que no está en Pinterest (se repite por imagen)")
    p_mood.add_argument("--rehacer-referencias", action="store_true",
                        help="Vuelve a diseñar las plantillas de referencias que ya existían")
    p_mood.add_argument("--solo-referencias", action="store_true", help="No relee la dirección visual")
    p_mood.add_argument("--ids", default="", help="Solo estas referencias (IDs separados por comas)")
    p_mood.add_argument("--revalidar", action="store_true",
                        help="Repite las comprobaciones automáticas en las plantillas aprobadas (sin Gemini)")
    p_mood.add_argument("--reintentar-descartadas", action="store_true",
                        help="Vuelve a diseñar solo las referencias que la prueba descartó")

    p_onboarding = sub.add_parser("onboarding", help="Formulario de clientes nuevos en Notion")
    onboarding_sub = p_onboarding.add_subparsers(dest="action", required=True)
    p_setup = onboarding_sub.add_parser("setup", help="Crea la base de datos del formulario en Notion")
    p_setup.add_argument("--parent", required=True, help="ID o enlace de la página de Notion donde crearla")
    onboarding_sub.add_parser("run", help="Rellena las filas nuevas con su manual en PDF y genera el "
                                          "brand.yaml de las aprobadas")

    p_ui = sub.add_parser("ui", help="Panel local con el estado en vivo de cada paso del agente")
    p_ui.add_argument("--port", type=int, default=8765, help="Puerto local (por defecto 8765)")
    p_ui.add_argument("--no-abrir", action="store_true", help="No abre el navegador")

    args = parser.parse_args(argv)
    # La consola de Windows usa cp1252 por defecto y falla con algunos caracteres.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()

    if args.command == "ui":
        from .dashboard.server import serve_dashboard

        serve_dashboard(settings, port=args.port, open_browser=not args.no_abrir)
        return 0
    flow = tracked_flow(args)
    if flow is None:
        return _dispatch(args, settings)
    from . import progress

    info = {k: v for k, v in vars(args).items() if k not in ("command", "action") and v not in (None, False, [], "")}
    with progress.run(flow, settings.out_dir / "_runs", args=info, client=getattr(args, "client", "") or ""), \
            contextlib.redirect_stdout(progress.Tee(sys.stdout)):
        code = _dispatch(args, settings)
        if code:
            progress.fail("Terminó sin completar: revisa el resumen")
    return code


def tracked_flow(args: argparse.Namespace) -> str | None:
    """Comandos que se ven en el panel; los instantáneos (models, integrations, elegir…) no."""
    if args.command in ("plan", "publish", "rediseñar", "fotos", "moodboard"):
        return args.command
    if args.command == "onboarding" and args.action == "run":
        return "onboarding run"
    return None


def _dispatch(args: argparse.Namespace, settings) -> int:
    if args.command == "plan":
        from .pipeline import run_month

        plan = run_month(settings, args.client, args.month, args.dry_run, replace=args.reemplazar)
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

        postiz = PostizClient(settings.postiz_url, settings.postiz_api_key)
        for item in postiz.integrations():
            print(f"{item.get('identifier', '?'):12} {item.get('id')}  {item.get('name', '')}")
            if item.get("identifier") == "pinterest":  # el tablero va en pinterest_board de brand.yaml
                for board in postiz.boards(item["id"]):
                    print(f"{'':12}   tablero {board.get('id')}  {board.get('name', '')}")
    elif args.command == "rediseñar":
        from .llm.base import NoQuotaError
        from .pipeline import redesign_month

        try:
            result = redesign_month(settings, args.client, args.month, args.dry_run)
        except NoQuotaError as exc:
            print(f"No se generaron imágenes: {exc}. No se cambió nada en Notion ni en Postiz.")
            return 1
        print(f"Fotos generadas: {result['generadas']} (reutilizadas {result['reutilizadas']}, con la original "
              f"{result['originales']}) · filas reemplazadas en Notion: {result['reemplazadas']}"
              + (f" · sin tocar: {', '.join(result['sin_tocar'])}" if result["sin_tocar"] else ""))
    elif args.command == "fotos":
        from .llm import get_provider
        from .models import Brand
        from .photos import refresh_bank

        if args.importar is not None:
            from pathlib import Path

            from .photos import IMAGE_SUFFIXES, import_photos, loose_images

            files: list[Path] = []
            for raw in args.importar:
                path = Path(raw)
                if path.is_dir():
                    files += sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
                elif path.is_file():
                    files.append(path)
                else:
                    print(f"No existe: {raw}")
                    return 1
            if not args.importar:
                files = loose_images(settings.clients_dir, args.client)
            if not files:
                print(f"No hay imágenes para importar: ponlas en {settings.clients_dir / args.client / 'fotos'} "
                      "o pasa sus rutas")
                return 1
            r = import_photos(settings.clients_dir, Brand.load(settings.clients_dir, args.client),
                              get_provider(settings), files)
            print(f"Fotos importadas: {r['nuevas']} ({r['utiles_nuevas']} útiles) · ya estaban: {r['ya_estaban']} · "
                  f"sin persona: {r['sin_persona']} · fotos reales útiles en el banco: {r['utiles']}")
            for line in r["descartadas"]:
                print(f"  descartada {line[:200]}")
            return 0
        result = refresh_bank(settings, Brand.load(settings.clients_dir, args.client), get_provider(settings))
        print(f"Recortes nuevos: {result['nuevas']} ({result['utiles_nuevas']} útiles) · "
              f"fotos útiles en el banco: {result['utiles']}")
    elif args.command == "moodboard":
        from .llm import get_provider
        from .models import Brand, VisualGuide
        from pathlib import Path

        from .moodboard import guide_path, refresh_moodboard, refresh_pin_references
        from .references import load_references, references_dir

        brand = Brand.load(settings.clients_dir, args.client)
        llm = get_provider(settings)
        from . import progress

        if not brand.moodboard.board or args.solo_referencias:
            progress.skip("direccion", "Sin tablero" if not brand.moodboard.board else "--solo-referencias")
        if not args.revalidar:
            progress.skip("revalidar", "Sin --revalidar")
        if brand.moodboard.board and not args.solo_referencias:
            result = refresh_moodboard(settings, brand, llm, force=args.forzar)
            state = "analizado de nuevo" if result["analizado"] else "sin cambios, se mantiene la guía"
            print(f"Pines con imagen: {result['pines']} · {state}")
            if result["guide"]:
                print(VisualGuide.model_validate(result["guide"]).as_text())
                print(f"→ {guide_path(settings.clients_dir, brand.slug)}")
            brand = Brand.load(settings.clients_dir, args.client)  # con la guía nueva
        if args.revalidar:
            from .references import revalidate
            from .render import ReferenceRenderer

            with ReferenceRenderer(brand, settings.templates_dir, settings.clients_dir) as render:
                failed = revalidate(settings.clients_dir, brand, render)
            print(f"Revalidadas: {len(failed)} ahora fallan")
            for line in failed:
                print(f"  {line[:200]}")
        local = tuple(Path(f) for f in args.referencia)
        if missing := [str(f) for f in local if not f.is_file()]:
            print(f"No existe: {', '.join(missing)}")
            return 1
        refs = refresh_pin_references(settings, brand, llm, local=local, force=args.rehacer_referencias,
                                      retry=args.reintentar_descartadas,
                                      only={i.strip() for i in args.ids.split(",") if i.strip()} or None)
        print(f"Referencias: {refs['nuevas']} nuevas, {refs['descartadas']} descartadas, "
              f"{refs.get('existentes', 0)} ya estaban")
        for ref in load_references(settings.clients_dir, brand.slug):
            print(f"  {ref['id']}: {ref['design'].concept}")
        print(f"→ {references_dir(settings.clients_dir, brand.slug)} (preview.png de cada una)")
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
