from machine import Pin, I2C, UART, PWM
from utime import sleep_ms, ticks_ms, ticks_diff
import sh1106
import rp2
from rp2 import PIO, StateMachine, asm_pio

import network
import time
import umail


# ============================================================
# GENERAL SETUP
# ============================================================

led = Pin("LED", Pin.OUT)

# ============================================================
# EMAIL ALERT SETTINGS
# ============================================================

WIFI_SSID = "OPPO A3x"
WIFI_PASSWORD = "liverpooldamu"

sender_email = "mitchjaga@gmail.com"
sender_name = "Smart Toilet"
sender_app_password = "rbch zncz bcjx godz"
recipient_email = "mmatheri@andrew.cmu.edu" 

TANK_ALERT_LEVEL_PERCENT = 80

URINE_EMAIL_STATE_FILE = "urine_email_sent.txt"
POOP_EMAIL_STATE_FILE = "poop_email_sent.txt"

urine_email_sent = False
poop_email_sent = False

# ============================================================
# TANK LEVEL SETTINGS
# ============================================================
# Larger distance = emptier tank
# Smaller distance = fuller tank
#
# Urine tank:
#   500 mm = empty = 0%
#   40 mm  = full  = 100%
#
# Poop tank:
#   800 mm = empty = 0%
#   40 mm  = full  = 100%
# ============================================================

URINE_EMPTY_LEVEL_MM = 500
POOP_EMPTY_LEVEL_MM = 800
FULL_LEVEL_MM = 40

# Ultrasonic SENSOR STABILITY FILTER SETTINGS

STABLE_SAMPLE_COUNT = 6      # Number of readings to compare
STABLE_TOLERANCE_MM = 20     # Allowed variation between readings in mm

# Buffers for recent tank readings
urine_tank_readings = []
poop_tank_readings = []

# Last stable values shown on OLED
stable_urine_tank_distance = None
stable_poop_tank_distance = None

# ============================================================
# SWITCH INPUT SETUP
# Floor switch 1 -> GP2
# Door switch 1  -> GP4
# Lock switch 1  -> GP5
# ============================================================

floor_switch_1 = Pin(2, Pin.IN, Pin.PULL_UP)
door_switch_1 = Pin(4, Pin.IN, Pin.PULL_UP)
lock_switch_1 = Pin(5, Pin.IN, Pin.PULL_UP)

# ============================================================
# USER COUNT SETTINGS
# ============================================================

USER_COUNT_FILE = "user_count.txt"

user_count = 0

previous_door_state = door_switch_1.value()
previous_lock_state = lock_switch_1.value()

# This becomes True after the door changes from OFF to ON.
# Then we wait for the lock to change from OFF to ON.
door_opened_before_lock = False

# ============================================================
# RELAY OUTPUT SETUP
# Water pump relay -> GP0
# Solenoid valve relay -> GP8
# ============================================================

water_pump_relay = Pin(0, Pin.OUT)
solenoid_valve_relay = Pin(8, Pin.OUT)

# Start with both relays OFF
water_pump_relay.value(0)
solenoid_valve_relay.value(0)


# ============================================================
# L293D MOTOR DRIVER SETUP
# L293D 1A    -> Pico GP20
# L293D 2A    -> Pico GP22
# L293D EN1,2 -> Pico GP18 PWM
# ============================================================

motor_1A = Pin(20, Pin.OUT)
motor_2A = Pin(22, Pin.OUT)

motor_enable = PWM(Pin(18))
motor_enable.freq(1000)   # 1 kHz PWM frequency

# Start motor OFF
motor_1A.value(0)
motor_2A.value(0)
motor_enable.duty_u16(0)

# ============================================================
# OLED DISPLAY SETUP
# OLED SDA = GP16
# OLED SCL = GP17
# ============================================================

i2c = I2C(0, sda=Pin(16), scl=Pin(17), freq=400000)
display = sh1106.SH1106_I2C(128, 64, i2c, None, 0x3c)

