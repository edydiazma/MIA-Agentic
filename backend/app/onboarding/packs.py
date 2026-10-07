"""Paquetes por industria: plantillas de WhatsApp (español, listas para aprobación) y valores iniciales
(tipificaciones, grupos, tono). Ver docs/data-model.md §13.

Reglas de Meta que respetan las plantillas:
- Variables posicionales {{1}}..{{n}} con ejemplo cada una; el cuerpo no empieza ni termina con una variable.
- UTILITY solo para mensajes sobre una solicitud, cita, pedido o servicio del cliente (sin promociones).
- MARKETING para novedades y reactivación, con botón para dejar de recibir.
- Nombres en snake_case (únicos por WABA); idioma "es".
"""

import copy
import re

LANGUAGE = "es"
INDUSTRIES = ("automotriz", "salud", "educacion", "retail", "servicios", "inmobiliaria", "otro")
INDUSTRY_LABELS = {"automotriz": "Automotriz", "salud": "Salud", "educacion": "Educación", "retail": "Comercio / retail",
                   "servicios": "Servicios", "inmobiliaria": "Inmobiliaria", "otro": "Otro"}

OPT_OUT = {"type": "QUICK_REPLY", "text": "No recibir más"}


def _t(key: str, name: str, category: str, body: str, examples: list[str], buttons: list[dict] | None = None,
       header: str | None = None, footer: str | None = None, recommended: bool = True) -> dict:
    return {"template_key": key, "name": name, "category": category, "language": LANGUAGE, "body": body,
            "example_values": examples, "buttons": buttons or [], "header": header, "footer": footer,
            "recommended": recommended}


COMMON = [
    _t("solicitud_recibida", "solicitud_recibida", "UTILITY",
       "Hola {{1}}, recibimos tu solicitud en {company}. Un asesor te responderá por este medio en breve. "
       "Tu número de referencia es {{2}} y te sirve para cualquier consulta.",
       ["Ana", "A-1024"]),
    _t("recordatorio_cita", "recordatorio_cita", "UTILITY",
       "Hola {{1}}, te recordamos tu cita en {company} el {{2}} a las {{3}}. "
       "Si necesitas reprogramarla, responde a este mensaje.",
       ["Ana", "15 de octubre", "10:00 a. m."],
       [{"type": "QUICK_REPLY", "text": "Confirmar"}, {"type": "QUICK_REPLY", "text": "Reprogramar"}]),
    _t("retomar_conversacion", "retomar_conversacion", "UTILITY",
       "Hola {{1}}, te escribimos de {company} para continuar con tu consulta sobre {{2}}. "
       "Responde a este mensaje y seguimos con tu solicitud.",
       ["Ana", "tu cotización"]),
    _t("actualizacion_servicio", "actualizacion_servicio", "UTILITY",
       "Hola {{1}}, hay una actualización de tu {{2}}: {{3}}. Si tienes dudas, responde a este mensaje.",
       ["Ana", "pedido 1024", "está listo para entrega"]),
    _t("novedades", "novedades", "MARKETING",
       "Hola {{1}}, en {company} tenemos novedades que pueden interesarte: {{2}}. ¿Quieres que te contemos más?",
       ["Ana", "nuevos horarios y beneficios para clientes"],
       [{"type": "QUICK_REPLY", "text": "Me interesa"}, OPT_OUT], recommended=False),
]

