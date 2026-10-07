"""Segmentos dinámicos (docs/data-model.md §21.1).

Una regla es un árbol `{"all": [...]} | {"any": [...]} | {"not": regla}` con hojas `{"field", "op", "value"}`.
Se compila a UNA consulta SQL parametrizada sobre `contacts k` de la empresa: los identificadores salen solo del
catálogo `FIELDS` (nunca del usuario) y todos los valores van como parámetros enlazados, así que una regla no
puede inyectar SQL. Los campos de otras tablas (vehículos, negocios, productos, pedidos, consentimientos,
campos personalizados) se evalúan con EXISTS / subconsultas escalares por contacto.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from datetime import date, datetime

import jsonschema
from sqlalchemy import bindparam, delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, ContactField, Segment, SegmentMember, utcnow

MAX_RULES = 60
MAX_DEPTH = 6

# --- Catálogo de campos -----------------------------------------------------------------------------------------
# kind="col": expresión sobre la fila del contacto (k). kind="exists": subconsulta con {cond} sobre la columna `col`.
# dtype: text | number | date (columna date) | ts (timestamptz) | bool | enum | array (text[])


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    dtype: str
    sql: str  # col: expresión; exists: plantilla con {cond}
    kind: str = "col"
    col: str = ""  # exists: columna comparada dentro de la subconsulta
    options: tuple[str, ...] = ()


FIELDS: dict[str, Field] = {f.key: f for f in [
    # Cliente
    Field("contact.stage", "Etapa", "Cliente", "enum", "k.stage", options=("lead", "prospect", "client", "lost")),
    Field("contact.name", "Nombre", "Cliente", "text", "k.name"),
    Field("contact.email", "Correo", "Cliente", "text", "k.email"),
    Field("contact.created_at", "Fecha de creación", "Cliente", "ts", "k.created_at"),
    Field("contact.last_interaction_at", "Última interacción", "Cliente", "ts", "k.last_interaction_at"),
    Field("contact.days_since_last_interaction", "Días sin interacción", "Cliente", "number",
          "(extract(epoch from now() - k.last_interaction_at) / 86400.0)"),
    Field("contact.conversations_count", "Conversaciones", "Cliente", "number", "k.conversations_count"),
    Field("contact.products_count", "Productos de interés", "Cliente", "number", "k.products_count"),
    Field("contact.owner_agent_id", "Dueño (asesor)", "Cliente", "number", "k.owner_agent_id"),
    Field("contact.channels", "Canales", "Cliente", "array", "k.channel_providers",
          options=("whatsapp_cloud", "messenger", "instagram", "webchat", "email")),
    Field("contact.marketing_opt_out", "No acepta marketing (opt-out)", "Cliente", "bool", "k.marketing_opt_out"),
    Field("contact.tag", "Etiqueta", "Cliente", "text",
          "exists (select 1 from public.contact_tags ct join public.tags t on t.id = ct.tag_id "
          "where ct.contact_id = k.id and {cond})", kind="exists", col="t.name"),
    # Fuente
    Field("source.first_channel", "Canal de origen", "Fuente", "text", "k.first_source_channel"),
    Field("source.first_campaign", "Campaña de origen", "Fuente", "text", "k.first_source_campaign"),
    Field("source.first_ad_id", "Anuncio de origen", "Fuente", "text", "k.first_source_ad_id"),
    Field("source.last_channel", "Última fuente (canal)", "Fuente", "text", "k.last_source_channel"),
    Field("source.last_campaign", "Última campaña", "Fuente", "text", "k.last_source_campaign"),
    # Registro maestro
    Field("golden.has_document", "Tiene documento", "Datos maestros", "bool",
          "exists (select 1 from public.contact_golden g where g.contact_id = k.id and g.document_number is not null)"),
    Field("golden.has_email", "Tiene correo", "Datos maestros", "bool",
          "exists (select 1 from public.contact_golden g where g.contact_id = k.id and g.primary_email is not null)"),
    Field("golden.has_birthdate", "Tiene fecha de nacimiento", "Datos maestros", "bool",
          "exists (select 1 from public.contact_golden g where g.contact_id = k.id and g.birthdate is not null)"),
    Field("golden.birthday_this_month", "Cumple años este mes", "Datos maestros", "bool",
          "exists (select 1 from public.contact_golden g where g.contact_id = k.id "
          "and extract(month from g.birthdate) = extract(month from current_date))"),
    Field("golden.completeness_pct", "Completitud del registro (%)", "Datos maestros", "number",
          "coalesce((select g.completeness_pct from public.contact_golden g where g.contact_id = k.id), 0)"),
    # Vehículos
    Field("vehicle.make", "Marca del vehículo", "Vehículos", "text",
          "exists (select 1 from public.contact_vehicles v where v.contact_id = k.id and v.status = 'active' "
          "and {cond})", kind="exists", col="v.make"),
    Field("vehicle.model", "Modelo del vehículo", "Vehículos", "text",
          "exists (select 1 from public.contact_vehicles v where v.contact_id = k.id and v.status = 'active' "
          "and {cond})", kind="exists", col="v.model"),
    Field("vehicle.year", "Año del vehículo", "Vehículos", "number",
          "exists (select 1 from public.contact_vehicles v where v.contact_id = k.id and v.status = 'active' "
          "and {cond})", kind="exists", col="v.year"),
    Field("vehicle.insurance_due", "Vence el SOAT / seguro", "Vehículos", "date",
          "exists (select 1 from public.contact_vehicles v where v.contact_id = k.id and v.status = 'active' "
          "and {cond})", kind="exists", col="v.insurance_due"),
    Field("vehicle.inspection_due", "Vence la revisión técnico-mecánica", "Vehículos", "date",
          "exists (select 1 from public.contact_vehicles v where v.contact_id = k.id and v.status = 'active' "
          "and {cond})", kind="exists", col="v.inspection_due"),
    Field("vehicle.next_service_at", "Próximo mantenimiento", "Vehículos", "date",
          "exists (select 1 from public.contact_vehicles v where v.contact_id = k.id and v.status = 'active' "
          "and {cond})", kind="exists", col="v.next_service_at"),
    # Consentimientos (último registro vigente por tipo)
    Field("consent.marketing", "Aceptó marketing", "Consentimientos", "bool",
          "coalesce((select c.granted from public.contact_consents c where c.contact_id = k.id "
          "and c.consent_type = 'marketing' and c.revoked_at is null order by c.recorded_at desc limit 1), false)"),
    Field("consent.habeas_data", "Aceptó habeas data", "Consentimientos", "bool",
          "coalesce((select c.granted from public.contact_consents c where c.contact_id = k.id "
          "and c.consent_type = 'habeas_data' and c.revoked_at is null order by c.recorded_at desc limit 1), false)"),
    # Negocios
    Field("deal.open_pipeline", "Negocio abierto en la línea", "Negocios", "text",
          "exists (select 1 from public.deals d where d.contact_id = k.id and d.status = 'open' and {cond})",
          kind="exists", col="d.pipeline"),
    Field("deal.open_stage", "Negocio abierto en la etapa", "Negocios", "text",
          "exists (select 1 from public.deals d where d.contact_id = k.id and d.status = 'open' and {cond})",
          kind="exists", col="d.stage"),
    Field("deal.won_at", "Negocio ganado (fecha)", "Negocios", "ts",
          "exists (select 1 from public.deals d where d.contact_id = k.id and d.status = 'won' and {cond})",
          kind="exists", col="d.closed_at"),
    # Productos
    Field("product.interested", "Interesado en producto", "Productos", "text",
          "exists (select 1 from public.interaction_products p where p.contact_id = k.id "
          "and p.stage in ('mentioned', 'interested', 'quoted') and {cond})", kind="exists", col="p.name"),
    Field("product.purchased", "Compró producto", "Productos", "text",
          "exists (select 1 from public.interaction_products p where p.contact_id = k.id "
          "and p.stage = 'purchased' and {cond})", kind="exists", col="p.name"),
    # Pedidos de tiendas / ERP
    Field("order.paid_at", "Pedido pagado (fecha)", "Pedidos", "ts",
          "exists (select 1 from public.external_orders o where o.contact_id = k.id "
          "and o.status in ('paid', 'fulfilled') and {cond})", kind="exists", col="o.placed_at"),
    Field("order.total", "Total de un pedido pagado", "Pedidos", "number",
          "exists (select 1 from public.external_orders o where o.contact_id = k.id "
          "and o.status in ('paid', 'fulfilled') and {cond})", kind="exists", col="o.total"),
]}

OPS_BY_TYPE = {
    "text": ("eq", "neq", "in", "contains", "exists"),
    "enum": ("eq", "neq", "in"),
    "number": ("eq", "neq", "gte", "lte", "between", "exists"),
    "date": ("gte", "lte", "between", "within_days", "before_days", "exists"),
    "ts": ("gte", "lte", "between", "within_days", "before_days", "exists"),
    "bool": ("eq", "exists"),
    "array": ("eq", "contains", "in"),
}
OP_LABELS = {"eq": "es igual a", "neq": "es distinto de", "in": "es uno de", "contains": "contiene",
             "gte": "es mayor o igual a", "lte": "es menor o igual a", "between": "está entre",
             "within_days": "en los próximos N días", "before_days": "en los últimos N días", "exists": "tiene valor"}
CUSTOM_RE = re.compile(r"^custom:([a-z][a-z0-9_]{0,49})$")
CUSTOM_TYPES = {"text": "text", "long_text": "text", "email": "text", "phone": "text", "select": "text",
                "number": "number", "currency": "number", "date": "date", "datetime": "ts", "boolean": "bool"}
CUSTOM_COLUMN = {"text": "fv.value_text", "number": "fv.value_number", "date": "fv.value_date",
                 "ts": "fv.value_date", "bool": "fv.value_bool"}

RULE_SCHEMA = {
    "$defs": {
        "node": {"oneOf": [
            {"type": "object", "required": ["all"], "additionalProperties": False,
             "properties": {"all": {"type": "array", "items": {"$ref": "#/$defs/node"}}}},
            {"type": "object", "required": ["any"], "additionalProperties": False,
             "properties": {"any": {"type": "array", "items": {"$ref": "#/$defs/node"}}}},
            {"type": "object", "required": ["not"], "additionalProperties": False,
             "properties": {"not": {"$ref": "#/$defs/node"}}},
            {"type": "object", "required": ["field", "op"], "additionalProperties": False,
             "properties": {"field": {"type": "string", "maxLength": 80}, "op": {"type": "string"}, "value": {}}},
        ]},
    },
    "$ref": "#/$defs/node",
}


class SegmentError(ValueError):
    pass


async def custom_fields(session: AsyncSession, org: int) -> dict[str, str]:
    """custom:<key> → tipo del catálogo (text|number|date|ts|bool)."""
    rows = (await session.execute(select(ContactField.key, ContactField.type).where(
        ContactField.organization_id == org, ContactField.archived_at.is_(None)))).all()
    return {f"custom:{k}": CUSTOM_TYPES.get(t, "text") for k, t in rows}


async def field_catalog(session: AsyncSession, org: int) -> list[dict]:
    out = [{"key": f.key, "label": f.label, "group": f.group, "type": f.dtype, "ops": list(OPS_BY_TYPE[f.dtype]),
            "options": list(f.options)} for f in FIELDS.values()]
    labels = dict((await session.execute(select(ContactField.key, ContactField.label).where(
        ContactField.organization_id == org, ContactField.archived_at.is_(None)))).all())
    for key, dtype in (await custom_fields(session, org)).items():
        out.append({"key": key, "label": labels.get(key.split(":", 1)[1], key), "group": "Campos personalizados",
                    "type": dtype, "ops": list(OPS_BY_TYPE[dtype]), "options": []})
    return out


# --- Compilación ------------------------------------------------------------------------------------------------
class _Compiler:
    def __init__(self, org: int, customs: dict[str, str]):
        self.params: dict = {"org": org}
        self.expanding: set[str] = set()
        self.customs = customs
        self._n = itertools.count(1)
        self.rules = 0

    def p(self, value, expanding: bool = False) -> str:
        name = f"p{next(self._n)}"
        self.params[name] = value
        if expanding:
            self.expanding.add(name)
        return f":{name}"

    def node(self, n: dict, depth: int = 0) -> str:
        if depth > MAX_DEPTH:
            raise SegmentError("La regla tiene demasiados niveles")
        if "all" in n or "any" in n:
            items = n.get("all", n.get("any")) or []
            if not items:
                return "true"
            parts = [self.node(i, depth + 1) for i in items]
            return "(" + (" and " if "all" in n else " or ").join(parts) + ")"
        if "not" in n:
            return f"(not {self.node(n['not'], depth + 1)})"
        self.rules += 1
        if self.rules > MAX_RULES:
            raise SegmentError(f"Máximo {MAX_RULES} condiciones por segmento")
        return self.leaf(n)

    def leaf(self, n: dict) -> str:
        key, op, value = n["field"], n["op"], n.get("value")
        m = CUSTOM_RE.match(key)
        if m:
            dtype = self.customs.get(key)
            if dtype is None:
                raise SegmentError(f"Campo personalizado desconocido: {key}")
            col = CUSTOM_COLUMN[dtype]
            self._check(key, dtype, op, value)
            k = self.p(m.group(1))
            tpl = ("exists (select 1 from public.contact_field_values fv join public.contact_fields cf "
                   f"on cf.id = fv.field_id where fv.contact_id = k.id and cf.organization_id = :org and cf.key = {k} "
                   "and {cond})")
            return tpl.replace("{cond}", "true" if op == "exists" else self.cmp(col, dtype, op, value))
        f = FIELDS.get(key)
        if f is None:
            raise SegmentError(f"Campo desconocido: {key}")
        self._check(key, f.dtype, op, value)
        if f.kind == "exists":
            return f.sql.replace("{cond}", "true" if op == "exists" else self.cmp(f.col, f.dtype, op, value))
        if op == "exists":
            return f"({f.sql} is not null)"
        return self.cmp(f.sql, f.dtype, op, value)

    @staticmethod
    def _check(key: str, dtype: str, op: str, value) -> None:
        if op not in OPS_BY_TYPE[dtype]:
            raise SegmentError(f"El operador «{op}» no aplica a {key}")
        if op == "exists":
            return
        if op == "in" and not (isinstance(value, list) and value and len(value) <= 500):
            raise SegmentError(f"{key}: «es uno de» necesita una lista (1 a 500 valores)")
        if op == "between" and not (isinstance(value, list) and len(value) == 2):
            raise SegmentError(f"{key}: «está entre» necesita [desde, hasta]")
        if op in ("within_days", "before_days"):
            if not isinstance(value, int | float) or isinstance(value, bool) or not 0 <= value <= 3650:
                raise SegmentError(f"{key}: indica un número de días entre 0 y 3650")
        if value is None and op not in ("exists",):
            raise SegmentError(f"{key}: falta el valor")

    def _typed(self, dtype: str, value):
        """Convierte el valor al tipo de la columna (asyncpg exige tipos exactos)."""
        try:
            if dtype == "number":
                return float(value)
            if dtype == "bool":
                if isinstance(value, bool):
                    return value
                return str(value).lower() in ("true", "1", "si", "sí", "yes")
            if dtype == "date":
                return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
            if dtype == "ts":
                if isinstance(value, datetime):
                    return value
                v = str(value)
                return datetime.fromisoformat(v if "T" in v or " " in v else v + "T00:00:00+00:00")
            return str(value)
        except (TypeError, ValueError) as e:
            raise SegmentError(f"Valor inválido «{value}»") from e

    def cmp(self, col: str, dtype: str, op: str, value) -> str:
        if dtype == "array":
            if op in ("eq", "contains"):
                return f"({self.p(str(value))} = any({col}))"
            return f"({col} && cast({self.p([str(v) for v in value])} as text[]))"
        if op == "eq":
            return f"({col} = {self.p(self._typed(dtype, value))})"
        if op == "neq":
            return f"({col} is distinct from {self.p(self._typed(dtype, value))})"
        if op == "in":
            return f"({col} in {self.p([self._typed(dtype, v) for v in value], expanding=True)})"
        if op == "contains":
            esc = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            return f"({col} ilike {self.p('%' + esc + '%')})"
        if op == "gte":
            return f"({col} >= {self.p(self._typed(dtype, value))})"
        if op == "lte":
            return f"({col} <= {self.p(self._typed(dtype, value))})"
        if op == "between":
            return f"({col} between {self.p(self._typed(dtype, value[0]))} and {self.p(self._typed(dtype, value[1]))})"
        days = int(value)
        if dtype == "date":
            if op == "within_days":
                return f"({col} between current_date and current_date + cast({self.p(days)} as integer))"
            return f"({col} between current_date - cast({self.p(days)} as integer) and current_date)"
        if op == "within_days":
            return f"({col} between now() and now() + make_interval(days => {self.p(days)}))"
        return f"({col} between now() - make_interval(days => {self.p(days)}) and now())"


async def compile_definition(session: AsyncSession, org: int, definition: dict) -> tuple[str, dict, set[str]]:
    """(condición SQL sobre k, parámetros, nombres expandibles). Lanza SegmentError si la regla es inválida."""
    validate_definition(definition)
    comp = _Compiler(org, await custom_fields(session, org))
    where = comp.node(definition) if definition else "true"
    return where, comp.params, comp.expanding


def validate_definition(definition: dict) -> None:
    if not definition:
        return
    try:
        jsonschema.validate(definition, RULE_SCHEMA)
    except jsonschema.ValidationError as e:
        raise SegmentError(f"Regla inválida: {e.message}") from e


def _stmt(where: str, params: dict, expanding: set[str], select_sql: str, tail: str = ""):
    sql = (f"select {select_sql} from public.contacts k where k.organization_id = :org and not k.blocked "
           f"and ({where}) {tail}")
    stmt = text(sql)
    if expanding:
        stmt = stmt.bindparams(*[bindparam(n, expanding=True) for n in expanding])
    return stmt.bindparams(**{k: v for k, v in params.items() if k not in expanding}), \
        {k: v for k, v in params.items() if k in expanding}


async def matching_ids(session: AsyncSession, org: int, definition: dict, extra_where: str = "",
                       extra_params: dict | None = None) -> list[int]:
    where, params, expanding = await compile_definition(session, org, definition)
    if extra_where:
        where = f"({where}) and ({extra_where})"
        params.update(extra_params or {})
    stmt, exp = _stmt(where, params, expanding, "k.id", "order by k.id")
    return [r[0] for r in (await session.execute(stmt, exp)).all()]


async def segment_contact_ids(session: AsyncSession, segment: Segment) -> list[int]:
    if segment.kind == "static":
        return list((await session.scalars(select(SegmentMember.contact_id).where(
            SegmentMember.segment_id == segment.id).order_by(SegmentMember.contact_id))).all())
    return await matching_ids(session, segment.organization_id, segment.definition or {})


async def preview(session: AsyncSession, segment_or_def: Segment | dict, org: int, limit: int = 20) -> dict:
    if isinstance(segment_or_def, Segment) and segment_or_def.kind == "static":
        ids = await segment_contact_ids(session, segment_or_def)
    else:
        definition = segment_or_def.definition if isinstance(segment_or_def, Segment) else segment_or_def
        ids = await matching_ids(session, org, definition or {})
    sample = []
    if ids:
        rows = (await session.scalars(select(Contact).where(Contact.id.in_(ids[:limit])).order_by(Contact.id))).all()
        sample = [{"id": c.id, "name": c.name, "wa_id": c.wa_id, "wa_username": c.wa_username, "email": c.email,
                   "stage": c.stage, "last_interaction_at": c.last_interaction_at} for c in rows]
    return {"count": len(ids), "sample": sample}


async def refresh(session: AsyncSession, segment: Segment) -> dict:
    """Materializa los miembros: devuelve los que entraron y los que salieron desde la última evaluación."""
    if segment.kind == "static":
        segment.member_count = await session.scalar(select(func.count()).where(
            SegmentMember.segment_id == segment.id)) or 0
        segment.last_computed_at = utcnow()
        await session.commit()
        return {"entered": [], "left": [], "count": segment.member_count}
    now_ids = set(await segment_contact_ids(session, segment))
    old_ids = set((await session.scalars(select(SegmentMember.contact_id).where(
        SegmentMember.segment_id == segment.id))).all())
    entered, left = sorted(now_ids - old_ids), sorted(old_ids - now_ids)
    if left:
        await session.execute(delete(SegmentMember).where(SegmentMember.segment_id == segment.id,
                                                           SegmentMember.contact_id.in_(left)))
    if entered:
        await session.execute(insert(SegmentMember), [{"segment_id": segment.id, "contact_id": c} for c in entered])
    segment.member_count, segment.last_computed_at = len(now_ids), utcnow()
    await session.commit()
    return {"entered": entered, "left": left, "count": len(now_ids)}