def read_switches():
    door_state = door_switch_1.value()
    lock_state = lock_switch_1.value()

    print("Door switch 1:", door_state)
    print("Lock switch 1:", lock_state)

def read_switch_states():
    floor_state = floor_switch_1.value()
    door_state = door_switch_1.value()
    lock_state = lock_switch_1.value()

    return floor_state, door_state, lock_state


def format_switch_state(state):
    if state == 0:
        return "ON"
    else:
        return "OFF"


def show_switch_states(floor_state, door_state, lock_state):
    display.fill(0)

    display.text("Switch States", 0, 0)

    display.text("Floor 1:", 0, 18)
    display.text(format_switch_state(floor_state), 75, 18)

    display.text("Door:", 0, 34)
    display.text(format_switch_state(door_state), 75, 34)

    display.text("Lock:", 0, 50)
    display.text(format_switch_state(lock_state), 75, 50)

    display.show()


def print_switch_states(floor_state, door_state, lock_state):
    print("Floor switch 1:", floor_state, "-", format_switch_state(floor_state))
    print("Door switch 1:", door_state, "-", format_switch_state(door_state))
    print("Lock switch 1:", lock_state, "-", format_switch_state(lock_state))
    
def control_relays_from_door_switch():
    door_state = door_switch_1.value()

    if door_state == 0:
        # Door switch is closed/pressed
        water_pump_relay.value(1)
        solenoid_valve_relay.value(1)

        print("Door switch 1: 0")
        print("Water pump relay: ON")
        print("Solenoid valve relay: ON")
    else:
        # Door switch is open/not pressed
        water_pump_relay.value(0)
        solenoid_valve_relay.value(0)

        print("Door switch 1: 1")
        print("Water pump relay: OFF")
        print("Solenoid valve relay: OFF")

MOTOR_QUARTER_SPEED = 16384
MOTOR_HALF_SPEED = 32768   # 50% duty cycle
MOTOR_FULL_SPEED = 65535   # 100% duty cycle
MOTOR_OFF = 0


def motor_stop():
    motor_enable.duty_u16(MOTOR_OFF)
    motor_1A.value(0)
    motor_2A.value(0)
    print("Motor: STOP")


def motor_clockwise():
    motor_1A.value(1)
    motor_2A.value(0)
    motor_enable.duty_u16(MOTOR_HALF_SPEED)
    print("Motor: CLOCKWISE at half speed")


def motor_anticlockwise():
    motor_1A.value(0)
    motor_2A.value(1)
    motor_enable.duty_u16(MOTOR_HALF_SPEED)
    print("Motor: ANTI-CLOCKWISE at half speed")


def run_motor_test_cycle():
    motor_clockwise()
    sleep_ms(5000)

    motor_stop()
    sleep_ms(5000)

    motor_anticlockwise()
    sleep_ms(5000)

    motor_stop()

def init_display():
    display.fill(0)
    display.text("System Starting", 0, 0)
    display.text("Please wait...", 0, 16)
    display.show()
    sleep_ms(1000)

def distance_to_level_percent(distance_mm, empty_level_mm):
    """
    Converts ultrasonic distance to tank fill percentage.
    """

    if distance_mm < 0:
        return None

    level = ((empty_level_mm - distance_mm) * 100) / (empty_level_mm - FULL_LEVEL_MM)

    if level < 0:
        level = 0

    if level > 100:
        level = 100

    return int(level)


