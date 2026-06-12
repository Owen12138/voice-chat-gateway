import base64
import itertools
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("voice-chat-gateway")

# ── persistent config ─────────────────────────────────────────────────────────
_CONFIG_FILE = Path("/app/data/config.json")


def _default_config() -> dict:
    return {
        "voice_chat_api_key":   os.environ["VOICE_CHAT_API_KEY"],
        "stt_url":              os.environ["STT_URL"],
        "stt_api_key":          os.environ["STT_API_KEY"],
        "chat_completions_url": os.environ["CHAT_COMPLETIONS_URL"],
        "chat_api_key":         os.environ["CHAT_API_KEY"],
        "chat_model":           os.getenv("CHAT_MODEL", "default"),
        "tts_url":              os.environ["TTS_URL"],
        "tts_api_key":          os.environ["TTS_API_KEY"],
        "default_voice":        os.getenv("DEFAULT_VOICE", "default"),
        "system_prompt": (
            "You are a low-latency voice assistant. "
            "Reply in the user's language. "
            "Keep normal replies concise: 1-2 short sentences unless the user asks for detail. "
            "No markdown, no bullet lists unless requested."
        ),
    }


def _load_config() -> dict:
    cfg = _default_config()
    if _CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(_CONFIG_FILE.read_text()))
        except Exception as exc:
            log.warning("config load failed: %s", exc)
    return cfg


def _save_config(cfg: dict) -> None:
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


CFG: dict = _load_config()

# ── request log ───────────────────────────────────────────────────────────────
REQUEST_LOG: deque = deque(maxlen=100)
_seq = itertools.count(1)

# ── app ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="voice-chat-gateway")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _auth(request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[len("Bearer "):] != CFG["voice_chat_api_key"]:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/config")
def get_config(request: Request):
    _auth(request)
    return CFG


@app.post("/config")
async def set_config(request: Request):
    _auth(request)
    updates = await request.json()
    allowed = {
        "stt_url", "stt_api_key",
        "chat_completions_url", "chat_api_key", "chat_model",
        "tts_url", "tts_api_key", "default_voice",
        "system_prompt", "voice_chat_api_key",
    }
    for k, v in updates.items():
        if k in allowed:
            CFG[k] = v
    try:
        _save_config(CFG)
    except Exception as exc:
        log.warning("config save failed: %s", exc)
    return CFG


@app.get("/logs")
def get_logs(request: Request):
    _auth(request)
    return list(reversed(REQUEST_LOG))


@app.post("/voice-chat")
async def voice_chat(
    request: Request,
    audio: UploadFile = File(...),
    language: Optional[str] = Form(default=None),
    voice: Optional[str] = Form(default=None),
    model: Optional[str] = Form(default=None),
    response_audio_format: Optional[str] = Form(default="wav"),
):
    _auth(request)
    t_start = time.perf_counter()

    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(400, detail="No audio data received.")

    filename = audio.filename or "audio.bin"
    used_voice = voice or CFG["default_voice"]
    used_model = model or CFG["chat_model"]

    log.info(
        "upload received — %d bytes  file=%s  lang=%s  voice=%s  model=%s",
        len(audio_bytes), filename, language, used_voice, used_model,
    )

    entry = {
        "id": next(_seq),
        "ts": time.time(),
        "file": filename,
        "lang": language or "auto",
        "voice": used_voice,
        "model": used_model,
        "stt_ms": 0, "chat_ms": 0, "tts_ms": 0, "total_ms": 0,
        "transcript": "", "reply": "",
        "audio_bytes": 0,
        "status": "error",
        "error": None,
    }

    try:
        t0 = time.perf_counter()
        transcript = await _stt(audio_bytes, filename, language)
        stt_elapsed = time.perf_counter() - t0
        entry["stt_ms"] = int(stt_elapsed * 1000)
        entry["transcript"] = transcript
        log.info("STT %.2fs — %r", stt_elapsed, transcript[:100])

        if not transcript:
            raise HTTPException(422, detail="STT returned an empty transcript.")

        t0 = time.perf_counter()
        reply = await _chat(transcript, used_model)
        chat_elapsed = time.perf_counter() - t0
        entry["chat_ms"] = int(chat_elapsed * 1000)
        entry["reply"] = reply
        log.info("Chat %.2fs — %r", chat_elapsed, reply[:100])

        if not reply:
            raise HTTPException(502, detail="Chat returned an empty reply.")

        t0 = time.perf_counter()
        tts_audio = await _tts(reply, used_voice)
        tts_elapsed = time.perf_counter() - t0
        entry["tts_ms"] = int(tts_elapsed * 1000)
        entry["audio_bytes"] = len(tts_audio)
        entry["total_ms"] = int((time.perf_counter() - t_start) * 1000)
        entry["status"] = "ok"
        log.info("TTS %.2fs — total %.2fs — %d bytes", tts_elapsed, entry["total_ms"] / 1000, len(tts_audio))

        REQUEST_LOG.append(entry)
        return {
            "transcript":   transcript,
            "reply":        reply,
            "audio_format": response_audio_format or "wav",
            "sample_rate":  24000,
            "channels":     1,
            "audio_base64": base64.b64encode(tts_audio).decode(),
        }

    except HTTPException as exc:
        entry["total_ms"] = int((time.perf_counter() - t_start) * 1000)
        entry["error"] = str(exc.detail)
        REQUEST_LOG.append(entry)
        raise


async def _stt(audio_bytes: bytes, filename: str, language: Optional[str]) -> str:
    params: dict = {}
    if language and language.lower() not in ("auto", ""):
        params["language"] = language.lower()

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = await client.post(
            CFG["stt_url"],
            params=params,
            headers={"Authorization": f"Bearer {CFG['stt_api_key']}"},
            files={"file": (filename, audio_bytes)},
        )

    if resp.status_code != 200:
        raise HTTPException(502, detail=f"STT upstream error {resp.status_code}: {resp.text[:300]}")
    return resp.json().get("transcript", "")


async def _chat(transcript: str, model: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CFG["system_prompt"]},
            {"role": "user",   "content": transcript},
        ],
        "max_tokens": 140,
        "temperature": 0.7,
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.post(
            CFG["chat_completions_url"],
            json=payload,
            headers={
                "Authorization": f"Bearer {CFG['chat_api_key']}",
                "Content-Type": "application/json",
            },
        )

    if resp.status_code != 200:
        raise HTTPException(502, detail=f"Chat upstream error {resp.status_code}: {resp.text[:300]}")

    try:
        return resp.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as exc:
        raise HTTPException(502, detail=f"Unexpected chat response shape: {exc}")


async def _tts(text: str, voice: str) -> bytes:
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = await client.get(
            CFG["tts_url"],
            params={"text": text, "voice": voice},
            headers={"Authorization": f"Bearer {CFG['tts_api_key']}"},
        )

    if resp.status_code != 200:
        raise HTTPException(502, detail=f"TTS upstream error {resp.status_code}: {resp.text[:300]}")
    return resp.content
