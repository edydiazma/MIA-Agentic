"""Base de datos de pruebas: Postgres local creado desde supabase/migrations (+ stubs de Supabase y seed).

- Si TEST_DATABASE_URL está definido, se usa tal cual (p. ej. CI con un Postgres de servicio).
- Si no, levanta un clúster local en backend/.pgtest (puerto 55433) con los binarios de PGB
  (por defecto Homebrew postgresql@17) y recrea la base `wa_test` en cada sesión de pruebas.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUPABASE = ROOT / "supabase"
PGB = Path(os.environ.get("PGB", "/opt/homebrew/opt/postgresql@17/bin"))
PORT = int(os.environ.get("TEST_PG_PORT", "55433"))
DATA = ROOT / "backend" / ".pgtest"
# Una base por proceso de pruebas: varias ejecuciones en paralelo no se pisan.
DB = os.environ.get("TEST_PG_DB", f"wa_test_{os.getpid()}")


def _run(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, capture_output=True, text=True, **kw)


def _psql_file(path: Path) -> None:
    env = {**os.environ, "PGOPTIONS": "-c search_path=public,extensions"}
    r = subprocess.run([str(PGB / "psql"), "-h", "localhost", "-p", str(PORT), "-U", "postgres", "-d", DB,
                        "-v", "ON_ERROR_STOP=1", "-q", "-f", str(path)], capture_output=True, text=True, env=env)
    if r.returncode:
        raise RuntimeError(f"Falló {path.name}:\n{r.stderr[-2000:]}")


def ensure_test_database() -> str:
    if os.environ.get("TEST_DATABASE_URL"):
        return os.environ["TEST_DATABASE_URL"]
    ready = subprocess.run([str(PGB / "pg_isready"), "-h", "localhost", "-p", str(PORT)], capture_output=True)
    if ready.returncode != 0:
        if not (DATA / "PG_VERSION").exists():
            _run(str(PGB / "initdb"), "-D", str(DATA), "-U", "postgres", "-A", "trust")
        _run(str(PGB / "pg_ctl"), "-D", str(DATA), "-o", f"-p {PORT} -k '' -c listen_addresses=localhost",
             "-l", str(DATA / "log.txt"), "-w", "start")
    base = ["-h", "localhost", "-p", str(PORT), "-U", "postgres"]
    _run(str(PGB / "dropdb"), *base, "--if-exists", "--force", DB)
    _run(str(PGB / "createdb"), *base, DB)
    _psql_file(SUPABASE / "tests" / "supabase_stubs.sql")
    for f in sorted((SUPABASE / "migrations").glob("*.sql")):
        _psql_file(f)
    _psql_file(SUPABASE / "seed.sql")
    # Varias pruebas crean empresas con ids fijos (8, 9, 9101…): los ids automáticos arrancan lejos de ellos
    _run(str(PGB / "psql"), *base, "-d", DB, "-qc",
         "select setval(pg_get_serial_sequence('public.organizations', 'id'), 100000)")
    return f"postgresql+asyncpg://postgres@localhost:{PORT}/{DB}"


def drop_test_database() -> None:
    if os.environ.get("TEST_DATABASE_URL"):
        return
    subprocess.run([str(PGB / "dropdb"), "-h", "localhost", "-p", str(PORT), "-U", "postgres", "--if-exists",
                    "--force", DB], capture_output=True)