def draw_vertical_tank(x, y, width, height, percent):
    """
    Draws a vertical battery-like tank symbol on the OLED.
    """

    # Outer border
    display.rect(x, y, width, height, 1)

    # Small cap at the top
    cap_width = width // 2
    cap_x = x + (width - cap_width) // 2
    display.rect(cap_x, y - 3, cap_width, 3, 1)

    if percent is None:
        # Draw X for invalid sensor reading
        display.line(x, y, x + width - 1, y + height - 1, 1)
        display.line(x + width - 1, y, x, y + height - 1, 1)
        return

    # Fill from bottom upward
    inner_height = height - 2
    fill_height = int((percent / 100) * inner_height)

    fill_x = x + 1
    fill_y = y + height - 1 - fill_height
    fill_width = width - 2

    if fill_height > 0:
        display.fill_rect(fill_x, fill_y, fill_width, fill_height, 1)


def show_tank_levels(urine_tank_distance, poop_tank_distance):
    urine_percent = distance_to_level_percent(
        urine_tank_distance,
        URINE_EMPTY_LEVEL_MM
    )

    poop_percent = distance_to_level_percent(
        poop_tank_distance,
        POOP_EMPTY_LEVEL_MM
    )

    display.fill(0)

    display.text("Tank Levels", 20, 0)

    # Urine tank display
    display.text("Urine", 0, 14)
    draw_vertical_tank(8, 28, 20, 32, urine_percent)

    if urine_percent is None:
        display.text("ERR", 35, 40)
    else:
        display.text(str(urine_percent) + "%", 35, 40)

    # Poop tank display
    display.text("Poop", 72, 14)
    draw_vertical_tank(80, 28, 20, 32, poop_percent)

    if poop_percent is None:
        display.text("ERR", 107, 40)
    else:
        display.text(str(poop_percent) + "%", 107, 40)

    display.show()

def show_tank_distances(urine_tank_distance, poop_tank_distance):
    display.fill(0)

    display.text("Tank Distances", 0, 0)

    display.text("Urine:", 0, 18)
    display.text(format_distance(urine_tank_distance), 55, 18)

    display.text("Poop:", 0, 38)
    display.text(format_distance(poop_tank_distance), 55, 38)

    display.show()


def format_distance(distance):
    if distance >= 0:
        return str(distance) + " mm"
    elif distance == -1:
        return "Invalid"
    elif distance == -2:
        return "Checksum Err"
    elif distance == -3:
        return "Read Err"
    elif distance == -4:
        return "No Data"
    else:
        return "Error"


# ============================================================
# URINE TANK SENSOR: PIO UART SENSOR
# Urine tank sensor TX -> Pico GP15
# Urine tank sensor RX -> Pico GP14, optional
#
# GP14 and GP15 are not normal UART TX/RX pins.
# Therefore, urine tank sensor is read using PIO UART on GP15.
# ============================================================

urine_tank_sensor_rx_control = Pin(14, Pin.OUT)
urine_tank_sensor_rx_control.value(1)


@asm_pio(
    in_shiftdir=PIO.SHIFT_RIGHT,
    autopush=False
)
def uart_rx_pio():
    # Wait for UART start bit
    wait(0, pin, 0)

    # Wait near middle of first data bit
    set(x, 7) [10]

    # Read 8 data bits
    label("bitloop")
    in_(pins, 1) [6]
    jmp(x_dec, "bitloop")

    # Push byte into RX FIFO
    push(block)


URINE_TANK_RX_PIN = 15
BAUDRATE = 9600

urine_tank_sm = StateMachine(
    0,
    uart_rx_pio,
    freq=BAUDRATE * 8,
    in_base=Pin(URINE_TANK_RX_PIN),
    jmp_pin=Pin(URINE_TANK_RX_PIN)
)


def init_urine_tank_pio_uart():
    urine_tank_sm.active(1)


def urine_tank_any():
    return urine_tank_sm.rx_fifo()


def urine_tank_read_byte():
    if urine_tank_sm.rx_fifo():
        return urine_tank_sm.get() >> 24
    return None


def clear_urine_tank_buffer():
    while urine_tank_sm.rx_fifo():
        urine_tank_sm.get()


