"""Windows global voice typing client with a persistent settings GUI."""
from __future__ import annotations

import io
import json
import os
import queue
import threading
import time
import tkinter as tk
import wave
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

import keyboard
import pyperclip
import requests
import sounddevice as sd
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

_local_app_data = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ASR"
CONFIG_FILE = Path(os.getenv("ASR_CONFIG_FILE") or (_local_app_data / "config.json"))
LOG_FILE = Path(os.getenv("ASR_LOG_FILE") or (_local_app_data / "asr-client.log"))
SAMPLE_RATE = int(os.getenv("ASR_SAMPLE_RATE", "16000"))
RECORD_HOTKEY = os.getenv("ASR_RECORD_HOTKEY", "ctrl+alt+space")
CORRECTION_HOTKEY = os.getenv("ASR_CORRECTION_HOTKEY", "ctrl+alt+p")

DEFAULT_SETTINGS: dict[str, Any] = {
    "url": os.getenv("ASR_URL", "http://127.0.0.1:8080").rstrip("/"),
    "token": os.getenv("ASR_TOKEN", ""),
    "language": os.getenv("ASR_LANGUAGE", "zh"),
    "hotwords": os.getenv("ASR_HOTWORDS", ""),
    "request_timeout": float(os.getenv("ASR_REQUEST_TIMEOUT", "900")),
    "enable_correction": os.getenv("ASR_ENABLE_CORRECTION", "true").lower()
    in ("1", "true", "yes", "on"),
}

recording = False
chunks: list[bytes] = []
stream = None
_settings_lock = threading.RLock()
_recording_lock = threading.Lock()
_ui_events: queue.Queue[tuple[str, Any]] = queue.Queue()


def _log(event: str, **fields: Any) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[ASR] cannot write log {LOG_FILE}: {exc}")


def _load_settings() -> dict[str, Any]:
    settings = DEFAULT_SETTINGS.copy()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key in settings:
                if key in data:
                    settings[key] = data[key]
    except FileNotFoundError:
        pass
    except Exception as exc:
        _log("config_load_error", error=f"{type(exc).__name__}: {exc}")

    settings["url"] = str(settings["url"]).rstrip("/")
    settings["token"] = str(settings["token"])
    settings["language"] = str(settings["language"])
    settings["hotwords"] = str(settings["hotwords"])
    try:
        settings["request_timeout"] = float(settings["request_timeout"])
    except (TypeError, ValueError):
        settings["request_timeout"] = DEFAULT_SETTINGS["request_timeout"]
    if not isinstance(settings["enable_correction"], bool):
        settings["enable_correction"] = str(settings["enable_correction"]).lower() in (
            "1", "true", "yes", "on")
    return settings


settings = _load_settings()


def _save_settings_locked() -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(CONFIG_FILE)


def update_settings(**changes: Any) -> dict[str, Any]:
    with _settings_lock:
        settings.update(changes)
        _save_settings_locked()
        return settings.copy()


def settings_snapshot() -> dict[str, Any]:
    with _settings_lock:
        return settings.copy()


def _notify(event: str, value: Any = None) -> None:
    _ui_events.put((event, value))


def _callback(indata, frames, timing, status) -> None:
    if recording:
        chunks.append(bytes(indata))