BY_INDUSTRY: dict[str, list[dict]] = {
    "automotriz": [
        _t("cotizacion_vehiculo", "cotizacion_vehiculo", "UTILITY",
           "Hola {{1}}, esta es la cotización del {{2}} que solicitaste en {company}: {{3}}. "
           "Responde a este mensaje si quieres agendar una prueba de manejo.",
           ["Ana", "Onix LTZ 2026", "precio de lista 89.900.000 COP"]),
        _t("vehiculo_listo_taller", "vehiculo_listo_taller", "UTILITY",
           "Hola {{1}}, tu vehículo de placa {{2}} ya está listo en el taller de {company}. "
           "Puedes recogerlo en nuestro horario de atención.",
           ["Ana", "ABC123"]),
    ],
    "salud": [
        _t("resultados_disponibles", "resultados_disponibles", "UTILITY",
           "Hola {{1}}, tus resultados de {{2}} ya están disponibles en {company}. "
           "Responde a este mensaje para saber cómo recibirlos.",
           ["Ana", "laboratorio"]),
    ],
    "educacion": [
        _t("inscripcion_recibida", "inscripcion_recibida", "UTILITY",
           "Hola {{1}}, recibimos tu inscripción al programa {{2}} en {company}. "
           "Te escribiremos por este medio con los siguientes pasos.",
           ["Ana", "Diseño UX"]),
        _t("recordatorio_clase", "recordatorio_clase", "UTILITY",
           "Hola {{1}}, te recordamos tu clase de {{2}} el {{3}}. ¡Te esperamos en {company}!",
           ["Ana", "Inglés B1", "lunes a las 6:00 p. m."]),
    ],
    "retail": [
        _t("pedido_confirmado", "pedido_confirmado", "UTILITY",
           "Hola {{1}}, confirmamos tu pedido {{2}} en {company} por {{3}}. Te avisaremos cuando sea enviado.",
           ["Ana", "#1024", "120.000 COP"]),
        _t("pedido_enviado", "pedido_enviado", "UTILITY",
           "Hola {{1}}, tu pedido {{2}} va en camino con la guía {{3}}. Gracias por comprar en {company}.",
           ["Ana", "#1024", "GU-55821"]),
        _t("carrito_pendiente", "carrito_pendiente", "MARKETING",
           "Hola {{1}}, dejaste {{2}} en tu carrito de {company}. ¿Te ayudamos a terminar tu compra?",
           ["Ana", "2 productos"], [{"type": "QUICK_REPLY", "text": "Quiero comprar"}, OPT_OUT], recommended=False),
    ],
    "servicios": [
        _t("cotizacion_enviada", "cotizacion_enviada", "UTILITY",
           "Hola {{1}}, te compartimos la cotización de {{2}} que solicitaste a {company}: {{3}}. "
           "Responde a este mensaje si tienes preguntas.",
           ["Ana", "mantenimiento mensual", "350.000 COP al mes"]),
        _t("visita_programada", "visita_programada", "UTILITY",
           "Hola {{1}}, tu visita técnica de {company} quedó programada para el {{2}}. "
           "Responde a este mensaje si necesitas cambiarla.",
           ["Ana", "jueves 16 de octubre a las 9:00 a. m."]),
    ],
    "inmobiliaria": [
        _t("visita_inmueble", "visita_inmueble", "UTILITY",
           "Hola {{1}}, confirmamos tu visita al inmueble {{2}} el {{3}}. Te espera un asesor de {company}.",
           ["Ana", "Apartamento 302, Chicó", "sábado a las 11:00 a. m."],
           [{"type": "QUICK_REPLY", "text": "Confirmar"}, {"type": "QUICK_REPLY", "text": "Reprogramar"}]),
        _t("nuevos_inmuebles", "nuevos_inmuebles", "MARKETING",
           "Hola {{1}}, en {company} tenemos nuevos inmuebles en {{2}} que coinciden con lo que buscas. "
           "¿Quieres recibir las opciones?",
           ["Ana", "el norte de la ciudad"], [{"type": "QUICK_REPLY", "text": "Quiero verlos"}, OPT_OUT],
           recommended=False),
    ],
    "otro": [],
}

PRESETS: dict[str, dict] = {
    "automotriz": {
        "typifications": ["Venta", "Cotización enviada", "Prueba de manejo agendada", "Servicio de taller",
                          "Consulta resuelta", "Reclamo", "Sin respuesta", "Spam"],
        "groups": [("Ventas", "Compra de vehículos nuevos o usados, cotizaciones, financiación y pruebas de manejo"),
                   ("Posventa", "Taller, repuestos, garantías y citas de servicio")],
    },
    "salud": {
        "typifications": ["Cita agendada", "Consulta resuelta", "Resultados entregados", "Reclamo", "Sin respuesta",
                          "Spam"],
        "groups": [("Citas", "Agendar, cambiar o cancelar citas"), ("Atención al paciente", "Resultados, órdenes y dudas")],
    },
    "educacion": {
        "typifications": ["Inscripción", "Información enviada", "Consulta resuelta", "Reclamo", "Sin respuesta", "Spam"],
        "groups": [("Admisiones", "Programas, precios, becas e inscripciones"),
                   ("Estudiantes", "Clases, certificados y soporte académico")],
    },
    "retail": {
        "typifications": ["Venta", "Pedido gestionado", "Cambio o devolución", "Consulta resuelta", "Reclamo",
                          "Sin respuesta", "Spam"],
        "groups": [("Ventas", "Productos, precios, disponibilidad y compras"),
                   ("Servicio al cliente", "Pedidos, envíos, cambios y devoluciones")],
    },
    "servicios": {
        "typifications": ["Venta", "Cotización enviada", "Visita agendada", "Consulta resuelta", "Reclamo",
                          "Sin respuesta", "Spam"],
        "groups": [("Comercial", "Cotizaciones y contratación"), ("Soporte", "Clientes actuales y visitas técnicas")],
    },
    "inmobiliaria": {
        "typifications": ["Visita agendada", "Negocio cerrado", "Información enviada", "Consulta resuelta",
                          "Sin respuesta", "Spam"],
        "groups": [("Ventas", "Compra de inmuebles"), ("Arriendos", "Arrendamiento y administración")],
    },
    "otro": {
        "typifications": ["Venta", "Consulta resuelta", "Cotización enviada", "Reclamo", "Sin respuesta", "Spam"],
        "groups": [("Atención", "Consultas generales")],
    },
}


