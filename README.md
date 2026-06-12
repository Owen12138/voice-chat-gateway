# voice-chat-gateway

A generic voice-chat pipeline: **audio in → STT → chat completion → TTS → base64 audio out**.
Any client (web app, bot, embedded device, automation script) can call one endpoint and get a
spoken response back.

## Setup

```bash
cp .env.example .env
# Edit .env and fill in your API keys
docker compose up -d --build
```

> **Note:** Port 18891 is reserved for stt-frontend. This service runs on **18892**.

## Web UI

Open `http://localhost:18892/` after the container starts.

The web UI is an operations console for:

- live request monitoring with STT, LLM, and TTS timings
- editing gateway, STT, LLM, TTS, CORS, timeout, and log-retention settings
- selecting OpenAI-compatible LLM provider presets
- recording a browser microphone test request

The UI defaults to dark theme. Use the theme toggle in the top bar to switch
between dark and light themes. The selected theme is stored in the browser.

### Web UI login

The web UI uses username/password login from `.env`:

```bash
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin
```

Those defaults are for first boot only. Change them before exposing the service
outside a trusted network.

The API bearer token remains separate from the UI login. API clients should keep
using `Authorization: Bearer <VOICE_CHAT_API_KEY>`.

### LLM provider presets

The UI includes presets for OpenClaw, ChatGPT/OpenAI, Anthropic, Gemini, Ollama,
DeepSeek, LM Studio, OpenRouter, and Custom.

Presets update the OpenAI-compatible chat-completions URL and default model.
They do not overwrite API keys.

## Configuration

Initial config is loaded from `.env` (copied from `.env.example`). The web UI can
then persist runtime config to `/app/data/config.json` in the Docker volume.

| Variable               | Description                                            |
|------------------------|--------------------------------------------------------|
| `VOICE_CHAT_API_KEY`   | Bearer token required on every request                 |
| `ADMIN_USERNAME`       | Web UI login username (defaults to `admin`)            |
| `ADMIN_PASSWORD`       | Web UI login password (defaults to `admin`)            |
| `STT_URL`              | Full URL to the STT `/transcribe` endpoint             |
| `STT_API_KEY`          | Bearer token for the STT service                       |
| `CHAT_COMPLETIONS_URL` | OpenAI-compatible `/v1/chat/completions` URL           |
| `CHAT_API_KEY`         | API key for the chat service                           |
| `CHAT_MODEL`           | Default model name passed to the chat service          |
| `TTS_URL`              | Base URL of the TTS proxy (e.g. `http://…:5001/`)      |
| `TTS_API_KEY`          | Bearer token for the TTS service                       |
| `DEFAULT_VOICE`        | Voice ID to use when the client omits `voice`          |
| `DEFAULT_LANGUAGE`     | Language to use when the client omits `language`       |
| `DEFAULT_AUDIO_FORMAT` | Response audio format label returned by the gateway    |
| `CHAT_TEMPERATURE`     | Default chat completion temperature                    |
| `CHAT_MAX_TOKENS`      | Default chat completion token cap                      |
| `STT_TIMEOUT_SECONDS`  | STT upstream timeout                                   |
| `CHAT_TIMEOUT_SECONDS` | Chat upstream timeout                                  |
| `TTS_TIMEOUT_SECONDS`  | TTS upstream timeout                                   |
| `CORS_ALLOWED_ORIGINS` | Comma-separated allowed origins, or `*`                |
| `LOG_RETENTION`        | Number of request log entries kept in memory           |

### STT service assumed request shape

```
POST {STT_URL}?language={lang}
Authorization: Bearer {STT_API_KEY}
Content-Type: multipart/form-data
  file=<audio bytes>
```

Expected response:
```json
{ "transcript": "...", "language": "en", "duration_seconds": 1.23 }
```

Omit `language` param (or pass `auto`) to let the STT service auto-detect.

## Endpoints

### `GET /health`

```json
{ "ok": true }
```

### `POST /voice-chat`

**Headers:** `Authorization: Bearer <VOICE_CHAT_API_KEY>`

**Form fields:**

| Field                  | Required | Description                                       |
|------------------------|----------|---------------------------------------------------|
| `audio`                | Yes      | Audio file (wav, mp3, webm, ogg, …)               |
| `language`             | No       | `auto` (default), `en`, `zh`, etc.                |
| `voice`                | No       | TTS voice ID (falls back to `DEFAULT_VOICE`)       |
| `model`                | No       | Chat model override (falls back to `CHAT_MODEL`)   |
| `response_audio_format`| No       | `wav` (default)                                   |

**Response:**

```json
{
  "transcript":   "what time is it",
  "reply":        "I don't have access to real-time data, so I can't tell you the exact time.",
  "audio_format": "wav",
  "sample_rate":  24000,
  "channels":     1,
  "audio_base64": "<base64-encoded WAV bytes>"
}
```

**Error codes:**

| Code | Reason                              |
|------|-------------------------------------|
| 401  | Missing or wrong bearer token       |
| 400  | No audio provided                   |
| 422  | STT returned empty transcript       |
| 502  | Upstream STT / chat / TTS error     |
| 500  | Internal error                      |

## curl example

```bash
curl -X POST http://localhost:18892/voice-chat \
  -H "Authorization: Bearer $VOICE_CHAT_API_KEY" \
  -F "audio=@sample.wav" \
  -F "language=auto" \
  -F "voice=robin"
```

Decode audio in Python:

```python
import base64, json

data = json.loads(response_text)
wav = base64.b64decode(data["audio_base64"])
open("reply.wav", "wb").write(wav)
```

## Logs

Each request emits per-stage timings:

```
upload received — 48320 bytes  file=sample.wav  lang=auto  voice=robin  model=openclaw/default
STT 1.23s — 'what time is it'
Chat 0.87s — "I don't have access to real-time data..."
TTS 2.11s — total 4.22s — response 86400 bytes
```
