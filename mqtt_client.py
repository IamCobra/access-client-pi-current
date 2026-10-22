import getpass
import json
import pathlib
import ssl
import threading
import time
from datetime import datetime, timezone

from gpiozero import DigitalInputDevice
from smbus2 import SMBus
import paho.mqtt.client as mqtt

import door_buzzer
import status_led
from keypad import PmodKeypad
from mfrc522_i2c import MFRC522


HOST = "mqtt.ikkeigen.dk"
PORT = 443
USERNAME = "ikkeigen"

DOOR_SENSOR_GPIO = 16
RFID_ADDRESS = 0x28
PIN_LENGTH = 6
PIN_TIMEOUT = 15
SERVER_TIMEOUT = 5
OPEN_ALARM_DELAY = 30

DEVICE_UUID_FILE = pathlib.Path("device_uuid.txt")
LOCK_STATE_FILE = pathlib.Path("lock_state.txt")

device_uuid = DEVICE_UUID_FILE.read_text().strip()
password = getpass.getpass("MQTT-adgangskode: ")
base = f"devices/{device_uuid}"

mqtt_connected = False
stop_event = threading.Event()
state_lock = threading.RLock()


class LinuxI2C:
    def __init__(self, bus_number):
        self.bus = SMBus(bus_number)

    def readfrom_mem(self, address, register, length):
        if length == 1:
            return bytes([self.bus.read_byte_data(address, register)])

        return bytes(
            self.bus.read_i2c_block_data(address, register, length)
        )

    def writeto_mem(self, address, register, data):
        self.bus.write_byte_data(address, register, data[0])

    def close(self):
        self.bus.close()


def load_locked_state():
    try:
        return LOCK_STATE_FILE.read_text().strip() != "unlocked"
    except FileNotFoundError:
        return True


def save_locked_state():
    LOCK_STATE_FILE.write_text("locked" if door_locked else "unlocked")


door_sensor = DigitalInputDevice(
    DOOR_SENSOR_GPIO,
    pull_up=False,
    bounce_time=0.1,
)

i2c = LinuxI2C(1)
rfid = MFRC522(i2c, RFID_ADDRESS)
keypad = PmodKeypad()

door_locked = load_locked_state()
door_open = not door_sensor.is_active
door_open_since = time.monotonic() if door_open else None
stay_open_allowed = True
alarm_suppressed_until = None
alarm_active = False

pending_card_uuid = None
entered_pin = ""
pin_attempts = 0
pin_deadline = None
pin_submitted = False
server_deadline = None


def play_sound(sound):
    threading.Thread(target=sound, daemon=True).start()


def update_led():
    with state_lock:
        if alarm_active:
            state = "alarm"
        elif pending_card_uuid is not None:
            state = "waiting_pin"
        elif door_locked:
            state = "locked"
        elif door_open:
            state = "unlocked_open"
        else:
            state = "unlocked_closed"

    status_led.set_state(state)


def send_door_state(state):
    if not mqtt_connected:
        return

    client.publish(f"{base}/door/{state}", json.dumps({}), qos=1)
    print(f"Dørstatus sendt: {state}")


def report_lock_state():
    if not mqtt_connected:
        return

    state = "lock" if door_locked else "unlock"
    client.publish(f"{base}/door/{state}", json.dumps({}), qos=1)
    print(f"Låsestatus sendt: {state}")


