"""Formato neutral de conversación para que cada proveedor de IA lo traduzca."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class TextPart:
    text: str


@dataclass
class ImagePart:
    data: bytes
    mime: str


@dataclass
class DocumentPart:
    """PDF u otro documento binario que el modelo puede leer directamente."""

    data: bytes
    mime: str
    filename: str


Part = TextPart | ImagePart | DocumentPart


@dataclass
class Turn:
    role: Literal["user", "assistant"]
    parts: list[Part] = field(default_factory=list)


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]


@dataclass
class AgentRequest:
    model: str
    system: str  # estable entre conversaciones: se cachea
    turns: list[Turn]
    tools: list[ToolSpec]
    effort: str | None = None
    max_tokens: int = 4096
    context: str = ""  # dinámico (fecha, datos del cliente): va después de la parte cacheada


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, input_tokens=0, output_tokens=0, cache_read_tokens=0) -> None:
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0
        self.cache_read_tokens += cache_read_tokens or 0


@dataclass
class AgentResult:
    text: str
    refused: bool = False
    usage: Usage = field(default_factory=Usage)


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]


class LLMProvider(Protocol):
    async def run(self, req: AgentRequest, execute_tool: ToolExecutor) -> AgentResult:
        """Corre el ciclo de herramientas completo y devuelve el texto final."""
        ...


MAX_TOOL_STEPS = 6
