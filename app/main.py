import base64
import hmac
import itertools
import json
import logging
import os
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

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
        "default_language":     os.getenv("DEFAULT_LANGUAGE", "auto"),
        "default_audio_format": os.getenv("DEFAULT_AUDIO_FORMAT", "wav"),
        "chat_temperature":     float(os.getenv("CHAT_TEMPERATURE", "0.7")),
        "chat_max_tokens":      int(os.getenv("CHAT_MAX_TOKENS", "140")),
        "stt_timeout_seconds":  float(os.getenv("STT_TIMEOUT_SECONDS", "60")),
        "chat_timeout_seconds": float(os.getenv("CHAT_TIMEOUT_SECONDS", "30")),
        "tts_timeout_seconds":  float(os.getenv("TTS_TIMEOUT_SECONDS", "60")),
        "cors_allowed_origins": os.getenv("CORS_ALLOWED_ORIGINS", "*"),
        "log_retention":        int(os.getenv("LOG_RETENTION", "100")),
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
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

# ── request log ───────────────────────────────────────────────────────────────
REQUEST_LOG: deque = deque(maxlen=int(CFG.get("log_retention") or 100))
_seq = itertools.count(1)
SESSION_TOKENS: set[str] = set()

# ── app ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="voice-chat-gateway")


def _auth(request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    token = auth[len("Bearer "):]
    if token in SESSION_TOKENS:
        return
    if hmac.compare_digest(token, str(CFG["voice_chat_api_key"])):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


def _check_login(username: str, password: str) -> None:
    user_ok = hmac.compare_digest(username, ADMIN_USERNAME)
    pass_ok = hmac.compare_digest(password, ADMIN_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _configured_origins() -> list[str]:
    raw = str(CFG.get("cors_allowed_origins") or "*")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _cors_origin_for(request: Request) -> Optional[str]:
    origin = request.headers.get("origin")
    origins = _configured_origins()
    if "*" in origins:
        return "*"
    if origin and origin in origins:
        return origin
    return None


@app.middleware("http")
async def configurable_cors(request: Request, call_next):
    if request.method == "OPTIONS":
        response = Response(status_code=204)
    else:
        response = await call_next(request)

    origin = _cors_origin_for(request)
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization,Content-Type"
        response.headers["Vary"] = "Origin"
    return response


def _coerce_config_value(key: str, value):
    numeric = {
        "chat_temperature": float,
        "stt_timeout_seconds": float,
        "chat_timeout_seconds": float,
        "tts_timeout_seconds": float,
        "chat_max_tokens": int,
        "log_retention": int,
    }
    if key not in numeric:
        return value
    try:
        coerced = numeric[key](value)
    except (TypeError, ValueError):
        raise HTTPException(400, detail=f"Invalid value for {key}")
    if key == "log_retention" and coerced < 1:
        raise HTTPException(400, detail="log_retention must be at least 1")
    return coerced


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/login")
async def login(request: Request):
    credentials = await request.json()
    _check_login(str(credentials.get("username", "")), str(credentials.get("password", "")))
    token = secrets.token_urlsafe(32)
    SESSION_TOKENS.add(token)
    return {"token": token}


@app.post("/logout")
async def logout(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        SESSION_TOKENS.discard(auth[len("Bearer "):])
    return {"ok": True}


@app.get("/config")
def get_config(request: Request):
    _auth(request)
    return CFG


@app.post("/config")
async def set_config(request: Request):
    global REQUEST_LOG
    _auth(request)
    updates = await request.json()
    allowed = {
        "stt_url", "stt_api_key",
        "chat_completions_url", "chat_api_key", "chat_model",
        "tts_url", "tts_api_key", "default_voice",
        "system_prompt", "voice_chat_api_key",
        "default_language", "default_audio_format",
        "chat_temperature", "chat_max_tokens",
        "stt_timeout_seconds", "chat_timeout_seconds", "tts_timeout_seconds",
        "cors_allowed_origins", "log_retention",
    }
    for k, v in updates.items():
        if k in allowed:
            CFG[k] = _coerce_config_value(k, v)
    REQUEST_LOG = deque(REQUEST_LOG, maxlen=int(CFG.get("log_retention") or 100))
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
    language = language or CFG.get("default_language") or "auto"
    used_voice = voice or CFG["default_voice"]
    used_model = model or CFG["chat_model"]
    audio_format = response_audio_format or CFG.get("default_audio_format") or "wav"

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
            "audio_format": audio_format,
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

    async with httpx.AsyncClient(timeout=httpx.Timeout(float(CFG.get("stt_timeout_seconds") or 60))) as client:
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
        "max_tokens": int(CFG.get("chat_max_tokens") or 140),
        "temperature": float(CFG.get("chat_temperature") or 0.7),
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(float(CFG.get("chat_timeout_seconds") or 30))) as client:
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
    async with httpx.AsyncClient(timeout=httpx.Timeout(float(CFG.get("tts_timeout_seconds") or 60))) as client:
        resp = await client.get(
            CFG["tts_url"],
            params={"text": text, "voice": voice},
            headers={"Authorization": f"Bearer {CFG['tts_api_key']}"},
        )

    if resp.status_code != 200:
        raise HTTPException(502, detail=f"TTS upstream error {resp.status_code}: {resp.text[:300]}")
    return resp.content
