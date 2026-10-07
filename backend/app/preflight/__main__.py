"""CLI del diagnóstico: `python -m app.preflight [--areas meta,stripe] [--org ID | --all-orgs] [--json] [--with-llm]`.

Siempre corre las verificaciones de la plataforma (variables del servidor); con --org / --all-orgs también las de
las conexiones de esas empresas. Código de salida: 0 todo bien, 1 advertencias, 2 fallas (para CI / despliegues).
"""

import argparse
import asyncio
import json
import sys

ICONS = {"pass": "✔", "warn": "!", "fail": "✘", "skipped": "-"}


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.preflight", description="Diagnóstico de integraciones (solo lectura)")
    p.add_argument("--areas", help="lista separada por comas (por defecto todas)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--org", type=int, help="también verifica las conexiones de esta empresa")
    g.add_argument("--all-orgs", action="store_true", help="también verifica las conexiones de todas las empresas")
    p.add_argument("--json", action="store_true", help="salida JSON")
    p.add_argument("--with-llm", action="store_true", help="hace una llamada mínima a cada conexión de IA (cuesta)")
    p.add_argument("--no-save", action="store_true", help="no guarda los resultados en la base")
    p.add_argument("--trigger", default="cli", choices=["cli", "deploy", "schedule"])
    return p.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Organization
    from app.preflight.core import AREAS, Context, execute, exit_code, run_and_save

    areas = [a.strip() for a in args.areas.split(",")] if args.areas else None
    unknown = [a for a in areas or [] if a not in AREAS]
    if unknown:
        print(f"Áreas desconocidas: {', '.join(unknown)}. Válidas: {', '.join(AREAS)}", file=sys.stderr)
        return 2
    orgs: list[int] = []
    if args.org:
        orgs = [args.org]
    elif args.all_orgs:
        async with SessionLocal() as s:
            orgs = list((await s.scalars(select(Organization.id).order_by(Organization.id))).all())

    sections = []
    scopes = [(None, "platform")] + [(o, "org") for o in orgs]
    for org, scope in scopes:
        if args.no_save:
            results = await execute(Context(organization_id=org, with_llm=args.with_llm), scope, areas)
        else:
            _run, results = await run_and_save(org, scope, areas, trigger=args.trigger, with_llm=args.with_llm)
        sections.append((org, results))

    every = [r for _, rs in sections for r in rs]
    if args.json:
        print(json.dumps({"exit_code": exit_code(every), "sections": [
            {"organization_id": org, "results": [r.out() for r in rs]} for org, rs in sections]},
            ensure_ascii=False, indent=2, default=str))
    else:
        for org, rs in sections:
            print(f"\n== {'Plataforma' if org is None else f'Empresa {org}'} ==")
            for r in sorted(rs, key=lambda x: (x.area, x.check_key)):
                print(f" {ICONS[r.status]} [{r.area}] {r.label}: {r.detail or ''}")
        c = {s: sum(1 for r in every if r.status == s) for s in ICONS}
        print(f"\n{c['pass']} correctas · {c['warn']} advertencias · {c['fail']} fallas · {c['skipped']} omitidas")
    return exit_code(every)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(_parse(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    sys.exit(main())
