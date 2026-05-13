"""
Pulsar — background capture daemon.
Takes a screenshot every N seconds when the machine is active.
Run as a daemon via Task Scheduler (setup_autostart.ps1).
"""

import ctypes
import logging
import logging.handlers
import sys
import time
import configparser
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import mss
import psutil
import win32gui
import win32process
from PIL import Image


# ── Single-instance guard ──────────────────────────────────────────────────
# Windows named mutex. Using WinDLL(use_last_error=True) so ctypes captures
# the API's last-error into a thread-local that GetLastError can't clobber.
# The mutex handle is held via a module-level reference for the process lifetime.
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype  = wintypes.HANDLE

_mutex_handle = _kernel32.CreateMutexW(None, False, 'Local\\PulsarCapture.singleton')
if ctypes.get_last_error() == 183:        # ERROR_ALREADY_EXISTS
    sys.exit(0)


def get_active_window() -> tuple[str, str]:
    hwnd = win32gui.GetForegroundWindow()
    title = win32gui.GetWindowText(hwnd)
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        app = psutil.Process(pid).name().replace('.exe', '')
    except Exception:
        app = 'unknown'
    return title, app


def idle_seconds() -> float:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_ulong)]
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
    elapsed_ms = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
    return elapsed_ms / 1000.0


def is_screensaver_active() -> bool:
    SPI_GETSCREENSAVERRUNNING = 0x0072
    running = ctypes.c_bool()
    ctypes.windll.user32.SystemParametersInfoW(SPI_GETSCREENSAVERRUNNING, 0, ctypes.byref(running), 0)
    return running.value


def capture(output_dir: Path, max_width: int, idle_s: int = 0) -> Path:
    title, app = get_active_window()

    with mss.mss() as sct:
        raw = sct.grab(sct.monitors[0])
        img = Image.frombytes('RGB', raw.size, raw.bgra, 'raw', 'BGRX')

    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)

    ts = datetime.now().strftime('%H-%M-%S')
    safe_app   = ''.join(c for c in app[:20]   if c.isalnum() or c in '-_')
    safe_title = ''.join(c for c in title[:50] if c.isalnum() or c in ' -_()').strip()
    # idle suffix `i<seconds>` records seconds since last keyboard/mouse input
    path = output_dir / f'{ts}__{safe_app}__{safe_title}__i{int(idle_s)}.jpg'
    img.save(path, 'JPEG', quality=75, optimize=True)
    return path


def _setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / 'capture.log',
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding='utf-8',
    )
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
        handlers=[handler],
    )
    return logging.getLogger(__name__)


def main():
    cfg = configparser.ConfigParser()
    cfg.read(Path(__file__).parent / 'config.ini')

    interval       = cfg.getint('capture', 'interval_seconds',       fallback=120)
    idle_threshold = cfg.getint('capture', 'idle_threshold_seconds', fallback=300)
    max_width      = cfg.getint('capture', 'max_width_px',           fallback=1280)

    log = _setup_logging(Path(__file__).parent / 'logs')
    log.info('Pulsar capture started — interval %ds, idle threshold %ds', interval, idle_threshold)

    last_window    = ('', '')
    last_shot_time = 0.0
    was_blocked    = False  # True while screensaver or idle suppresses capture
    pending_window = None   # window we detected a switch to, waiting for debounce
    pending_since  = 0.0

    SWITCH_DEBOUNCE = 3  # seconds to settle on a new window before capturing

    while True:
        try:
            screensaver = is_screensaver_active()
            blocked     = screensaver or idle_seconds() >= idle_threshold

            if blocked:
                if not was_blocked:
                    log.info('paused — %s', 'screensaver' if screensaver else 'idle')
                was_blocked = True
            else:
                if was_blocked:
                    log.info('resumed')
                    was_blocked = False

                current_window   = get_active_window()
                now              = time.monotonic()
                interval_elapsed = (now - last_shot_time) >= interval

                if current_window != last_window:
                    if current_window != pending_window:
                        # New switch target — start (or reset) the debounce timer
                        pending_window = current_window
                        pending_since  = now
                    # else: user returned to the already-pending window; keep existing timer
                elif pending_window is not None:
                    # User came back to the last-screenshot window — cancel pending capture
                    pending_window = None

                debounce_ready = (
                    pending_window is not None
                    and current_window == pending_window
                    and (now - pending_since) >= SWITCH_DEBOUNCE
                )

                if debounce_ready or interval_elapsed:
                    day_dir = Path(__file__).parent / 'logs' / datetime.now().strftime('%Y-%m-%d') / 'screenshots'
                    day_dir.mkdir(parents=True, exist_ok=True)
                    path = capture(day_dir, max_width, int(idle_seconds()))
                    trigger = 'switch' if debounce_ready else 'interval'
                    log.info('[%s] %s', trigger, path.name)
                    last_window    = current_window
                    last_shot_time = now
                    pending_window = None

        except Exception as exc:
            log.error('%s', exc)

        time.sleep(1)


if __name__ == '__main__':
    main()
