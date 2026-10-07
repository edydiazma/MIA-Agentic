"""El ORM debe ser espejo exacto de las migraciones: misma tabla, mismas columnas, misma nulabilidad."""

from sqlalchemy import text

from app.db import Base, engine


async def test_orm_matches_migrations():
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "select table_name, column_name, is_nullable = 'YES' from information_schema.columns "
            "where table_schema = 'public'"))).all()
    db: dict[str, dict[str, bool]] = {}
    for table, column, nullable in rows:
        db.setdefault(table, {})[column] = nullable

    problems = []
    for table in Base.metadata.tables.values():
        cols = db.get(table.name)
        if cols is None:
            problems.append(f"{table.name}: no existe en la base")
            continue
        orm_cols = {c.name: c for c in table.columns}
        for name in orm_cols.keys() - cols.keys():
            problems.append(f"{table.name}.{name}: está en el ORM y no en la base")
        # Columnas de la base que el ORM ignora (solo se permiten las generadas)
        for name in cols.keys() - orm_cols.keys() - {"search", "chars", "fts"}:
            problems.append(f"{table.name}.{name}: está en la base y no en el ORM")
        for name, col in orm_cols.items():
            if name in cols and not col.primary_key and col.nullable != cols[name]:
                problems.append(f"{table.name}.{name}: nulabilidad distinta (orm={col.nullable}, db={cols[name]})")
    assert not problems, "\n".join(problems)