def read_urine_tank_distance():
    """
    Reads distance from the urine tank sensor using PIO UART on GP15.

    Returns:
        positive value = distance in mm
        -1 = invalid measurement
        -2 = checksum error
        -3 = incomplete packet/read error
        -4 = timeout/no data
    """

    clear_urine_tank_buffer()

    packet = []
    start_time = ticks_ms()

    # Wait for start byte 0xFF
    while True:
        if urine_tank_any():
            byte = urine_tank_read_byte()

            if byte == 0xFF:
                packet.append(byte)
                break

        if ticks_diff(ticks_ms(), start_time) > 1000:
            return -4

    # Read remaining 3 bytes
    while len(packet) < 4:
        if urine_tank_any():
            byte = urine_tank_read_byte()
            packet.append(byte)

        if ticks_diff(ticks_ms(), start_time) > 1000:
            return -3

    return decode_a02yyuw_packet(packet)


# ============================================================
# POOP TANK SENSOR: HARDWARE UART SENSOR
# Poop tank sensor TX -> Pico GP13
# Poop tank sensor RX -> Pico GP12
#
# GP12 and GP13 are valid hardware UART pins.
# Therefore, poop tank sensor is read normally using UART0.
# ============================================================

poop_tank_uart = UART(
    0,
    baudrate=9600,
    tx=Pin(12),
    rx=Pin(13),
    bits=8,
    parity=None,
    stop=1
)


def clear_poop_tank_buffer():
    while poop_tank_uart.any():
        poop_tank_uart.read()


def read_poop_tank_distance():
    """
    Reads distance from the poop tank sensor using hardware UART0 on GP12/GP13.

    Returns:
        positive value = distance in mm
        -1 = invalid measurement
        -2 = checksum error
        -3 = incomplete packet/read error
        -4 = timeout/no data
    """

    clear_poop_tank_buffer()

    packet = []
    start_time = ticks_ms()

    # Wait for start byte 0xFF
    while True:
        if poop_tank_uart.any():
            byte_data = poop_tank_uart.read(1)

            if byte_data:
                byte = byte_data[0]

                if byte == 0xFF:
                    packet.append(byte)
                    break

        if ticks_diff(ticks_ms(), start_time) > 1000:
            return -4

    # Read remaining 3 bytes
    while len(packet) < 4:
        if poop_tank_uart.any():
            byte_data = poop_tank_uart.read(1)

            if byte_data:
                packet.append(byte_data[0])

        if ticks_diff(ticks_ms(), start_time) > 1000:
            return -3

    return decode_a02yyuw_packet(packet)


# ============================================================
# COMMON PACKET DECODER
# Works for both A02YYUW sensors
# Packet format:
# Byte 0 = 0xFF
# Byte 1 = distance high byte
# Byte 2 = distance low byte
# Byte 3 = checksum
# ============================================================

def decode_a02yyuw_packet(packet):
    if len(packet) != 4:
        return -3

    checksum = (packet[0] + packet[1] + packet[2]) & 0xFF

    if checksum == packet[3]:
        measurement = (packet[1] << 8) + packet[2]

        if measurement == 250:
            return -1
        else:
            return measurement
    else:
        return -2


# ============================================================
# SYSTEM INITIALIZATION
# ============================================================

def init_system():
    init_display()
    init_urine_tank_pio_uart()

    display.fill(0)
    display.text("System Ready", 0, 0)
    display.text("Urine: GP15 PIO", 0, 16)
    display.text("Poop: GP12/13", 0, 32)
    display.show()
    sleep_ms(1000)

