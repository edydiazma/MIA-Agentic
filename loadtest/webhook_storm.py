"""Alternativa en Python (asyncio + httpx) a k6/webhook_storm.js, para cuando k6 no está instalado.

    cd backend && .venv/bin/python ../loadtest/webhook_storm.py --base https://staging... --rps 50 --seconds 300 \
        --phones 2000 --pnid <phone_number_id> --waba <waba_id>
WA_APP_SECRET se lee del entorno (no lo pases por la línea de comandos: queda en el historial).

Genera la misma mezcla (70 % mensajes: texto, imagen, Click to WhatsApp, 5 % solo BSUID; 30 % estados), firma con
X-Hub-Signature-256 y al final imprime p50/p95/p99 y tasa de error. Sale con código 1 si se rompen los umbrales
(p95 > 300 ms o errores > 0,5 %), igual que k6.
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import random
import statistics
import time
import uuid

import httpx


def phone(i: int, phones: int) -> str:
    return f"5730{10000000 + i % phones:08d}"


def bsuid(i: int, phones: int) -> str:
    return f"CO.{1000000000000 + i % phones}"


def change(i: int, args) -> dict:
    now = str(int(time.time()))
    meta = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": args.pnid}}
    if random.random() < 0.3:
        return {"field": "messages", "value": {**meta, "statuses": [{
            "id": f"wamid.out.{uuid.uuid4().hex}", "status": random.choice(["sent", "delivered", "read"]),
            "timestamp": now, "recipient_id": phone(i, args.phones), "recipient_user_id": bsuid(i, args.phones)}]}}
    r = random.random()
    with_phone = r > 0.05
    msg = {"id": f"wamid.lt.{uuid.uuid4().hex}", "timestamp": now, "from_user_id": bsuid(i, args.phones)}
    contact = {"profile": {"name": f"Cliente {i % args.phones}"}, "user_id": bsuid(i, args.phones)}
    if with_phone:
        msg["from"] = contact["wa_id"] = phone(i, args.phones)
    if r < 0.10:
        msg |= {"type": "text", "text": {"body": "Hola, quiero la promo"},
                "referral": {"source_type": "ad", "source_id": f"1202{i % 50}", "headline": "Promo",
                             "ctwa_clid": f"CLID{uuid.uuid4().hex[:12]}"}}
    elif r < 0.18:
        msg |= {"type": "image", "image": {"id": f"media{uuid.uuid4().hex[:10]}", "mime_type": "image/jpeg"}}
    else:
        msg |= {"type": "text", "text": {"body": random.choice(["hola", "precio?", "quiero cotizar", "gracias"])}}
    return {"field": "messages", "value": {**meta, "contacts": [contact], "messages": [msg]}}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--rps", type=float, default=20)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--phones", type=int, default=2000)
    ap.add_argument("--pnid", default="PNID")
    ap.add_argument("--waba", default="WABA")
    args = ap.parse_args()
    secret = os.environ.get("WA_APP_SECRET", "").encode()

    latencies: list[float] = []
    errors = 0
    sem = asyncio.Semaphore(int(args.rps * 4) or 1)

    async def one(client: httpx.AsyncClient, i: int) -> None:
        nonlocal errors
        body = json.dumps({"object": "whatsapp_business_account",
                           "entry": [{"id": args.waba, "changes": [change(i, args)]}]}).encode()
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        async with sem:
            t0 = time.perf_counter()
            try:
                res = await client.post(f"{args.base}/webhooks/whatsapp", content=body, headers=headers)
                if res.status_code != 200:
                    errors += 1
            except httpx.HTTPError:
                errors += 1
            latencies.append((time.perf_counter() - t0) * 1000)

    async with httpx.AsyncClient(timeout=10, limits=httpx.Limits(max_connections=int(args.rps * 4) or 1)) as client:
        tasks, start, i = [], time.perf_counter(), 0
        while (elapsed := time.perf_counter() - start) < args.seconds:
            target = int(elapsed * args.rps)  # tasa constante: dispara lo que falte hasta este instante
            while i < target:
                tasks.append(asyncio.create_task(one(client, random.randrange(10**9))))
                i += 1
            await asyncio.sleep(0.01)
        await asyncio.gather(*tasks)

    n = len(latencies)
    q = statistics.quantiles(latencies, n=100) if n >= 2 else [latencies[0] if n else 0] * 99
    err_rate = errors / n if n else 1
    print(f"enviados={n} errores={errors} ({err_rate:.2%}) p50={q[49]:.0f}ms p95={q[94]:.0f}ms p99={q[98]:.0f}ms")
    return 1 if q[94] > 300 or err_rate > 0.005 else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