def industry_or_default(industry: str | None) -> str:
    return industry if industry in INDUSTRIES else "otro"


def pack_key(industry: str | None) -> str:
    return f"{industry_or_default(industry)}.basico"


def _company(answers: dict) -> str:
    profile = answers.get("company") or {}
    name = (profile.get("name") or "").strip()
    return name[:60] or "nuestra empresa"


def pack(industry: str | None, answers: dict | None = None) -> dict:
    """Paquete personalizado con el nombre de la empresa (texto fijo, no variable)."""
    industry = industry_or_default(industry)
    company = _company(answers or {})
    templates = []
    for t in BY_INDUSTRY.get(industry, []) + COMMON:
        t = copy.deepcopy(t)
        t["body"] = t["body"].replace("{company}", company)
        templates.append(t)
    return {"pack_key": pack_key(industry), "templates": templates}


VAR = re.compile(r"\{\{(\d+)\}\}")


def validate(t: dict) -> str | None:
    """Errores que Meta rechazaría antes de enviar (texto en español)."""
    body = (t.get("body") or "").strip()
    if not body or len(body) > 1024:
        return "El cuerpo debe tener entre 1 y 1024 caracteres"
    if re.match(r"^\{\{\d+\}\}", body) or re.search(r"\{\{\d+\}\}[.!?]?$", body):
        return "El cuerpo no puede empezar ni terminar con una variable"
    nums = [int(n) for n in VAR.findall(body)]
    if nums and sorted(set(nums)) != list(range(1, max(nums) + 1)):
        return "Las variables deben ser {{1}}, {{2}}… en orden y sin saltos"
    if len(t.get("example_values") or []) < len(set(nums)):
        return "Falta un ejemplo para cada variable"
    if t.get("header") and len(t["header"]) > 60:
        return "El encabezado admite máximo 60 caracteres"
    if t.get("footer") and len(t["footer"]) > 60:
        return "El pie admite máximo 60 caracteres"
    if not re.match(r"^[a-z0-9_]{1,512}$", t.get("name") or ""):
        return "El nombre solo admite minúsculas, números y guion bajo"
    return None


def to_components(t: dict) -> list[dict]:
    comps: list[dict] = []
    if t.get("header"):
        comps.append({"type": "HEADER", "format": "TEXT", "text": t["header"]})
    body = {"type": "BODY", "text": t["body"]}
    n = len(set(VAR.findall(t["body"])))
    if n:
        body["example"] = {"body_text": [list(t["example_values"][:n])]}
    comps.append(body)
    if t.get("footer"):
        comps.append({"type": "FOOTER", "text": t["footer"]})
    if t.get("buttons"):
        buttons = []
        for b in t["buttons"]:
            if b.get("type") == "URL":
                buttons.append({"type": "URL", "text": b["text"], "url": b["url"]})
            else:
                buttons.append({"type": "QUICK_REPLY", "text": b["text"]})
        comps.append({"type": "BUTTONS", "buttons": buttons})
    return comps


def from_components(row_components: list) -> dict:
    """Reconstruye body/header/footer/buttons desde los componentes guardados."""
    out = {"body": "", "header": None, "footer": None, "buttons": [], "example_values": []}
    for c in row_components or []:
        kind = c.get("type")
        if kind == "BODY":
            out["body"] = c.get("text", "")
            ex = (c.get("example") or {}).get("body_text") or [[]]
            out["example_values"] = list(ex[0]) if ex else []
        elif kind == "HEADER" and c.get("format") == "TEXT":
            out["header"] = c.get("text")
        elif kind == "FOOTER":
            out["footer"] = c.get("text")
        elif kind == "BUTTONS":
            out["buttons"] = [{k: v for k, v in b.items() if k in ("type", "text", "url")} for b in c.get("buttons", [])]
    return out