def parse_timestamp(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        print(f"Ugyldigt alarmtidspunkt fra backend: {value}")
        return None


def alarm_is_suppressed():
    with state_lock:
        until = alarm_suppressed_until

    return until is not None and datetime.now(timezone.utc) < until


def start_alarm(reason):
    global alarm_active

    if alarm_is_suppressed():
        print(f"Alarm undertrykt: {reason}")
        return

    with state_lock:
        if alarm_active:
            return
        alarm_active = True

    update_led()
    door_buzzer.start_alarm()

    if mqtt_connected:
        client.publish(
            f"{base}/alarm/start",
            json.dumps({"type": reason}),
            qos=1,
        )

    print(f"Alarm startet: {reason}")


def resolve_alarm(report=True):
    global alarm_active

    with state_lock:
        was_active = alarm_active
        alarm_active = False

    door_buzzer.stop_alarm()
    update_led()

    if report and was_active and mqtt_connected:
        client.publish(
            f"{base}/alarm/resolve",
            json.dumps({}),
            qos=1,
        )

    if was_active:
        print("Alarm løst")


def reset_pin():
    global pending_card_uuid, entered_pin, pin_attempts
    global pin_deadline, pin_submitted, server_deadline

    with state_lock:
        pending_card_uuid = None
        entered_pin = ""
        pin_attempts = 0
        pin_deadline = None
        pin_submitted = False
        server_deadline = None

    update_led()


def start_pin_entry(card_uuid):
    global pending_card_uuid, entered_pin, pin_attempts
    global pin_deadline, pin_submitted, server_deadline

    with state_lock:
        pending_card_uuid = card_uuid
        entered_pin = ""
        pin_attempts = 0
        pin_deadline = time.monotonic() + PIN_TIMEOUT
        pin_submitted = False
        server_deadline = None

    update_led()
    play_sound(door_buzzer.keypress)
    print("PIN kræves: indtast 6 cifre. F rydder.")


def reject_attempt(message, cancel=False):
    global entered_pin, pin_attempts, pin_deadline
    global pin_submitted, server_deadline, pending_card_uuid

    with state_lock:
        if pending_card_uuid is None:
            cancel = True
        else:
            pin_attempts += 1
            cancel = cancel or pin_attempts >= 3

        attempts = pin_attempts
        entered_pin = ""
        pin_submitted = False
        server_deadline = None

        if cancel:
            pending_card_uuid = None
            pin_deadline = None
        else:
            pin_deadline = time.monotonic() + PIN_TIMEOUT

    update_led()
    status_led.flash_error()
    play_sound(door_buzzer.error)

    if cancel:
        print(f"{message}. Scan kortet igen.")
    else:
        print(f"{message}. Forsøg {attempts} af 3.")


def set_locked(locked, report=True):
    global door_locked

    with state_lock:
        if locked and door_open:
            print("Døren er åben og kan derfor ikke låses")
            status_led.flash_error()
            play_sound(door_buzzer.error)
            return False

        changed = door_locked != locked
        door_locked = locked
        save_locked_state()

    if not locked:
        resolve_alarm(report=True)

    update_led()

    if report:
        report_lock_state()

    if changed:
        play_sound(door_buzzer.success)
        print("Døren er nu låst" if locked else "Døren er nu låst op")

    return True


def toggle_lock():
    with state_lock:
        currently_locked = door_locked

    set_locked(not currently_locked, report=True)


def handle_key(key):
    global entered_pin, pin_deadline, pin_submitted, server_deadline

    topic = None
    payload = None

    with state_lock:
        if pending_card_uuid is None or pin_submitted:
            return

        if key == "F":
            entered_pin = ""
            pin_deadline = time.monotonic() + PIN_TIMEOUT
            print("PIN ryddet")
            return

        if not key.isdigit():
            return

        entered_pin += key
        pin_deadline = time.monotonic() + PIN_TIMEOUT
        print(f"PIN: {'*' * len(entered_pin)}")
        play_sound(door_buzzer.keypress)

        if len(entered_pin) == PIN_LENGTH:
            topic = (
                f"{base}/door/card/"
                f"{pending_card_uuid}/check-pin"
            )
            payload = {"pin": entered_pin}
            pin_submitted = True
            server_deadline = time.monotonic() + SERVER_TIMEOUT

    if topic:
        client.publish(topic, json.dumps(payload), qos=1)
        print("PIN sendt til backend")


def handle_door_change(opened):
    global door_open, door_open_since

    with state_lock:
        door_open = opened
        door_open_since = time.monotonic() if opened else None
        locked_now = door_locked
        alarm_was_active = alarm_active

    send_door_state("open" if opened else "closed")
    update_led()

    if not opened:
        # Alarmen forbliver aktiv, men lyden stopper, når døren lukkes.
        door_buzzer.stop_alarm()
    elif locked_now:
        # En uløst alarm begynder at lyde igen, hvis døren åbnes igen.
        if alarm_was_active:
            door_buzzer.start_alarm()
        else:
            start_alarm("forced_open")


def send_card(uid):
    if not mqtt_connected:
        print("Kort fundet, men MQTT er ikke forbundet")
        return

    reset_pin()
    # Kort bip betyder kun, at RFID-kortet er blevet aflæst.
    play_sound(door_buzzer.keypress)
    payload = {"card_uid": uid}
    client.publish(
        f"{base}/door/check-card",
        json.dumps(payload),
        qos=1,
    )
    print(f"Kort sendt til backend: {uid}")


def rfid_loop():
    active_uid = None
    absent_since = None

    while not stop_event.is_set():
        try:
            status, _, _ = rfid.scan()

            if status == rfid.MIFARE_OK:
                status, uid_data, _ = rfid.identify()

                if status == rfid.MIFARE_OK:
                    uid_bytes = uid_data[:-1]

                    if uid_bytes and uid_bytes[0] == 0x88:
                        print("Langt UID understøttes ikke endnu")
                        time.sleep(1)
                        continue

                    uid = ":".join(
                        f"{byte:02X}" for byte in uid_bytes
                    )
                    absent_since = None

                    if uid != active_uid:
                        active_uid = uid
                        send_card(uid)

            elif active_uid is not None:
                if absent_since is None:
                    absent_since = time.monotonic()
                elif time.monotonic() - absent_since >= 0.5:
                    active_uid = None
                    absent_since = None

        except OSError as error:
            print(f"RFID I²C-fejl: {error}")
            time.sleep(1)

        time.sleep(0.1)


def keypad_loop():
    keypad.run(stop_event, handle_key)


def state_monitor_loop():
    while not stop_event.wait(0.1):
        now = time.monotonic()
        pin_timeout = False
        server_timeout = False
        open_too_long = False
        forced_open = False

        with state_lock:
            if (
                pending_card_uuid is not None
                and not pin_submitted
                and pin_deadline is not None
                and now >= pin_deadline
            ):
                pin_timeout = True

            if (
                pending_card_uuid is not None
                and pin_submitted
                and server_deadline is not None
                and now >= server_deadline
            ):
                server_timeout = True

            if (
                door_open
                and not door_locked
                and not stay_open_allowed
                and not alarm_active
                and door_open_since is not None
                and now - door_open_since >= OPEN_ALARM_DELAY
            ):
                open_too_long = True

            if door_open and door_locked and not alarm_active:
                forced_open = True

        if pin_timeout:
            reset_pin()
            status_led.flash_error()
            play_sound(door_buzzer.error)
            print("PIN-tiden udløb. Scan kortet igen.")

        if server_timeout:
            reset_pin()
            status_led.flash_error()
            play_sound(door_buzzer.error)
            print("Backend svarede ikke inden for 5 sekunder")

        if open_too_long:
            start_alarm("open_too_long")

        if forced_open:
            start_alarm("forced_open")


def on_connect(client, userdata, flags, reason_code, properties):
    global mqtt_connected

    if reason_code.is_failure:
        print(f"MQTT-forbindelsen blev afvist: {reason_code}")
        return

    mqtt_connected = True
    print("Forbundet til MQTT-serveren")

    topics = [
        f"{base}/connected",
        f"{base}/alarm/resolve",
        f"{base}/card-success",
        f"{base}/card-error",
        f"{base}/wrong-pin",
        f"{base}/unlock",
        f"{base}/door",
        f"{base}/lock",
        f"{base}/error",
    ]

    for topic in topics:
        client.subscribe(topic, qos=1)

    client.publish(f"{base}/connect", json.dumps({}), qos=1)
    print("Online-status sendt")

    send_door_state("open" if door_open else "closed")
    report_lock_state()
    update_led()

    if door_locked and door_open:
        start_alarm("forced_open")


def on_message(client, userdata, message):
    global stay_open_allowed, alarm_suppressed_until

    payload = message.payload.decode("utf-8", errors="replace")
    print(f"Modtaget på {message.topic}: {payload}")

    try:
        data = json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        data = {}

    if message.topic == f"{base}/card-success":
        with state_lock:
            finishing_pin = pin_submitted
            currently_locked = door_locked

        if finishing_pin:
            reset_pin()
            toggle_lock()
        elif not currently_locked:
            # Genlåsning kræver et godkendt kort, men ingen ny PIN.
            reset_pin()
            set_locked(True, report=True)
        elif data.get("requires_pin"):
            card_uuid = data.get("card_uuid")

            if card_uuid:
                start_pin_entry(card_uuid)
            else:
                reset_pin()
                status_led.flash_error()
                play_sound(door_buzzer.error)
                print("Backend sendte ikke kortets UUID")
        else:
            reset_pin()
            toggle_lock()

    elif message.topic == f"{base}/wrong-pin":
        reject_attempt("Forkert PIN")

    elif message.topic == f"{base}/card-error":
        reset_pin()
        status_led.flash_error()
        play_sound(door_buzzer.error)

    elif message.topic == f"{base}/unlock":
        reset_pin()
        set_locked(False, report=True)

    elif message.topic == f"{base}/lock":
        reset_pin()
        set_locked(True, report=True)

    elif message.topic == f"{base}/alarm/resolve":
        resolve_alarm(report=False)

    elif message.topic == f"{base}/door":
        with state_lock:
            stay_open_allowed = bool(
                data.get("stay_open_allowed", False)
            )
            alarm_suppressed_until = parse_timestamp(
                data.get("alarm_suppressed_until")
            )

        print(
            "Døren må stå åben"
            if stay_open_allowed
            else "Alarm efter 30 sekunders åben dør"
        )

    elif message.topic == f"{base}/error":
        status_led.flash_error()
        play_sound(door_buzzer.error)


def on_disconnect(
    client,
    userdata,
    disconnect_flags,
    reason_code,
    properties,
):
    global mqtt_connected
    mqtt_connected = False
    print(f"MQTT-forbindelsen blev afbrudt: {reason_code}")


client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id=f"access-client-{device_uuid}",
    transport="websockets",
)

