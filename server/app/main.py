"""ASR HTTP service: FunASR first, faster-whisper fallback."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

MAX_BYTES = int(os.getenv("ASR_MAX_BYTES", str(25 * 1024 * 1024)))
API_TOKEN = os.getenv("ASR_API_TOKEN", "")
GPU_URL = os.getenv("ASR_GPU_URL", "").rstrip("/")
ENGINE_NAME = os.getenv("ASR_ENGINE", "auto").lower()
MODEL = os.getenv("ASR_MODEL", "iic/SenseVoiceSmall")
WHISPER_MODEL = os.getenv("ASR_WHISPER_MODEL", "small")
DEVICE = os.getenv("ASR_DEVICE", "cpu")
COMPUTE_TYPE = os.getenv("ASR_COMPUTE_TYPE", "int8")
FUNASR_VAD_MODEL = os.getenv("ASR_FUNASR_VAD_MODEL", "fsmn-vad")
FUNASR_MAX_SEGMENT_MS = int(os.getenv("ASR_FUNASR_MAX_SEGMENT_MS", "30000"))
WHISPER_BEAM_SIZE = int(os.getenv("ASR_WHISPER_BEAM_SIZE", "5"))
DEFAULT_CORRECTOR_PROMPT = (
    "Correct ASR punctuation and obvious technical terms. Preserve code, English "
    "identifiers, and meaning. Return only corrected text. 如果输入是大于一百字的长文本，"
    "请理解原意后在不偏离原意太多和不增加太多篇幅的前提下，对文本进行重写润色，"
    "确保语义通顺、逻辑清晰、表达自然。"
)
CORRECTOR_URL = os.getenv("ASR_CORRECTOR_URL", "").rstrip("/")
CORRECTOR_MODEL = os.getenv("ASR_CORRECTOR_MODEL", "")
_configured_models = os.getenv("ASR_CORRECTOR_MODELS", "") or CORRECTOR_MODEL
CORRECTOR_MODELS = tuple(dict.fromkeys(
    model.strip() for model in _configured_models.split(",") if model.strip()
))
CORRECTOR_KEY = os.getenv("ASR_CORRECTOR_KEY", "")
CORRECTOR_PROMPT = os.getenv("ASR_CORRECTOR_PROMPT", "").strip() or DEFAULT_CORRECTOR_PROMPT
CORRECTOR_TIMEOUT = float(os.getenv("ASR_CORRECTOR_TIMEOUT", "60"))
CORRECTOR_PROBE_INTERVAL = float(os.getenv("ASR_CORRECTOR_PROBE_INTERVAL", "300"))
CORRECTOR_TEMPERATURE = float(os.getenv("ASR_CORRECTOR_TEMPERATURE", "0"))
CORRECTOR_FORCE_IPV4 = os.getenv("ASR_CORRECTOR_FORCE_IPV4", "false").lower() in (
    "1", "true", "yes", "on")
INFERENCE_TIMEOUT = float(os.getenv("ASR_INFERENCE_TIMEOUT", "300"))
GPU_TIMEOUT = float(os.getenv("ASR_GPU_TIMEOUT", "180"))
DEFAULT_LANGUAGE = os.getenv("ASR_DEFAULT_LANGUAGE", "zh")
REMOVE_FILLERS = os.getenv("ASR_REMOVE_FILLERS", "true").lower() in ("1", "true", "yes", "on")

app = FastAPI(title="Private ASR", version="1.0")
logger = logging.getLogger(__name__)
_engine: Any = None
_engine_name = "unloaded"
_engine_lock = threading.Lock()
_corrector_state_lock = asyncio.Lock()
_corrector_preferred_model: str | None = None
_corrector_next_probe_at = 0.0


def _authorized(token: str | None) -> bool:
    return bool(API_TOKEN) and token == API_TOKEN


def _load_engine() -> Any:
    global _engine, _engine_name
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        errors: list[str] = []
        if ENGINE_NAME in ("auto", "funasr"):
            try:
                from funasr import AutoModel  # type: ignore
                _engine = AutoModel(model=MODEL, device=DEVICE, disable_update=True,
                                    vad_model=FUNASR_VAD_MODEL,
                                    vad_kwargs={"max_single_segment_time":
                                                FUNASR_MAX_SEGMENT_MS})
                _engine_name = "funasr"
                return _engine
            except Exception as exc:  # optional dependency/model/network failure
                errors.append(f"funasr: {exc}")
                if ENGINE_NAME == "funasr":
                    raise RuntimeError("; ".join(errors))
        try:
            from faster_whisper import WhisperModel  # type: ignore
            _engine = WhisperModel(WHISPER_MODEL, device=DEVICE,
                                   compute_type=COMPUTE_TYPE)
            _engine_name = "faster-whisper"
            return _engine
        except Exception as exc:
            errors.append(f"faster-whisper: {exc}")
            raise RuntimeError("No ASR engine available: " + "; ".join(errors))


def _transcribe(path: str, language: str | None, hotwords: str | None) -> str:
    engine = _load_engine()
    if _engine_name == "funasr":
        result = engine.generate(input=path, language=language or "auto",
                                 hotword=hotwords or "")
        if isinstance(result, list) and result:
            return str(result[0].get("text", "")).strip()
        return str(result).strip()
    whisper_language = None if not language or language.lower() == "auto" else language.lower()
    segments, _ = engine.transcribe(path, language=whisper_language,
                                    beam_size=WHISPER_BEAM_SIZE,
                                    vad_filter=True, condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segments).strip()


def _simplify(text: str) -> str:
    """Convert Traditional Chinese to Simplified while preserving English/code."""
    try:
        from opencc import OpenCC  # type: ignore
        return OpenCC("t2s").convert(text)
    except Exception:
        return text


def _remove_fillers(text: str) -> str:
    """Remove obvious standalone hesitation sounds without deleting normal words."""
    if not REMOVE_FILLERS:
        return text
    # Repeated hesitation sounds are safe to remove; single "啊/嗯" can be meaningful.
    text = re.sub(r"(?:呃|额|嗯|唔){2,}", "", text)
    # Remove a leading hesitation and its following pause punctuation.
    text = re.sub(r"^(?:呃|额|嗯|唔)[，,、\s]+", "", text)
    # Remove hesitation after a clear pause, but keep words such as "就是" intact.
    text = re.sub(r"([，,。.!！?？；;、\s])(?:呃|额|嗯|唔)[，,、\s]+", r"\1", text)
    text = re.sub(r"([，,。.!！?？；;、])\s*([，,。.!！?？；;、])", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _remove_engine_tokens(text: str) -> str:
    """Remove metadata tokens emitted by SenseVoice/FunASR."""
    # SenseVoice prefixes results with tokens such as <|zh|> and
    # <|NEUTRAL|>. They are engine metadata, not speech to paste.
    return re.sub(r"<\|[^<>|]{1,64}\|>", "", text)


async def _correct(text: str, enabled: bool = True) -> tuple[str, str]:
    """Optional correction; disabled unless a trusted OpenAI-compatible URL is set."""
    text = _remove_engine_tokens(text)
    text = _simplify(_remove_fillers(re.sub(r"[ \t]+", " ", text).strip()))
    if not text:
        return text, "skipped"
    if not enabled:
        return text, "disabled"
    if not CORRECTOR_URL or not CORRECTOR_MODELS:
        return text, "unconfigured"
    headers = {"content-type": "application/json"}
    if CORRECTOR_KEY:
        headers["authorization"] = f"Bearer {CORRECTOR_KEY}"
    async def request_model(client: httpx.AsyncClient, model: str) -> str:
        body = {"model": model, "temperature": CORRECTOR_TEMPERATURE,
                "messages": [{"role": "system", "content": CORRECTOR_PROMPT},
                             {"role": "user", "content": text}]}
        response = await client.post(f"{CORRECTOR_URL}/chat/completions",
                                     json=body, headers=headers)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return _remove_engine_tokens(str(content)).strip()

    async def race_models(client: httpx.AsyncClient,
                          models: tuple[str, ...]) -> tuple[str, str] | None:
        task_models = {asyncio.create_task(request_model(client, model)): model
                       for model in models}
        pending = set(task_models)
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        corrected = task.result()
                    except Exception as exc:
                        logger.warning("ASR correction model failed: %s: %s",
                                       type(exc).__name__, exc)
                        continue
                    if corrected:
                        return task_models[task], corrected
            return None
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    try:
        transport = (httpx.AsyncHTTPTransport(local_address="0.0.0.0")
                     if CORRECTOR_FORCE_IPV4 else None)
        async with httpx.AsyncClient(timeout=CORRECTOR_TIMEOUT,
                                     transport=transport) as client:
            global _corrector_next_probe_at, _corrector_preferred_model
            now = time.monotonic()
            async with _corrector_state_lock:
                preferred = _corrector_preferred_model
                probe_due = preferred is None or now >= _corrector_next_probe_at
            models = CORRECTOR_MODELS if probe_due else (preferred,)
            result = await race_models(client, models)

            # A failed sticky model triggers an immediate full probe.
            if result is None and not probe_due:
                result = await race_models(client, CORRECTOR_MODELS)
                probe_due = True
            if result is not None:
                winner, corrected = result
                async with _corrector_state_lock:
                    _corrector_preferred_model = winner
                    _corrector_next_probe_at = time.monotonic() + CORRECTOR_PROBE_INTERVAL
                return corrected, "applied"
            return text, "failed"
    except Exception as exc:
        logger.warning("ASR correction failed: %s: %s", type(exc).__name__, exc)
        return text, "failed"


@app.get("/v1/health")
def health() -> dict[str, Any]:
    return {"ok": True, "ready": _engine is not None, "engine": _engine_name,
            "gpu_configured": bool(GPU_URL),
            "corrector_configured": bool(CORRECTOR_URL and CORRECTOR_MODELS)}


@app.post("/v1/transcribe")
async def transcribe(file: UploadFile = File(...),
                     x_asr_token: str | None = Header(default=None),
                     language: str | None = None,
                     hotwords: str | None = None,
                     correct: bool = True,
                     no_fallback: bool = False) -> JSONResponse:
    if not _authorized(x_asr_token):
        raise HTTPException(status_code=401, detail="invalid ASR token")
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="audio file is too large")
    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    language = language or DEFAULT_LANGUAGE
    started = time.perf_counter()
    gpu_error: str | None = None
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        if GPU_URL:
            try:
                # GPU_URL is a WireGuard/private endpoint; never send it through the web proxy.
                async with httpx.AsyncClient(timeout=GPU_TIMEOUT, trust_env=False) as client:
                    response = await client.post(
                        f"{GPU_URL}/v1/transcribe",
                        files={"file": (file.filename or "audio.wav", data,
                                file.content_type or "audio/wav")},
                        headers={"x-asr-token": API_TOKEN},
                        params={"language": language, "hotwords": hotwords or ""})
                    response.raise_for_status()
                    payload = response.json()
                    payload["text"], payload["correction"] = await _correct(
                        str(payload.get("text", "")), enabled=correct)
                    payload["route"] = "gpu"
                    return JSONResponse(payload)
            except Exception as exc:
                gpu_error = f"{type(exc).__name__}: {exc}"
                if no_fallback:
                    raise HTTPException(status_code=502, detail=f"GPU backend failed: {exc}")
        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(_transcribe, path, language, hotwords),
                timeout=INFERENCE_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            detail = f"local CPU inference timed out after {INFERENCE_TIMEOUT:g}s"
            if gpu_error:
                detail += f" after GPU attempt ({gpu_error})"
            raise HTTPException(status_code=503,
                                detail=detail) from exc
        except Exception as exc:
            detail = f"local CPU backend failed: {exc}"
            if gpu_error:
                detail += f" after GPU attempt ({gpu_error})"
            raise HTTPException(status_code=503, detail=detail) from exc
        text, correction = await _correct(text, enabled=correct)
        payload = {"text": text, "engine": _engine_name, "route": "cpu",
                   "correction": correction,
                   "duration_ms": round((time.perf_counter()-started)*1000)}
        if gpu_error:
            payload["fallback_reason"] = gpu_error
        return JSONResponse(payload)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