def update_stable_distance(new_distance, readings_buffer, last_stable_distance):
    """
    Updates the tank distance only if the latest readings are stable.

    new_distance:
        latest raw ultrasonic reading

    readings_buffer:
        list storing recent valid readings

    last_stable_distance:
        previous accepted stable distance

    Returns:
        updated stable distance if readings are stable,
        otherwise returns previous stable distance.
    """

    # Ignore invalid readings
    if new_distance < 0:
        return last_stable_distance

    # Add new valid reading
    readings_buffer.append(new_distance)

    # Keep only the latest 6 readings
    if len(readings_buffer) > STABLE_SAMPLE_COUNT:
        readings_buffer.pop(0)

    # Do not update until we have 6 readings
    if len(readings_buffer) < STABLE_SAMPLE_COUNT:
        return last_stable_distance

    highest_reading = max(readings_buffer)
    lowest_reading = min(readings_buffer)

    # Check if all 6 readings are close to each other
    if highest_reading - lowest_reading <= STABLE_TOLERANCE_MM:
        # Use average of the stable readings
        stable_value = sum(readings_buffer) // len(readings_buffer)
        return stable_value

    # If readings are not stable, keep previous stable value
    return last_stable_distance

def run_floor_on_to_off_action():
    """
    Runs when floor switch changes from ON to OFF.
    ON  = 0
    OFF = 1

    Action:
    Rotate motor anti-clockwise for 2.5 seconds.
    """

    print("Floor switch changed from ON to OFF")
    print("Running anti-clockwise motor action")

    motor_anticlockwise()
    sleep_ms(2500)
    motor_stop()


def run_floor_off_to_on_action():
    """
    Runs when floor switch changes from OFF to ON.
    ON  = 0
    OFF = 1

    Action:
    1. Wait 5 seconds
    2. Turn ON water pump relay and solenoid valve relay for 4 seconds
    3. Turn both relays OFF
    4. Wait 1 second
    5. Rotate motor clockwise for 2.5 seconds
    6. Stop motor
    """

    print("Floor switch changed from OFF to ON")
    print("Waiting 5 seconds before pump and valve")

    sleep_ms(5000)

    print("Turning pump and solenoid valve ON for 4 seconds")
    water_pump_relay.value(1)
    solenoid_valve_relay.value(1)

    sleep_ms(4000)

    print("Turning pump and solenoid valve OFF")
    water_pump_relay.value(0)
    solenoid_valve_relay.value(0)

    print("Waiting 1 second before clockwise motor rotation")
    sleep_ms(1000)

    print("Running clockwise motor action")
    motor_clockwise()
    sleep_ms(2500)
    motor_stop()


def handle_floor_switch_state_change(current_floor_state):
    """
    Checks whether floor switch state has changed.
    Motor/relay actions only run when there is a change.
    """

    global previous_floor_state

    if current_floor_state != previous_floor_state:

        # ON to OFF
        if previous_floor_state == 0 and current_floor_state == 1:
            run_floor_on_to_off_action()

        # OFF to ON
        elif previous_floor_state == 1 and current_floor_state == 0:
            run_floor_off_to_on_action()

        previous_floor_state = current_floor_state

def load_user_count():
    """
    Loads the saved user count from Pico flash memory.
    If the file does not exist, starts from 0.
    """

    try:
        file = open(USER_COUNT_FILE, "r")
        value = file.read()
        file.close()

        return int(value)

    except:
        return 0


def save_user_count(count):
    """
    Saves the user count to Pico flash memory.
    """

    try:
        file = open(USER_COUNT_FILE, "w")
        file.write(str(count))
        file.close()

    except:
        print("Error saving user count")


def increment_user_count():
    """
    Increments user count and saves it permanently.
    """

    global user_count

    user_count += 1
    save_user_count(user_count)

    print("User count increased to:", user_count)