client.username_pw_set(USERNAME, password)
client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
client.ws_set_options(path="/mqtt")

client.on_connect = on_connect
client.on_message = on_message
client.on_disconnect = on_disconnect
client.reconnect_delay_set(min_delay=1, max_delay=30)

door_sensor.when_activated = lambda: handle_door_change(False)
door_sensor.when_deactivated = lambda: handle_door_change(True)

threads = [
    threading.Thread(target=rfid_loop, daemon=True),
    threading.Thread(target=keypad_loop, daemon=True),
    threading.Thread(target=state_monitor_loop, daemon=True),
]

update_led()
print(f"Starter klient for enhed: {device_uuid}")
print(f"RFID-version: {rfid.getReaderVersion()}")
print("Scan et kort, eller stop med Ctrl+C.")
print("Gemt tilstand: låst" if door_locked else "Gemt tilstand: låst op")

try:
    for thread in threads:
        thread.start()

    client.connect(HOST, PORT, keepalive=30)
    client.loop_forever(retry_first_connection=True)

except KeyboardInterrupt:
    print("\nStopper klienten")

finally:
    mqtt_connected = False
    stop_event.set()

    for thread in threads:
        thread.join(timeout=2)

    keypad.close()
    door_sensor.close()
    door_buzzer.close()
    status_led.close()
    i2c.close()
    client.disconnect()
