import time

from gpiozero import Button, DigitalOutputDevice


KEYS = [
    ["1", "2", "3", "A"],
    ["4", "5", "6", "B"],
    ["7", "8", "9", "C"],
    ["0", "F", "E", "D"],
]


class PmodKeypad:
    def __init__(self):
        # COL1, COL2, COL3, COL4
        self.columns = [
            DigitalOutputDevice(23, initial_value=True),
            DigitalOutputDevice(22, initial_value=True),
            DigitalOutputDevice(27, initial_value=True),
            DigitalOutputDevice(17, initial_value=True),
        ]

        # ROW1, ROW2, ROW3, ROW4
        self.rows = [
            Button(6, pull_up=True),
            Button(5, pull_up=True),
            Button(25, pull_up=True),
            Button(24, pull_up=True),
        ]

    def run(self, stop_event, on_key):
        last_key = None

        while not stop_event.is_set():
            detected = None

            for column_number, column in enumerate(self.columns):
                column.off()
                time.sleep(0.002)

                for row_number, row in enumerate(self.rows):
                    if row.is_pressed:
                        detected = KEYS[row_number][column_number]
                        break

                column.on()

                if detected:
                    break

            if detected and detected != last_key:
                on_key(detected)

            last_key = detected
            time.sleep(0.02)

    def close(self):
        for column in self.columns:
            column.close()

        for row in self.rows:
            row.close()