def handle_user_counting(door_state, lock_state):
    """
    Counts a toilet user when:
    1. Door switch changes from OFF to ON
    2. Then lock switch changes from OFF to ON

    With Pin.PULL_UP:
        ON  = 0
        OFF = 1
    """

    global previous_door_state
    global previous_lock_state
    global door_opened_before_lock

    # Detect door changing from OFF to ON
    if previous_door_state == 1 and door_state == 0:
        door_opened_before_lock = True
        print("Door changed from OFF to ON")
        print("Waiting for lock switch to change from OFF to ON")

    # Detect lock changing from OFF to ON after door event
    if door_opened_before_lock:
        if previous_lock_state == 1 and lock_state == 0:
            print("Lock changed from OFF to ON")
            print("Valid toilet user detected")

            increment_user_count()

            # Reset sequence so this person is only counted once
            door_opened_before_lock = False

    # Optional reset:
    # If the door goes back OFF before the lock is turned ON,
    # cancel the pending count.
    if door_opened_before_lock:
        if previous_door_state == 0 and door_state == 1:
            print("Door went back OFF before lock was ON")
            print("User count sequence cancelled")
            door_opened_before_lock = False

    previous_door_state = door_state
    previous_lock_state = lock_state

def load_email_state(filename):
    """
    Loads whether an alert email had already been sent.
    Returns True if sent, False if not sent.
    """

    try:
        file = open(filename, "r")
        value = file.read()
        file.close()

        if value == "1":
            return True
        else:
            return False

    except:
        return False


def save_email_state(filename, state):
    """
    Saves email alert state permanently.
    state = True means email already sent.
    state = False means email not sent.
    """

    try:
        file = open(filename, "w")

        if state:
            file.write("1")
        else:
            file.write("0")

        file.close()

    except:
        print("Error saving email state:", filename)

def connect_to_internet(ssid, password):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if wlan.isconnected():
        print("Already connected")
        print("IP address:", wlan.ifconfig()[0])
        return True

    wlan.connect(ssid, password)

    max_wait = 20

    while max_wait > 0:
        if wlan.status() < 0 or wlan.status() >= 3:
            break

        max_wait -= 1
        print("waiting for connection...")
        time.sleep(1)

    if wlan.status() != 3:
        print("WiFi status:", wlan.status())
        print("Network connection failed")
        return False
    else:
        print("connected")
        print("WiFi status:", wlan.status())
        print("IP address:", wlan.ifconfig()[0])
        return True

def send_email_alert(tank_name, tank_level, tank_distance):
    """
    Sends an email alert using Gmail SMTP through umail.
    """

    email_subject = tank_name + " Tank Almost Full"

    email_body = (
        "Smart Toilet Tank Alert\n\n"
        + tank_name + " tank has reached "
        + str(tank_level) + "%.\n\n"
        + "Distance reading: "
        + str(tank_distance) + " mm\n\n"
        + "Please empty the " + tank_name.lower() + " tank."
    )

    try:
        smtp = umail.SMTP("smtp.gmail.com", 465, ssl=True)

        smtp.login(sender_email, sender_app_password)
        smtp.to(recipient_email)

        smtp.write("From:" + sender_name + "<" + sender_email + ">\n")
        smtp.write("Subject:" + email_subject + "\n\n")
        smtp.write(email_body)

        smtp.send()
        smtp.quit()

        print(tank_name, "tank email alert sent")
        return True

    except Exception as e:
        print("Failed to send", tank_name, "tank email:", e)
        return False

def check_tank_email_alerts(urine_level, urine_distance, poop_level, poop_distance):
    """
    Sends an email once when either tank reaches 80%.
    Resets the email state when the tank level goes below 80%.
    """

    global urine_email_sent
    global poop_email_sent

    # -----------------------------
    # Urine tank email alert
    # -----------------------------
    if urine_level is not None:

        if urine_level >= TANK_ALERT_LEVEL_PERCENT and not urine_email_sent:
            print("Urine tank has reached alert level")

            if send_email_alert("Urine", urine_level, urine_distance):
                urine_email_sent = True
                save_email_state(URINE_EMAIL_STATE_FILE, True)

        elif urine_level < TANK_ALERT_LEVEL_PERCENT and urine_email_sent:
            print("Urine tank level is below 80%. Resetting urine email state.")
            urine_email_sent = False
            save_email_state(URINE_EMAIL_STATE_FILE, False)

    # -----------------------------
    # Poop tank email alert
    # -----------------------------
    if poop_level is not None:

        if poop_level >= TANK_ALERT_LEVEL_PERCENT and not poop_email_sent:
            print("Poop tank has reached alert level")

            if send_email_alert("Poop", poop_level, poop_distance):
                poop_email_sent = True
                save_email_state(POOP_EMAIL_STATE_FILE, True)

        elif poop_level < TANK_ALERT_LEVEL_PERCENT and poop_email_sent:
            print("Poop tank level is below 80%. Resetting poop email state.")
            poop_email_sent = False
            save_email_state(POOP_EMAIL_STATE_FILE, False)

