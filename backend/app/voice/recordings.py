"""Grabación de llamadas atendidas por el agente de voz: mezcla mono PCM16 → WAV en Supabase Storage."""

import array
import io
import wave

from app import storage

RATE = 24000  # Hz, mismo formato que el audio del modelo en tiempo real


class Recorder:
    """Acumula dos pistas (cliente y agente) alineadas por posición en muestras y las mezcla al final."""

    def __init__(self, rate: int = RATE):
        self.rate = rate
        self.tracks: dict[str, array.array] = {"in": array.array("h"), "out": array.array("h")}

    def write(self, track: str, pcm16: bytes, at_sample: int | None = None) -> None:
        buf = self.tracks[track]
        samples = array.array("h")
        samples.frombytes(pcm16[: len(pcm16) - len(pcm16) % 2])
        if at_sample is not None and at_sample > len(buf):
            buf.extend([0] * (at_sample - len(buf)))
        buf.extend(samples)

    def position(self, track: str) -> int:
        return len(self.tracks[track])

    def mix(self) -> bytes:
        a, b = self.tracks["in"], self.tracks["out"]
        n = max(len(a), len(b))
        out = array.array("h", [0] * n)
        for i in range(n):
            v = (a[i] if i < len(a) else 0) + (b[i] if i < len(b) else 0)
            out[i] = 32767 if v > 32767 else -32768 if v < -32768 else v
        return out.tobytes()

    def wav(self) -> bytes:
        bio = io.BytesIO()
        with wave.open(bio, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(self.mix())
        return bio.getvalue()


async def save_recording(org: int, call_id: int, recorder: Recorder) -> str | None:
    if not any(len(t) for t in recorder.tracks.values()):
        return None
    path = storage.new_path(storage.MEDIA_BUCKET, org, f"calls/{call_id}", "audio/wav")
    await storage.upload(path, recorder.wav(), "audio/wav")
    return path
