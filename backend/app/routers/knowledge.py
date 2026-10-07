"""Base de conocimiento de la organización; cada agente de IA elige qué documentos usa (ai_agent_knowledge)."""

import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.db import get_session
from app.models import Agent, AIAgent, AIAgentKnowledge, KnowledgeDoc
from app.schemas import UTCDateTime

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

MAX_FILE = 20 * 1024 * 1024
TEXT_EXT = (".txt", ".md", ".csv", ".json")


class DocOut(BaseModel):
    id: int
    title: str
    content: str
    enabled: bool
    source_filename: str | None
    updated_at: UTCDateTime
    chars: int
    bot_ids: list[int] = []


class DocIn(BaseModel):
    title: str
    content: str
    enabled: bool = True
    bot_id: int | None = None  # si viene, el documento queda conectado a ese agente


class DocUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    enabled: bool | None = None


async def _links(session: AsyncSession, doc_ids: list[int]) -> dict[int, list[int]]:
    rows = (await session.execute(select(AIAgentKnowledge.doc_id, AIAgentKnowledge.ai_agent_id)
                                  .where(AIAgentKnowledge.doc_id.in_(doc_ids or [0])))).all()
    out: dict[int, list[int]] = {}
    for doc_id, bot_id in rows:
        out.setdefault(doc_id, []).append(bot_id)
    return out


def _out(d: KnowledgeDoc, bot_ids: list[int] | None = None) -> DocOut:
    return DocOut(id=d.id, title=d.title, content=d.content, enabled=d.enabled, source_filename=d.source_filename,
                  updated_at=d.updated_at, chars=len(d.content), bot_ids=sorted(bot_ids or []))


def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        reader = PdfReader(io.BytesIO(data))
        text = "\n\n".join(t for t in ((p.extract_text() or "").strip() for p in reader.pages) if t)
        if not text:
            raise HTTPException(422, "El PDF no tiene texto extraíble (¿es un escaneo?)")
        return text
    if name.endswith(TEXT_EXT):
        return data.decode("utf-8", "replace")
    raise HTTPException(422, f"Formato no soportado. Usa PDF o {', '.join(TEXT_EXT)}")


async def _link_bot(session: AsyncSession, org: int, bot_id: int | None, doc: KnowledgeDoc) -> None:
    if bot_id is None:
        return
    bot = await session.get(AIAgent, bot_id)
    if not bot or bot.organization_id != org:
        raise HTTPException(404, "Agente no encontrado")
    session.add(AIAgentKnowledge(ai_agent_id=bot_id, doc_id=doc.id))


async def _doc(session: AsyncSession, doc_id: int, org: int) -> KnowledgeDoc:
    doc = await session.get(KnowledgeDoc, doc_id)
    if not doc or doc.organization_id != org:
        raise HTTPException(404, "Documento no encontrado")
    return doc


@router.get("", response_model=list[DocOut])
async def list_docs(bot_id: int | None = None, agent: Agent = Depends(current_agent),
                    session: AsyncSession = Depends(get_session)):
    stmt = select(KnowledgeDoc).where(KnowledgeDoc.organization_id == agent.organization_id)
    if bot_id is not None:
        stmt = stmt.join(AIAgentKnowledge, AIAgentKnowledge.doc_id == KnowledgeDoc.id).where(
            AIAgentKnowledge.ai_agent_id == bot_id)
    docs = (await session.scalars(stmt.order_by(KnowledgeDoc.id))).all()
    links = await _links(session, [d.id for d in docs])
    return [_out(d, links.get(d.id)) for d in docs]


@router.post("", response_model=DocOut)
async def create_doc(body: DocIn, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    if not body.title.strip() or not body.content.strip():
        raise HTTPException(422, "Título y contenido son obligatorios")
    doc = KnowledgeDoc(organization_id=agent.organization_id, title=body.title.strip(), content=body.content,
                       enabled=body.enabled)
    session.add(doc)
    await session.flush()
    await _link_bot(session, agent.organization_id, body.bot_id, doc)
    await session.commit()
    return _out(doc, [body.bot_id] if body.bot_id else [])


@router.post("/upload", response_model=DocOut)
async def upload_doc(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    bot_id: int | None = Form(default=None),
    agent: Agent = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    data = await file.read()
    if len(data) > MAX_FILE:
        raise HTTPException(413, "Archivo demasiado grande (máx. 20 MB)")
    filename = file.filename or "documento"
    doc = KnowledgeDoc(organization_id=agent.organization_id, title=(title or filename.rsplit(".", 1)[0]).strip(),
                       content=extract_text(filename, data).strip(), source_filename=filename)
    session.add(doc)
    await session.flush()
    await _link_bot(session, agent.organization_id, bot_id, doc)
    await session.commit()
    return _out(doc, [bot_id] if bot_id else [])


@router.put("/{doc_id}", response_model=DocOut)
async def update_doc(doc_id: int, body: DocUpdate, agent: Agent = Depends(require_admin),
                     session: AsyncSession = Depends(get_session)):
    doc = await _doc(session, doc_id, agent.organization_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(doc, k, v)
    await session.commit()
    return _out(doc, (await _links(session, [doc.id])).get(doc.id))


@router.delete("/{doc_id}")
async def delete_doc(doc_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    doc = await _doc(session, doc_id, agent.organization_id)
    await session.delete(doc)  # ai_agent_knowledge se borra en cascada
    await session.commit()
    return {"ok": True}
