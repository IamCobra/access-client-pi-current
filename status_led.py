import threading
import time

from gpiozero import RGBLED


led = RGBLED(
    red=12,
    green=13,
    blue=19,
    pwm=True,
)

COLORS = {
    "off": (0, 0, 0),
    "red": (0.12, 0, 0),
    "green": (0, 0.12, 0),
}

VALID_STATES = {
    "locked",
    "unlocked_closed",
    "unlocked_open",
    "waiting_pin",
    "alarm",
}

_lock = threading.Lock()
_stop_event = threading.Event()
_base_state = "locked"
_error_until = 0.0


def set_state(state):
    global _base_state

    if state not in VALID_STATES:
        raise ValueError(f"Ukendt LED-tilstand: {state}")

    with _lock:
        _base_state = state


def flash_error(seconds=1.2):
    global _error_until

    with _lock:
        _error_until = max(_error_until, time.monotonic() + seconds)


def _blink(now, interval):
    return int(now / interval) % 2 == 0


def _render_loop():
    global _error_until

    while not _stop_event.is_set():
        now = time.monotonic()

        with _lock:
            state = _base_state
            error_active = now < _error_until

            if not error_active and _error_until:
                _error_until = 0.0

        if error_active:
            color = COLORS["red"] if _blink(now, 0.12) else COLORS["off"]
        elif state == "locked":
            color = COLORS["red"]
        elif state == "unlocked_closed":
            color = COLORS["green"]
        elif state == "unlocked_open":
            color = COLORS["green"] if _blink(now, 0.6) else COLORS["off"]
        elif state == "waiting_pin":
            color = COLORS["green"] if _blink(now, 0.15) else COLORS["off"]
        elif state == "alarm":
            color = COLORS["red"] if _blink(now, 0.12) else COLORS["off"]
        else:
            color = COLORS["off"]

        led.color = color
        _stop_event.wait(0.04)


_thread = threading.Thread(target=_render_loop, daemon=True)
_thread.start()


def close():
    _stop_event.set()
    _thread.join(timeout=1)
    led.off()
    led.close()
