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
import ctypes
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

import keyboard
import pyperclip
import pystray
import requests
import sounddevice as sd
from dotenv import load_dotenv
from PIL import Image, ImageDraw

load_dotenv(Path(__file__).with_name(".env"))

_local_app_data = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ASR"
CONFIG_FILE = Path(os.getenv("ASR_CONFIG_FILE") or (_local_app_data / "config.json"))
LOG_FILE = Path(os.getenv("ASR_LOG_FILE") or (_local_app_data / "asr-client.log"))
SAMPLE_RATE = int(os.getenv("ASR_SAMPLE_RATE", "16000"))
APP_NAME = "私有语音输入"

DEFAULT_SETTINGS: dict[str, Any] = {
    "url": os.getenv("ASR_URL", "http://127.0.0.1:8080").rstrip("/"),
    "token": os.getenv("ASR_TOKEN", ""),
    "language": os.getenv("ASR_LANGUAGE", "zh"),
    "hotwords": os.getenv("ASR_HOTWORDS", ""),
    "request_timeout": float(os.getenv("ASR_REQUEST_TIMEOUT", "900")),
    "enable_correction": os.getenv("ASR_ENABLE_CORRECTION", "true").lower()
    in ("1", "true", "yes", "on"),
    "record_hotkey": os.getenv("ASR_RECORD_HOTKEY", "ctrl+alt+space"),
    "correction_hotkey": os.getenv("ASR_CORRECTION_HOTKEY", "ctrl+alt+p"),
    "undo_hotkey": os.getenv("ASR_UNDO_HOTKEY", "ctrl+alt+backspace"),
    "start_minimized": os.getenv("ASR_START_MINIMIZED", "false").lower()
    in ("1", "true", "yes", "on"),
}

recording = False
chunks: list[bytes] = []
stream = None
_settings_lock = threading.RLock()
_recording_lock = threading.Lock()
_injection_lock = threading.Lock()
_ui_events: queue.Queue[tuple[str, Any]] = queue.Queue()
_last_injection_available = False
_instance_mutex = None
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
if _kernel32:
    _kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    _kernel32.CreateMutexW.restype = ctypes.c_void_p
    _kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)


def _acquire_single_instance() -> bool:
    """Keep only one client process per signed-in Windows session."""
    global _instance_mutex
    if os.name != "nt":
        return True
    _instance_mutex = _kernel32.CreateMutexW(None, False, "Local\\PrivateASR.Client.Singleton")
    if not _instance_mutex:
        raise ctypes.WinError()
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        _kernel32.CloseHandle(_instance_mutex)
        _instance_mutex = None
        return False
    return True