# ============================================================
# FLOOR SWITCH STATE TRACKING
# ============================================================

previous_floor_state = floor_switch_1.value()

# ============================================================
# MAIN LOOP
# ============================================================

init_system()

user_count = load_user_count()
print("Saved user count:", user_count)

# Load saved email alert states
urine_email_sent = load_email_state(URINE_EMAIL_STATE_FILE)
poop_email_sent = load_email_state(POOP_EMAIL_STATE_FILE)

print("Saved urine email state:", urine_email_sent)
print("Saved poop email state:", poop_email_sent)

# Connect to WiFi
wifi_connected = connect_to_internet(WIFI_SSID, WIFI_PASSWORD)

#run_motor_test_cycle()

while True:

    # Read raw tank distances
    raw_urine_tank_distance = read_urine_tank_distance()
    raw_poop_tank_distance = read_poop_tank_distance()

    # Update only if readings are stable
    stable_urine_tank_distance = update_stable_distance(
        raw_urine_tank_distance,
        urine_tank_readings,
        stable_urine_tank_distance
    )

    stable_poop_tank_distance = update_stable_distance(
        raw_poop_tank_distance,
        poop_tank_readings,
        stable_poop_tank_distance
    )

    # Read switches
    floor_state, door_state, lock_state = read_switch_states()

    handle_user_counting(door_state, lock_state)
    handle_floor_switch_state_change(floor_state)

    # Print raw readings
    print("Raw urine tank distance:", format_distance(raw_urine_tank_distance))
    print("Raw poop tank distance:", format_distance(raw_poop_tank_distance))

    # Print stable readings
    if stable_urine_tank_distance is not None:
        print("Stable urine tank distance:", stable_urine_tank_distance, "mm")
        print(
            "Stable urine tank level:",
            distance_to_level_percent(stable_urine_tank_distance, URINE_EMPTY_LEVEL_MM),
            "%"
        )
    else:
        print("Stable urine tank distance: waiting...")

    if stable_poop_tank_distance is not None:
        print("Stable poop tank distance:", stable_poop_tank_distance, "mm")
        print(
            "Stable poop tank level:",
            distance_to_level_percent(stable_poop_tank_distance, POOP_EMPTY_LEVEL_MM),
            "%"
        )
    else:
        print("Stable poop tank distance: waiting...")

    # Email alert check and OLED update
    if stable_urine_tank_distance is not None and stable_poop_tank_distance is not None:

        urine_tank_level = distance_to_level_percent(
            stable_urine_tank_distance,
            URINE_EMPTY_LEVEL_MM
        )

        poop_tank_level = distance_to_level_percent(
            stable_poop_tank_distance,
            POOP_EMPTY_LEVEL_MM
        )

        if wifi_connected:
            check_tank_email_alerts(
                urine_tank_level,
                stable_urine_tank_distance,
                poop_tank_level,
                stable_poop_tank_distance
            )
        else:
            print("WiFi not connected. Email alert not sent.")

        # Show stable tank levels on OLED
        show_tank_levels(stable_urine_tank_distance, stable_poop_tank_distance)

    else:
        display.fill(0)
        display.text("Stabilizing...", 0, 0)
        display.text("Please wait", 0, 20)
        display.show()

    print("--------------------")
    sleep_ms(200)