def _wav(data: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(data)
    return output.getvalue()


def _inject(text: str) -> None:
    if not text:
        _log("inject_skipped", reason="empty_text")
        return
    old = None
    try:
        old = pyperclip.paste()
        pyperclip.copy(text)
        keyboard.press("ctrl")
        keyboard.press("v")
        keyboard.release("v")
        keyboard.release("ctrl")
        time.sleep(0.35)
        _log("inject_succeeded", text_length=len(text))
    except Exception as exc:
        _log("inject_error", error=f"{type(exc).__name__}: {exc}")
        _notify("status", f"粘贴失败：{exc}")
    finally:
        if old is not None:
            try:
                time.sleep(0.15)
                pyperclip.copy(old)
            except Exception as exc:
                _log("clipboard_restore_error", error=f"{type(exc).__name__}: {exc}")


def toggle_correction() -> None:
    current = settings_snapshot()["enable_correction"]
    updated = update_settings(enable_correction=not current)
    _notify("correction", updated["enable_correction"])
    _log("correction_toggled", enabled=updated["enable_correction"])


def toggle_recording() -> None:
    global recording, chunks, stream
    with _recording_lock:
        if not recording:
            try:
                chunks = []
                recording = True
                stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1,
                                           dtype="int16", callback=_callback,
                                           blocksize=8000)
                stream.start()
            except Exception as exc:
                recording = False
                stream = None
                _log("microphone_error", error=f"{type(exc).__name__}: {exc}")
                _notify("status", f"麦克风不可用：{exc}")
                return
            _log("recording_started", sample_rate=SAMPLE_RATE)
            _notify("recording", True)
            return

        recording = False
        if stream:
            stream.stop()
            stream.close()
            stream = None
        payload = _wav(b"".join(chunks))
        _notify("recording", False)

    request_settings = settings_snapshot()
    _notify("status", "正在识别...")
    _log("transcribe_started", endpoint=request_settings["url"], bytes=len(payload),
         correction_enabled=request_settings["enable_correction"])
    try:
        response = requests.post(
            f"{request_settings['url']}/v1/transcribe",
            files={"file": ("speech.wav", payload, "audio/wav")},
            headers={"X-ASR-Token": request_settings["token"]},
            params={"language": request_settings["language"],
                    "hotwords": request_settings["hotwords"],
                    "correct": str(request_settings["enable_correction"]).lower()},
            timeout=request_settings["request_timeout"],
        )
        response.raise_for_status()
        body = response.json()
        text = body.get("text", "")
        correction = body.get("correction", "unknown")
        _log("transcribe_succeeded", status=response.status_code,
             route=body.get("route"), engine=body.get("engine"), correction=correction,
             fallback_reason=body.get("fallback_reason"), text_length=len(text))
        _notify("result", {"text": text, "route": body.get("route"),
                           "correction": correction})
        _inject(text)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        detail = exc.response.text[:1000] if exc.response is not None else str(exc)
        _log("transcribe_http_error", status=status, detail=detail)
        _notify("status", f"服务返回错误 {status}")
    except requests.RequestException as exc:
        _log("transcribe_network_error", error=f"{type(exc).__name__}: {exc}")
        _notify("status", f"网络错误：{exc}")
    except Exception as exc:
        _log("client_error", error=f"{type(exc).__name__}: {exc}")
        _notify("status", f"处理失败：{exc}")


def start_toggle_thread() -> None:
    threading.Thread(target=toggle_recording, daemon=True).start()


class AsrWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("私有语音输入")
        self.root.geometry("620x470")
        self.root.minsize(540, 430)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        current = settings_snapshot()
        self.url_var = tk.StringVar(value=current["url"])
        self.token_var = tk.StringVar(value=current["token"])
        self.language_var = tk.StringVar(value=current["language"])
        self.hotwords_var = tk.StringVar(value=current["hotwords"])
        self.timeout_var = tk.StringVar(value=str(current["request_timeout"]))
        self.correction_var = tk.BooleanVar(value=current["enable_correction"])
        self.status_var = tk.StringVar(value="就绪")

        self._configure_style()
        self._build()
        self.root.after(100, self._drain_events)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Status.TLabel", foreground="#266141")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"),
                        padding=(14, 9))

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=20)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(1, weight=1)

        ttk.Label(outer, text="私有语音输入", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 18))

        labels = ("服务地址", "访问令牌", "识别语言", "热词", "请求超时（秒）")
        for row, label in enumerate(labels, start=1):
            ttk.Label(outer, text=label).grid(row=row, column=0, sticky="w",
                                              padx=(0, 14), pady=6)

        self.url_entry = ttk.Entry(outer, textvariable=self.url_var)
        self.url_entry.grid(row=1, column=1, sticky="ew", pady=6)
        self.token_entry = ttk.Entry(outer, textvariable=self.token_var, show="*")
        self.token_entry.grid(row=2, column=1, sticky="ew", pady=6)
        self.language_box = ttk.Combobox(outer, textvariable=self.language_var,
                                         values=("zh", "en", "auto"), state="readonly")
        self.language_box.grid(row=3, column=1, sticky="ew", pady=6)
        self.hotwords_entry = ttk.Entry(outer, textvariable=self.hotwords_var)
        self.hotwords_entry.grid(row=4, column=1, sticky="ew", pady=6)
        self.timeout_spin = ttk.Spinbox(outer, from_=10, to=3600,
                                        textvariable=self.timeout_var)
        self.timeout_spin.grid(row=5, column=1, sticky="ew", pady=6)

        self.correction_check = ttk.Checkbutton(
            outer, text="启用二次润色", variable=self.correction_var,
            command=self.correction_changed)
        self.correction_check.grid(row=6, column=0, columnspan=2, sticky="w", pady=(14, 10))

        controls = ttk.Frame(outer)
        controls.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(8, 18))
        controls.columnconfigure(0, weight=1)
        self.record_button = ttk.Button(controls, text="开始录音", style="Primary.TButton",
                                        command=start_toggle_thread)
        self.record_button.grid(row=0, column=0, sticky="w")
        ttk.Button(controls, text="保存设置", command=self.save_form).grid(
            row=0, column=1, padx=(10, 0))

        ttk.Separator(outer).grid(row=8, column=0, columnspan=2, sticky="ew")
        ttk.Label(outer, textvariable=self.status_var, style="Status.TLabel").grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(15, 6))
        result_frame = ttk.Frame(outer)
        result_frame.grid(row=10, column=0, columnspan=2, sticky="nsew")
        result_frame.columnconfigure(0, weight=1)
        result_frame.rowconfigure(0, weight=1)
        self.result_text = tk.Text(result_frame, height=5, wrap="word", relief="solid",
                                   borderwidth=1, padx=8, pady=7, state="disabled")
        self.result_text.grid(row=0, column=0, sticky="nsew")
        result_scrollbar = ttk.Scrollbar(result_frame, orient="vertical",
                                         command=self.result_text.yview)
        result_scrollbar.grid(row=0, column=1, sticky="ns")
        self.result_text.configure(yscrollcommand=result_scrollbar.set)
        outer.rowconfigure(10, weight=1)

        for widget in (self.url_entry, self.token_entry, self.hotwords_entry,
                       self.timeout_spin):
            widget.bind("<Return>", lambda _event: self.save_form())
        self.language_box.bind("<<ComboboxSelected>>", lambda _event: self.save_form(False))

    def correction_changed(self) -> None:
        enabled = self.correction_var.get()
        update_settings(enable_correction=enabled)
        self.status_var.set("二次润色已开启" if enabled else "二次润色已关闭")
        _log("correction_toggled", enabled=enabled, source="gui")

    def save_form(self, show_confirmation: bool = True) -> None:
        url = self.url_var.get().strip().rstrip("/")
        try:
            timeout = float(self.timeout_var.get())
            if timeout <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("设置无效", "请求超时必须是大于 0 的数字。")
            return
        if not url.startswith(("http://", "https://")):
            messagebox.showerror("设置无效", "服务地址必须以 http:// 或 https:// 开头。")
            return
        update_settings(url=url, token=self.token_var.get().strip(),
                        language=self.language_var.get(),
                        hotwords=self.hotwords_var.get().strip(),
                        request_timeout=timeout,
                        enable_correction=self.correction_var.get())
        self.url_var.set(url)
        if show_confirmation:
            self.status_var.set("设置已保存")
        _log("config_saved", config_file=str(CONFIG_FILE))

    def _drain_events(self) -> None:
        try:
            while True:
                event, value = _ui_events.get_nowait()
                if event == "correction":
                    self.correction_var.set(bool(value))
                    self.status_var.set("二次润色已开启" if value else "二次润色已关闭")
                elif event == "recording":
                    self.record_button.configure(text="结束录音" if value else "开始录音")
                    if value:
                        self.status_var.set("正在录音...")
                elif event == "status":
                    self.status_var.set(str(value))
                elif event == "result":
                    correction = value.get("correction", "unknown")
                    route = value.get("route") or "unknown"
                    correction_label = {
                        "applied": "已应用",
                        "disabled": "已关闭",
                        "failed": "失败，已保留原文",
                        "unconfigured": "未配置",
                        "skipped": "已跳过",
                    }.get(correction, correction)
                    self.status_var.set(f"识别完成 · {route} · 润色{correction_label}")
                    self.result_text.configure(state="normal")
                    self.result_text.delete("1.0", "end")
                    self.result_text.insert("1.0", value.get("text", ""))
                    self.result_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def close(self) -> None:
        global recording, stream
        recording = False
        if stream:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            stream = None
        keyboard.unhook_all_hotkeys()
        self.root.destroy()


def main() -> None:
    _log("client_started", endpoint=settings_snapshot()["url"],
         log_file=str(LOG_FILE), config_file=str(CONFIG_FILE), gui=True)
    keyboard.add_hotkey(RECORD_HOTKEY, start_toggle_thread)
    keyboard.add_hotkey(CORRECTION_HOTKEY, toggle_correction)
    root = tk.Tk()
    AsrWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