def _release_single_instance() -> None:
    global _instance_mutex
    if _instance_mutex and os.name == "nt":
        _kernel32.CloseHandle(_instance_mutex)
        _instance_mutex = None


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

    for key in ("url", "token", "language", "hotwords", "record_hotkey",
                "correction_hotkey", "undo_hotkey"):
        settings[key] = str(settings[key]).strip()
    settings["url"] = settings["url"].rstrip("/")
    try:
        settings["request_timeout"] = float(settings["request_timeout"])
    except (TypeError, ValueError):
        settings["request_timeout"] = DEFAULT_SETTINGS["request_timeout"]
    if not isinstance(settings["enable_correction"], bool):
        settings["enable_correction"] = str(settings["enable_correction"]).lower() in (
            "1", "true", "yes", "on")
    if not isinstance(settings["start_minimized"], bool):
        settings["start_minimized"] = str(settings["start_minimized"]).lower() in (
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
    global _last_injection_available
    if not text:
        _log("inject_skipped", reason="empty_text")
        _notify("toast", ("没有识别到文字", "warning"))
        return
    old = None
    with _injection_lock:
        try:
            old = pyperclip.paste()
            pyperclip.copy(text)
            keyboard.send("ctrl+v")
            time.sleep(0.35)
            _last_injection_available = True
            _log("inject_succeeded", text_length=len(text))
            _notify("toast", ("文字已输入", "success"))
        except Exception as exc:
            _log("inject_error", error=f"{type(exc).__name__}: {exc}")
            _notify("status", f"粘贴失败：{exc}")
            _notify("toast", ("文字输入失败", "error"))
        finally:
            if old is not None:
                try:
                    time.sleep(0.15)
                    pyperclip.copy(old)
                except Exception as exc:
                    _log("clipboard_restore_error", error=f"{type(exc).__name__}: {exc}")


def undo_last_injection() -> None:
    """Undo the most recent successful paste once in the focused application."""
    global _last_injection_available
    with _injection_lock:
        if not _last_injection_available:
            _notify("toast", ("没有可撤销的语音输入", "warning"))
            return
        try:
            keyboard.send("ctrl+z")
            _last_injection_available = False
            _log("injection_undone")
            _notify("status", "已撤销最近一次语音输入")
            _notify("toast", ("已撤销语音输入", "success"))
        except Exception as exc:
            _log("undo_error", error=f"{type(exc).__name__}: {exc}")
            _notify("toast", ("撤销失败", "error"))


def toggle_correction() -> None:
    current = settings_snapshot()["enable_correction"]
    updated = update_settings(enable_correction=not current)
    _notify("correction", updated["enable_correction"])
    _notify("toast", ("二次润色已开启" if updated["enable_correction"] else "二次润色已关闭",
                      "success"))
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
                _notify("toast", ("麦克风不可用", "error"))
                return
            _log("recording_started", sample_rate=SAMPLE_RATE)
            _notify("recording", True)
            _notify("toast", ("开始录音", "recording"))
            return

        recording = False
        if stream:
            stream.stop()
            stream.close()
            stream = None
        payload = _wav(b"".join(chunks))
        _notify("recording", False)
        _notify("toast", ("录音结束，正在识别", "working"))

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
        _notify("toast", (f"识别失败：服务错误 {status}", "error"))
    except requests.RequestException as exc:
        _log("transcribe_network_error", error=f"{type(exc).__name__}: {exc}")
        _notify("status", f"网络错误：{exc}")
        _notify("toast", ("识别失败：网络错误", "error"))
    except Exception as exc:
        _log("client_error", error=f"{type(exc).__name__}: {exc}")
        _notify("status", f"处理失败：{exc}")
        _notify("toast", ("识别处理失败", "error"))


def start_toggle_thread() -> None:
    threading.Thread(target=toggle_recording, daemon=True).start()


def normalize_hotkey(value: str) -> str:
    names = [name.strip() for name in value.split("+") if name.strip()]
    if not names:
        raise ValueError("快捷键不能为空。")
    normalized = keyboard.get_hotkey_name(names)
    if not normalized:
        raise ValueError("无法识别这个快捷键。")
    return normalized


class HotkeyManager:
    def __init__(self) -> None:
        self._handles: list[Any] = []

    @staticmethod
    def validate(bindings: dict[str, tuple[str, Callable[[], None]]]) -> None:
        normalized: list[str] = []
        for label, (hotkey, _callback_function) in bindings.items():
            if not hotkey.strip():
                raise ValueError(f"{label}不能为空。")
            try:
                keyboard.parse_hotkey(hotkey)
            except Exception as exc:
                raise ValueError(f"{label}格式无效：{hotkey}") from exc
            normalized.append(normalize_hotkey(hotkey))
        if len(normalized) != len(set(normalized)):
            raise ValueError("三个快捷键不能重复。")

    def replace(self, bindings: dict[str, tuple[str, Callable[[], None]]]) -> None:
        self.validate(bindings)
        new_handles: list[Any] = []
        try:
            for _label, (hotkey, callback_function) in bindings.items():
                new_handles.append(keyboard.add_hotkey(hotkey, callback_function))
        except Exception:
            for handle in new_handles:
                keyboard.remove_hotkey(handle)
            raise
        for handle in self._handles:
            keyboard.remove_hotkey(handle)
        self._handles = new_handles

    def clear(self) -> None:
        for handle in self._handles:
            keyboard.remove_hotkey(handle)
        self._handles.clear()


class AsrWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("680x680")
        self.root.minsize(600, 620)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.hotkeys = HotkeyManager()
        self.tray_icon: pystray.Icon | None = None
        self.toast_window: tk.Toplevel | None = None
        self.toast_after_id: str | None = None
        self.exiting = False

        current = settings_snapshot()
        self.url_var = tk.StringVar(value=current["url"])
        self.token_var = tk.StringVar(value=current["token"])
        self.language_var = tk.StringVar(value=current["language"])
        self.hotwords_var = tk.StringVar(value=current["hotwords"])
        self.timeout_var = tk.StringVar(value=str(current["request_timeout"]))
        self.correction_var = tk.BooleanVar(value=current["enable_correction"])
        self.record_hotkey_var = tk.StringVar(value=current["record_hotkey"])
        self.correction_hotkey_var = tk.StringVar(value=current["correction_hotkey"])
        self.undo_hotkey_var = tk.StringVar(value=current["undo_hotkey"])
        self.start_minimized_var = tk.BooleanVar(value=current["start_minimized"])
        self.status_var = tk.StringVar(value="就绪")
        self._capture_hook: Any = None
        self._capture_target: tk.StringVar | None = None
        self._capture_button: ttk.Button | None = None
        self._captured_hotkey: str | None = None
        self._capture_scan_code: int | None = None

        self._configure_style()
        self._build()
        self._apply_hotkeys(current)
        self._start_tray()
        self.root.after(100, self._drain_events)
        if current["start_minimized"]:
            self.root.after(0, self.root.withdraw)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
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

        hotkey_frame = ttk.LabelFrame(outer, text="全局快捷键", padding=(12, 8))
        hotkey_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(14, 5))
        hotkey_frame.columnconfigure(1, weight=1)
        hotkey_rows = (
            ("开始 / 结束录音", self.record_hotkey_var),
            ("切换二次润色", self.correction_hotkey_var),
            ("撤销最近输入", self.undo_hotkey_var),
        )
        for row, (label, variable) in enumerate(hotkey_rows):
            self._add_hotkey_control(hotkey_frame, row, label, variable)

        options = ttk.Frame(outer)
        options.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(12, 8))
        self.correction_check = ttk.Checkbutton(
            options, text="启用二次润色", variable=self.correction_var,
            command=self.correction_changed)
        self.correction_check.grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(options, text="启动后直接进入托盘",
                        variable=self.start_minimized_var).grid(
                            row=0, column=1, sticky="w", padx=(24, 0))

        controls = ttk.Frame(outer)
        controls.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(6, 14))
        controls.columnconfigure(0, weight=1)
        self.record_button = ttk.Button(controls, text="开始录音", style="Primary.TButton",
                                        command=start_toggle_thread)
        self.record_button.grid(row=0, column=0, sticky="w")
        ttk.Button(controls, text="保存设置", command=self.save_form).grid(
            row=0, column=1, padx=(10, 0))

        ttk.Separator(outer).grid(row=9, column=0, columnspan=2, sticky="ew")
        ttk.Label(outer, textvariable=self.status_var, style="Status.TLabel").grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(12, 5))
        result_frame = ttk.Frame(outer)
        result_frame.grid(row=11, column=0, columnspan=2, sticky="nsew")
        result_frame.columnconfigure(0, weight=1)
        result_frame.rowconfigure(0, weight=1)
        self.result_text = tk.Text(result_frame, height=5, wrap="word", relief="solid",
                                   borderwidth=1, padx=8, pady=7, state="disabled")
        self.result_text.grid(row=0, column=0, sticky="nsew")
        result_scrollbar = ttk.Scrollbar(result_frame, orient="vertical",
                                         command=self.result_text.yview)
        result_scrollbar.grid(row=0, column=1, sticky="ns")
        self.result_text.configure(yscrollcommand=result_scrollbar.set)
        outer.rowconfigure(11, weight=1)

        for widget in (self.url_entry, self.token_entry, self.hotwords_entry,
                       self.timeout_spin):
            widget.bind("<Return>", lambda _event: self.save_form())
        self.language_box.bind("<<ComboboxSelected>>", lambda _event: self.save_form(False))

    def _add_hotkey_control(self, parent: ttk.LabelFrame, row: int, label: str,
                            variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                           padx=(0, 12), pady=4)
        ttk.Entry(parent, textvariable=variable, state="readonly").grid(
            row=row, column=1, sticky="ew", pady=4)
        button = ttk.Button(parent, text="录制")
        button.configure(command=lambda: self.start_hotkey_capture(variable, button))
        button.grid(row=row, column=2, padx=(8, 0), pady=4)

    def start_hotkey_capture(self, target: tk.StringVar, button: ttk.Button) -> None:
        self.cancel_hotkey_capture()
        self.hotkeys.clear()
        self._capture_target = target
        self._capture_button = button
        self._captured_hotkey = None
        self._capture_scan_code = None
        button.configure(text="请按键...")
        self.status_var.set("请按下组合键；Esc 取消。支持 F1-F12。")
        pressed_modifiers: set[str] = set()

        def on_key(event: keyboard.KeyboardEvent) -> None:
            name = event.name or ""
            if name == "esc":
                if event.event_type == keyboard.KEY_UP:
                    _notify("hotkey_capture_cancel")
                return
            if keyboard.is_modifier(name):
                modifier = keyboard.get_hotkey_name([name])
                if event.event_type == keyboard.KEY_DOWN:
                    pressed_modifiers.add(modifier)
                else:
                    pressed_modifiers.discard(modifier)
                return
            if event.event_type == keyboard.KEY_DOWN:
                self._captured_hotkey = keyboard.get_hotkey_name(
                    [*pressed_modifiers, name])
                self._capture_scan_code = event.scan_code
            elif (event.event_type == keyboard.KEY_UP
                  and event.scan_code == self._capture_scan_code
                  and self._captured_hotkey):
                _notify("hotkey_captured", self._captured_hotkey)

        try:
            self._capture_hook = keyboard.hook(on_key, suppress=True)
        except Exception as exc:
            self._capture_hook = None
            self._restore_current_hotkeys()
            self._clear_capture_state()
            messagebox.showerror("无法录制快捷键", str(exc))

    def finish_hotkey_capture(self, hotkey: str) -> None:
        target = self._capture_target
        if self._capture_hook is None or target is None:
            return
        self._stop_capture_hook()
        target.set(normalize_hotkey(hotkey))
        self._clear_capture_state()
        self._restore_current_hotkeys()
        self.status_var.set("快捷键已录制，点击“保存设置”后生效。")

    def cancel_hotkey_capture(self) -> None:
        was_capturing = self._capture_hook is not None
        self._stop_capture_hook()
        self._clear_capture_state()
        if was_capturing:
            self._restore_current_hotkeys()
            self.status_var.set("已取消快捷键录制")

    def _stop_capture_hook(self) -> None:
        if self._capture_hook is not None:
            keyboard.unhook(self._capture_hook)
            self._capture_hook = None

    def _clear_capture_state(self) -> None:
        if self._capture_button is not None:
            self._capture_button.configure(text="录制")
        self._capture_target = None
        self._capture_button = None
        self._captured_hotkey = None
        self._capture_scan_code = None

    def _restore_current_hotkeys(self) -> None:
        try:
            self._apply_hotkeys(settings_snapshot())
        except Exception as exc:
            _log("hotkey_restore_error", error=f"{type(exc).__name__}: {exc}")
            self.status_var.set(f"快捷键恢复失败：{exc}")

    @staticmethod
    def _bindings(values: dict[str, Any]) -> dict[str, tuple[str, Callable[[], None]]]:
        return {
            "录音快捷键": (values["record_hotkey"], start_toggle_thread),
            "润色快捷键": (values["correction_hotkey"], toggle_correction),
            "撤销快捷键": (values["undo_hotkey"], undo_last_injection),
        }

    def _apply_hotkeys(self, values: dict[str, Any]) -> None:
        self.hotkeys.replace(self._bindings(values))
        _log("hotkeys_registered", record=values["record_hotkey"],
             correction=values["correction_hotkey"], undo=values["undo_hotkey"])

    def correction_changed(self) -> None:
        enabled = self.correction_var.get()
        update_settings(enable_correction=enabled)
        self.status_var.set("二次润色已开启" if enabled else "二次润色已关闭")
        _log("correction_toggled", enabled=enabled, source="gui")

    def save_form(self, show_confirmation: bool = True) -> bool:
        url = self.url_var.get().strip().rstrip("/")
        try:
            timeout = float(self.timeout_var.get())
            if timeout <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("设置无效", "请求超时必须是大于 0 的数字。")
            return False
        if not url.startswith(("http://", "https://")):
            messagebox.showerror("设置无效", "服务地址必须以 http:// 或 https:// 开头。")
            return False
        changes = {
            "url": url,
            "token": self.token_var.get().strip(),
            "language": self.language_var.get(),
            "hotwords": self.hotwords_var.get().strip(),
            "request_timeout": timeout,
            "enable_correction": self.correction_var.get(),
            "record_hotkey": self.record_hotkey_var.get(),
            "correction_hotkey": self.correction_hotkey_var.get(),
            "undo_hotkey": self.undo_hotkey_var.get(),
            "start_minimized": self.start_minimized_var.get(),
        }
        try:
            for key in ("record_hotkey", "correction_hotkey", "undo_hotkey"):
                changes[key] = normalize_hotkey(changes[key])
            self._apply_hotkeys(changes)
        except ValueError as exc:
            messagebox.showerror("快捷键无效", str(exc))
            return False
        except Exception as exc:
            messagebox.showerror("快捷键注册失败", f"快捷键可能被其他程序占用：{exc}")
            return False
        update_settings(**changes)
        self.url_var.set(url)
        self.record_hotkey_var.set(changes["record_hotkey"])
        self.correction_hotkey_var.set(changes["correction_hotkey"])
        self.undo_hotkey_var.set(changes["undo_hotkey"])
        if show_confirmation:
            self.status_var.set("设置已保存，快捷键已生效")
            self.show_toast("设置已保存", "success")
        _log("config_saved", config_file=str(CONFIG_FILE))
        return True

    def show_toast(self, text: str, kind: str = "success", duration_ms: int = 1800) -> None:
        colors = {
            "recording": "#b42318", "working": "#175cd3", "success": "#067647",
            "warning": "#b54708", "error": "#b42318",
        }
        if self.toast_after_id:
            self.root.after_cancel(self.toast_after_id)
        if self.toast_window:
            self.toast_window.destroy()
        toast = tk.Toplevel(self.root)
        self.toast_window = toast
        toast.overrideredirect(True)
        toast.attributes("-disabled", True)
        toast.attributes("-topmost", True)
        frame = tk.Frame(toast, bg=colors.get(kind, "#344054"), padx=18, pady=12)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=text, bg=frame["bg"], fg="white",
                 font=("Microsoft YaHei UI", 10, "bold")).pack()
        toast.update_idletasks()
        x = toast.winfo_screenwidth() - toast.winfo_reqwidth() - 24
        y = toast.winfo_screenheight() - toast.winfo_reqheight() - 70
        toast.geometry(f"+{x}+{y}")
        self.toast_after_id = self.root.after(duration_ms, self._hide_toast)

    def _hide_toast(self) -> None:
        if self.toast_window:
            self.toast_window.destroy()
            self.toast_window = None
        self.toast_after_id = None

    @staticmethod
    def _tray_image() -> Image.Image:
        image = Image.new("RGBA", (64, 64), (25, 35, 52, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((22, 9, 42, 39), radius=10, fill="white")
        draw.arc((15, 22, 49, 52), 0, 180, fill=(72, 187, 120, 255), width=5)
        draw.line((32, 49, 32, 57), fill=(72, 187, 120, 255), width=5)
        draw.line((23, 57, 41, 57), fill=(72, 187, 120, 255), width=5)
        return image

    def _start_tray(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("显示设置", lambda _icon, _item: self.root.after(0, self.show)),
            pystray.MenuItem("开始 / 结束录音",
                             lambda _icon, _item: start_toggle_thread()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda _icon, _item: self.root.after(0, self.exit)),
        )
        self.tray_icon = pystray.Icon("private-asr", self._tray_image(), APP_NAME, menu)
        self.tray_icon.run_detached()

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_to_tray(self) -> None:
        self.root.withdraw()
        self.show_toast("已在系统托盘后台运行", "working")

    def _drain_events(self) -> None:
        if self.exiting:
            return
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
                elif event == "hotkey_captured":
                    self.finish_hotkey_capture(str(value))
                elif event == "hotkey_capture_cancel":
                    self.cancel_hotkey_capture()
                elif event == "toast":
                    text, kind = value
                    self.show_toast(text, kind)
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

    def exit(self) -> None:
        global recording, stream
        self.exiting = True
        recording = False
        if stream:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            stream = None
        self._stop_capture_hook()
        self.hotkeys.clear()
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.destroy()


def main() -> None:
    if not _acquire_single_instance():
        ctypes.windll.user32.MessageBoxW(
            None, "私有语音输入已经在系统托盘中运行。", APP_NAME, 0x40)
        return
    _log("client_started", endpoint=settings_snapshot()["url"],
         log_file=str(LOG_FILE), config_file=str(CONFIG_FILE), gui=True)
    root = tk.Tk()
    try:
        AsrWindow(root)
    except Exception as exc:
        _log("startup_error", error=f"{type(exc).__name__}: {exc}")
        messagebox.showerror("启动失败", str(exc))
        root.destroy()
        return
    try:
        root.mainloop()
    finally:
        _release_single_instance()


if __name__ == "__main__":
    main()
