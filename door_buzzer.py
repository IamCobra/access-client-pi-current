import threading
import time

from gpiozero import PWMOutputDevice


BUZZER_GPIO = 26
VOLUME = 0.06

buzzer = PWMOutputDevice(BUZZER_GPIO, frequency=1000)
_sound_lock = threading.Lock()
_alarm_event = threading.Event()
_close_event = threading.Event()


def _tone(frequency, duration, volume=VOLUME):
    with _sound_lock:
        if _close_event.is_set():
            return

        buzzer.frequency = frequency
        buzzer.value = volume
        time.sleep(duration)
        buzzer.off()


def keypress():
    _tone(950, 0.025, 0.04)


def success():
    _tone(850, 0.07)
    time.sleep(0.04)
    _tone(1250, 0.08)


def error():
    _tone(350, 0.12)


def _alarm_loop():
    while not _close_event.is_set():
        if _alarm_event.is_set():
            _tone(520, 0.12, 0.018)
            _close_event.wait(0.25)
        else:
            _close_event.wait(0.05)


_alarm_thread = threading.Thread(target=_alarm_loop, daemon=True)
_alarm_thread.start()


def start_alarm():
    _alarm_event.set()


def stop_alarm():
    _alarm_event.clear()

    with _sound_lock:
        buzzer.off()


def close():
    _alarm_event.clear()
    _close_event.set()
    _alarm_thread.join(timeout=1)

    with _sound_lock:
        buzzer.off()
        buzzer.close()
