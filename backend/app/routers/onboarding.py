"""Asistente de onboarding y configuración automática (docs/data-model.md §13)."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.config import get_settings
from app.db import get_session
from app.models import Agent, Channel
from app.onboarding import checks, graph, meta, packs, service, website
from app.onboarding.service import StepError
from app.plans import enforce_limit

router = APIRouter(prefix="/api", tags=["onboarding"])


async def _run(session: AsyncSession, agent: Agent):
    run = await service.active_run(session, agent.organization_id, create=True, agent_id=agent.id)
    if run is None:
        raise HTTPException(409, "El asistente ya terminó. Puedes volver a empezarlo desde «Reiniciar».")
    return run


def _err(e: Exception):
    if isinstance(e, StepError):
        body = {"detail": str(e)}
        if e.blocking:
            body["blocking"] = e.blocking
        return JSONResponse(status_code=e.status, content=body)
    if isinstance(e, website.ImportError_):
        return JSONResponse(status_code=422, content={"detail": str(e)})
    return JSONResponse(status_code=502, content={"detail": f"Meta: {e}"})


ERRORS = (StepError, graph.GraphError, website.ImportError_)


@router.get("/onboarding")
async def get_onboarding(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    out = await service.state(session, agent.organization_id, agent.id)
    await session.commit()
    return out


class AnswersIn(BaseModel):
    industry: str | None = None
    answers: dict = Field(default_factory=dict)
    current_step: str | None = None


@router.put("/onboarding/answers")
async def put_answers(body: AnswersIn, agent: Agent = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    run = await _run(session, agent)
    try:
        await service.save_answers(session, run, body.industry, body.answers, agent.id, body.current_step)
    except StepError as e:
        return _err(e)
    await session.commit()
    return service.run_out(run)


class ImportIn(BaseModel):
    url: str


@router.post("/onboarding/import-website")
async def import_website(body: ImportIn, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    run = await _run(session, agent)
    try:
        data = await website.import_site(agent.organization_id, body.url)
    except ERRORS as e:
        return _err(e)
    except Exception as e:  # el LLM o la red fallaron: mensaje claro, sin traza al usuario
        return JSONResponse(status_code=502, content={"detail": f"No se pudo analizar el sitio: {type(e).__name__}"})
    run.answers = service.deep_merge(run.answers or {}, {"website_import": data})
    await session.commit()
    return data


class WhatsAppIn(BaseModel):
    code: str
    waba_id: str
    phone_number_id: str
    pin: str | None = Field(default=None, pattern=r"^\d{6}$")
    coexistence: bool = False
    name: str | None = None


@router.post("/onboarding/whatsapp")
async def connect_whatsapp(body: WhatsAppIn, agent: Agent = Depends(require_admin),
                           session: AsyncSession = Depends(get_session)):
    env = get_settings()
    if not (env.meta_app_id and env.meta_app_secret):
        raise HTTPException(409, "Falta configurar META_APP_ID y META_APP_SECRET en el servidor")
    run = await _run(session, agent)
    from sqlalchemy import select

    existing = await session.scalar(select(Channel).where(Channel.phone_number_id == body.phone_number_id))
    if existing and existing.organization_id != agent.organization_id:
        raise HTTPException(409, "Ese número ya está conectado a otra empresa")
    if not existing:
        await enforce_limit(session, agent.organization_id, "channels")
    s = await service.step(session, run, "whatsapp")
    service.begin(s)
    try:
        channel, pin, detail = await meta.connect_whatsapp(
            session, agent.organization_id, code=body.code, waba_id=body.waba_id,
            phone_number_id=body.phone_number_id, pin=body.pin, generate_pin=not body.pin and not body.coexistence,
            coexistence=body.coexistence, name=body.name)
        await service.after_connect(session, run, channel, detail)
    except graph.GraphError as e:
        service.finish(s, "failed", error=f"Meta: {e}")
        await session.commit()
        return _err(e)
    await session.commit()
    return {"channel": {"id": channel.id, "name": channel.name, "display_phone": channel.display_phone,
                        "phone_number_id": channel.phone_number_id, "waba_id": channel.waba_id,
                        "verified_name": channel.verified_name, "is_coexistence": channel.is_coexistence},
            "pin": pin}


@router.post("/onboarding/checks/run")
async def run_checks(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    run = await _run(session, agent)
    try:
        results = await service.run_validate(session, run)
    except ERRORS as e:
        return _err(e)
    await session.commit()
    return {"checks": results}


@router.post("/onboarding/checks/{check_key}/fix")
async def fix_check(check_key: str, agent: Agent = Depends(require_admin),
                    session: AsyncSession = Depends(get_session)):
    if check_key not in checks.CHECKS:
        raise HTTPException(404, "Chequeo desconocido")
    run = await _run(session, agent)
    try:
        channel = await service.run_channel(session, run)
        result = await checks.fix(session, channel, check_key, run, agent.id)
        await service.run_validate(session, run)
    except ERRORS as e:
        await session.commit()
        return _err(e)
    await session.commit()
    return {"check": result}


@router.get("/onboarding/template-pack")
async def template_pack(industry: str | None = None, agent: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    run = await service.active_run(session, agent.organization_id, create=False) or \
        await service.latest_run(session, agent.organization_id)
    return packs.pack(industry or (run.industry if run else None), (run.answers if run else {}) or {})


class TemplatesIn(BaseModel):
    template_keys: list[str] = Field(default_factory=list)
    edits: dict[str, dict] = Field(default_factory=dict)


@router.post("/onboarding/templates")
async def submit_templates(body: TemplatesIn, agent: Agent = Depends(require_admin),
                           session: AsyncSession = Depends(get_session)):
    run = await _run(session, agent)
    try:
        channel = await service.run_channel(session, run)
        out = await service.submit_pack(session, channel, run, body.template_keys or None, body.edits, agent.id)
    except ERRORS as e:
        return _err(e)
    await session.commit()
    return out


@router.post("/onboarding/templates/{template_key}/rewrite")
async def rewrite_template(template_key: str, agent: Agent = Depends(require_admin),
                           session: AsyncSession = Depends(get_session)):
    run = await _run(session, agent)
    try:
        out = await service.rewrite_rejected(session, run, template_key, agent.id)
    except ERRORS as e:
        return _err(e)
    await session.commit()
    return out


RUNNABLE = {"company", "whatsapp", "profile", "ai", "team", "validate"}


@router.post("/onboarding/steps/{key}/run")
async def run_step(key: str, body: dict | None = None, agent: Agent = Depends(require_admin),
                   session: AsyncSession = Depends(get_session)):
    if key not in RUNNABLE:
        raise HTTPException(422, f"El paso «{key}» no se ejecuta así")
    run = await _run(session, agent)
    try:
        if key == "company":
            s = await service.run_company(session, run, body or {}, agent.id)
        elif key == "whatsapp":  # conexión manual: el número se creó con POST /api/channels
            await service.adopt_channel(session, run)
            s = await service.step(session, run, "whatsapp")
        elif key == "profile":
            s = await service.run_profile(session, run, body or {})
        elif key == "ai":
            s = await service.run_ai(session, run)
        elif key == "team":
            s = await service.run_team(session, run)
        else:
            await service.run_validate(session, run)
            s = await service.step(session, run, "validate")
    except ERRORS as e:
        return _err(e)
    await session.commit()
    return {"step": service.step_out(s)}


@router.post("/onboarding/steps/{key}/skip")
async def skip_step(key: str, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    if key not in service.STEP_KEYS:
        raise HTTPException(404, "Paso desconocido")
    if key in service.REQUIRED:
        raise HTTPException(409, "Este paso es obligatorio")
    run = await _run(session, agent)
    s = await service.step(session, run, key)
    service.finish(s, "skipped")
    service.advance(run, await service.steps_of(session, run))
    await session.commit()
    return {"step": service.step_out(s)}


class TestIn(BaseModel):
    phone: str | None = None  # vacío = answers.test.phone


@router.post("/onboarding/test")
async def send_test(body: TestIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    run = await _run(session, agent)
    try:
        phone = body.phone or (((run.answers or {}).get("test") or {}).get("phone")) or ""
        out = await service.send_test(session, run, phone)
    except ERRORS as e:
        return _err(e)
    await session.commit()
    return out


@router.post("/onboarding/go-live")
async def go_live(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    run = await _run(session, agent)
    try:
        out = await service.go_live(session, run)
    except ERRORS as e:
        await session.commit()  # los chequeos recién corridos quedan guardados
        return _err(e)
    await session.commit()
    return out


@router.post("/onboarding/restart")
async def restart(agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    current = await service.active_run(session, agent.organization_id, create=False)
    previous = current or await service.latest_run(session, agent.organization_id)
    if current:
        current.status = "abandoned"
        await session.flush()
    run = await service.start_run(session, agent.organization_id, agent.id,
                                  channel_id=previous.channel_id if previous else None)
    if previous:
        run.industry = previous.industry
        run.answers = {k: v for k, v in (previous.answers or {}).items() if k != "test"}
    await session.commit()
    return await service.state(session, agent.organization_id, agent.id)


@router.get("/channels/{channel_id}/health")
async def channel_health(channel_id: int, agent: Agent = Depends(current_agent),
                         session: AsyncSession = Depends(get_session)):
    channel = await session.get(Channel, channel_id)
    if not channel or channel.organization_id != agent.organization_id:
        raise HTTPException(404, "Canal no encontrado")
    return {"checks": await checks.list_checks(session, channel.id)}
