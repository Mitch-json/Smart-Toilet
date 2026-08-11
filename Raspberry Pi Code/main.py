from machine import Pin, I2C, UART, PWM
from utime import sleep_ms, sleep_us, ticks_ms, ticks_us, ticks_diff
import sh1106
import rp2
from rp2 import PIO, StateMachine, asm_pio

import network
import time
import umail

import _thread

import socket
import json


# ============================================================
# GENERAL SETUP
# ============================================================

led = Pin("LED", Pin.OUT)

# ============================================================
# EMAIL ALERT SETTINGS
# ============================================================

WIFI_SSID = "4G-MIFI-A1C2"
WIFI_PASSWORD = "1234567890"

sender_email = "mitchjaga@gmail.com"
sender_name = "Smart Toilet"
sender_app_password = "rbch zncz bcjx godz"
recipient_email_1 = "nancy.matheri@egerton.ac.ke"
recipient_email_2 = "mmatheri@andrew.cmu.edu"

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

URINE_EMPTY_LEVEL_MM = 965
POOP_EMPTY_LEVEL_MM = 982
FULL_LEVEL_MM = 20

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
# Floor switch 2 -> GP3
# Door switch 1  -> GP4
# Lock switch 1  -> GP5
#
# Floor switch behavior from testing:
#   0 = released
#   1 = pressed
#
# Combined floor logic:
#   If either floor switch is pressed, floor_state = 1
#   If both floor switches are released, floor_state = 0
# ============================================================

floor_switch_1 = Pin(2, Pin.IN, Pin.PULL_UP)
floor_switch_2 = Pin(3, Pin.IN, Pin.PULL_UP)

door_switch_1 = Pin(4, Pin.IN, Pin.PULL_UP)
lock_switch_1 = Pin(5, Pin.IN, Pin.PULL_UP)

bulb_relay = Pin(9, Pin.OUT)                  

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
# BULB RELAY SEQUENCE SETTINGS
# ============================================================

previous_door_state_for_bulb = door_switch_1.value()
previous_lock_state_for_bulb = lock_switch_1.value()

waiting_for_lock_to_turn_bulb_on = False
waiting_for_door_to_close_to_turn_bulb_off = False

bulb_is_on = False

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
bulb_relay.value(0)

# ============================================================
# FLUSH TANK ULTRASONIC SENSOR SETUP
# Trigger -> GP10
# Echo    -> GP11
#
# Flush tank is considered EMPTY when distance > 30 cm
# ============================================================

flush_tank_trig = Pin(10, Pin.OUT)
flush_tank_echo = Pin(11, Pin.IN)

flush_tank_trig.value(0)

FLUSH_TANK_EMPTY_DISTANCE_CM = 28

no_water_display_until = 0
NO_WATER_DISPLAY_TIME_MS = 5000

# ============================================================
# SERVO MOTOR GATE SETUP
# GP6 servo: 180 degrees closed, 90 degrees open
# GP7 servo: 0 degrees closed, 90 degrees open
# ============================================================

servo_gp6 = PWM(Pin(6))
servo_gp7 = PWM(Pin(7))

servo_gp6.freq(50)
servo_gp7.freq(50)

# ============================================================
# FLOOR SWITCH DEBOUNCE SETTINGS
# ============================================================

FLOOR_DEBOUNCE_MS = 300

last_raw_floor_state = read_combined_floor_state()
debounced_floor_state = last_raw_floor_state
last_floor_change_time = ticks_ms()

# ============================================================
# FLOOR RELEASE CONFIRMATION SETTINGS
# ============================================================

FLOOR_RELEASE_CONFIRM_MS = 3000

floor_release_pending = False
floor_release_start_time = 0


# ============================================================
# GATE STATE SETTINGS
# ============================================================

GATE_STATE_FILE = "gate_state.txt"

GATE_OPEN = "OPEN"
GATE_CLOSED = "CLOSED"

current_gate_state = GATE_CLOSED


# ============================================================
# NON-BLOCKING FLUSH STATE MACHINE SETTINGS
# ============================================================

FLUSH_IDLE = 0
FLUSH_WAITING = 1
FLUSH_VALVE_ONLY = 2
FLUSH_PUMP_AND_VALVE = 3
FLUSH_CLOSING_GATE = 4

flush_state = FLUSH_IDLE
flush_state_start_time = 0

FLUSH_WAIT_BEFORE_WATER_MS = 5000
VALVE_ONLY_TIME_MS = 1500
PUMP_AND_VALVE_TIME_MS = 4000

# ============================================================
# OLED DISPLAY SETUP
# OLED SDA = GP16
# OLED SCL = GP17
# ============================================================

i2c = I2C(0, sda=Pin(16), scl=Pin(17), freq=400000)
display = sh1106.SH1106_I2C(128, 64, i2c, None, 0x3c)


# ============================================================
# DASHBOARD STATUS SENDING
# ============================================================

shared_status = {
    "poopTankLevel": 0,
    "urineTankLevel": 0,
    "userCount": 0,
    "solenoidValve": False,
    "bulbRelay": False,
    "batteryLevel": 100,
    "enoughWater": False
}

# ============================================================
# FACIAL RECOGNITION WELCOME MESSAGE
# ============================================================

latest_recognized_name = None
latest_recognition_confidence = 0

welcome_display_until = 0
WELCOME_DISPLAY_TIME_MS = 3000

last_welcomed_name = None


def update_shared_status(urine_level, poop_level):
    """
    Updates the status values that the dashboard reads.

    Pico sends:
        - poopTankLevel
        - urineTankLevel
        - userCount
        - solenoidValve
        - bulbRelay
        - batteryLevel

    NOTE:
        shared_status["solenoidValve"] is used by the frontend
        as the separation gate status.

        True  = separation gate open
        False = separation gate closed

        Do not overwrite it using solenoid_valve_relay.value(),
        because that relay is the water valve, not the separation gate.
    """

    if urine_level is not None:
        shared_status["urineTankLevel"] = urine_level

    if poop_level is not None:
        shared_status["poopTankLevel"] = poop_level

    shared_status["userCount"] = user_count
    shared_status["bulbRelay"] = bool(bulb_relay.value())

    # No battery sensor for now, so send fixed value
    shared_status["batteryLevel"] = 100


def read_combined_floor_state():
    """
    Reads both floor switches and combines them.

    Actual tested behavior:
        0 = released
        1 = pressed

    If either floor switch is pressed, return 1.
    If both are released, return 0.
    """

    floor_1 = floor_switch_1.value()
    floor_2 = floor_switch_2.value()

    if floor_1 == 1 or floor_2 == 1:
        return 1

    return 0

def read_switch_states():
    floor_state = read_combined_floor_state()
    door_state = door_switch_1.value()
    lock_state = lock_switch_1.value()

    return floor_state, door_state, lock_state

def switch_state_text(value):
    """
    With Pin.PULL_UP:
        0 = ON / pressed / closed
        1 = OFF / open / released
    """

    if value == 0:
        return "ON "
    else:
        return "OFF"


def show_live_switch_status(floor_state, door_state, lock_state):
    """
    Displays live switch states on the OLED for debugging.
    """

    display.fill(0)

    display.text("Switch Debug", 0, 0)

    display.text("Floor:", 0, 16)
    display.text(switch_state_text(floor_state), 64, 16)
    display.text("GP2", 96, 16)

    display.text("Door:", 0, 32)
    display.text(switch_state_text(door_state), 64, 32)
    display.text("GP4", 96, 32)

    display.text("Lock:", 0, 48)
    display.text(switch_state_text(lock_state), 64, 48)
    display.text("GP5", 96, 48)

    display.show()

MOTOR_QUARTER_SPEED = 16384
MOTOR_HALF_SPEED = 32768   # 50% duty cycle
MOTOR_FULL_SPEED = 65535   # 100% duty cycle
MOTOR_OFF = 0


def set_angle(servo, angle):
    """
    Moves a servo to a given angle from 0 to 180 degrees.
    """

    min_duty = 1638   # approximately 0 degrees
    max_duty = 8192   # approximately 180 degrees

    duty = int(min_duty + (angle / 180.0) * (max_duty - min_duty))
    servo.duty_u16(duty)


def move_one_servo(servo, start_angle, end_angle, delay=0.005):
    """
    Moves one servo slowly from start_angle to end_angle.
    """

    if start_angle < end_angle:
        for angle in range(start_angle, end_angle + 1):
            set_angle(servo, angle)
            time.sleep(delay)
    else:
        for angle in range(start_angle, end_angle - 1, -1):
            set_angle(servo, angle)
            time.sleep(delay)


def open_both_servos(delay=0.005):
    """
    Opens the separation gate.

    Opening:
    1. GP6 moves first from 180 to 170.
    2. Then GP6 and GP7 open together.

    GP6: 180 closed -> 90 open
    GP7: 0 closed   -> 90 open
    """

    print("Opening gate: GP6 starts first")

    move_one_servo(servo_gp6, 180, 170, delay)

    print("Opening gate: both servos moving together")

    gp6_angle = 170
    gp7_angle = 0

    while gp6_angle >= 90 or gp7_angle <= 90:

        if gp6_angle >= 90:
            set_angle(servo_gp6, gp6_angle)
            gp6_angle -= 1

        if gp7_angle <= 90:
            set_angle(servo_gp7, gp7_angle)
            gp7_angle += 1

        time.sleep(delay)

    shared_status["solenoidValve"] = True
    save_gate_state(GATE_OPEN)
    print("Separation gate OPEN")


def close_both_servos(delay=0.005):
    """
    Closes the separation gate.

    Closing:
    1. GP7 moves first from 90 to 80.
    2. Then GP7 and GP6 close together.

    GP7: 90 open -> 0 closed
    GP6: 90 open -> 180 closed
    """

    print("Closing gate: GP7 starts first")

    move_one_servo(servo_gp7, 90, 80, delay)

    print("Closing gate: both servos moving together")

    gp7_angle = 80
    gp6_angle = 90

    while gp7_angle >= 0 or gp6_angle <= 180:

        if gp7_angle >= 0:
            set_angle(servo_gp7, gp7_angle)
            gp7_angle -= 1

        if gp6_angle <= 180:
            set_angle(servo_gp6, gp6_angle)
            gp6_angle += 1

        time.sleep(delay)

    shared_status["solenoidValve"] = False
    save_gate_state(GATE_CLOSED)
    print("Separation gate CLOSED")

def save_gate_state(state):
    """
    Saves the last known gate state.
    """

    try:
        file = open(GATE_STATE_FILE, "w")
        file.write(state)
        file.close()
    except:
        print("Error saving gate state")


def load_gate_state():
    """
    Loads the last known gate state.
    """

    try:
        file = open(GATE_STATE_FILE, "r")
        state = file.read()
        file.close()

        if state == GATE_OPEN:
            return GATE_OPEN

        if state == GATE_CLOSED:
            return GATE_CLOSED

        return GATE_CLOSED

    except:
        return GATE_CLOSED


def startup_move_gate_to_known_position():
    """
    Moves the separation gate to a known CLOSED position during startup.

    Note:
    Standard servos do not provide position feedback to the Pico.
    So this uses the last saved gate state.

    If the gate was last known OPEN, it closes slowly.
    If it was last known CLOSED, it commands the closed angles directly.
    """

    global current_gate_state

    current_gate_state = load_gate_state()

    print("Startup gate state:", current_gate_state)

    # Always make sure pump and valve are OFF at startup
    water_pump_relay.value(0)
    solenoid_valve_relay.value(0)

    if current_gate_state == GATE_OPEN:
        print("Gate was last open. Closing gently at startup.")
        close_both_servos(delay=0.01)

    else:
        print("Gate was last closed. Setting closed servo positions.")
        set_angle(servo_gp6, 180)
        set_angle(servo_gp7, 0)

    current_gate_state = GATE_CLOSED
    shared_status["solenoidValve"] = False
    save_gate_state(GATE_CLOSED)

    sleep_ms(1000)


def update_floor_switch_debounce(raw_floor_state):
    """
    Debounces the floor switch.

    Returns the debounced floor state only after the raw state
    has remained stable for FLOOR_DEBOUNCE_MS.
    """

    global last_raw_floor_state
    global debounced_floor_state
    global last_floor_change_time

    if raw_floor_state != last_raw_floor_state:
        last_raw_floor_state = raw_floor_state
        last_floor_change_time = ticks_ms()

    if ticks_diff(ticks_ms(), last_floor_change_time) >= FLOOR_DEBOUNCE_MS:
        debounced_floor_state = last_raw_floor_state

    return debounced_floor_state


def cancel_flush_sequence():
    """
    Cancels any ongoing flush sequence and turns pump and valve OFF.
    """

    global flush_state

    if flush_state != FLUSH_IDLE:
        print("Cancelling flush sequence")

    water_pump_relay.value(0)
    solenoid_valve_relay.value(0)

    flush_state = FLUSH_IDLE


def start_flush_close_sequence():
    """
    Starts the non-blocking flush and close sequence.
    This does not pause the main loop.
    """

    global flush_state
    global flush_state_start_time

    if flush_state != FLUSH_IDLE:
        print("Flush sequence already running")
        return

    print("Starting non-blocking flush sequence")
    print("Waiting 5 seconds before opening water valve")

    flush_state = FLUSH_WAITING
    flush_state_start_time = ticks_ms()

def show_no_water_message(flush_distance):
    """
    Displays No Water warning on OLED.
    """

    display.fill(0)
    display.text("NO WATER", 28, 0)
    display.text("Flush tank", 15, 20)
    display.text("is empty", 28, 34)

    if flush_distance >= 0:
        display.text("{:.1f} cm".format(flush_distance), 32, 52)
    else:
        display.text("Sensor error", 12, 52)

    display.show()


def no_water_message_is_active():
    """
    Keeps the No Water message visible for a few seconds.
    """

    return ticks_diff(no_water_display_until, ticks_ms()) > 0

def read_flush_tank_distance_cm():
    """
    Reads distance from the flush tank ultrasonic sensor.

    Trigger -> GP10
    Echo    -> GP11

    Returns:
        positive value = distance in cm
        -4 = timeout / no echo
    """

    flush_tank_trig.value(0)
    sleep_us(2)

    flush_tank_trig.value(1)
    sleep_us(10)
    flush_tank_trig.value(0)

    start_wait = ticks_us()

    # Wait for echo to go HIGH
    while flush_tank_echo.value() == 0:
        if ticks_diff(ticks_us(), start_wait) > 30000:
            return -4

    pulse_start = ticks_us()

    # Wait for echo to go LOW
    while flush_tank_echo.value() == 1:
        if ticks_diff(ticks_us(), pulse_start) > 30000:
            return -4

    pulse_end = ticks_us()

    duration = ticks_diff(pulse_end, pulse_start)

    # Speed of sound = 0.0343 cm/us
    distance_cm = (duration * 0.0343) / 2

    return distance_cm

def update_enough_water_status():
    """
    Updates shared_status["enoughWater"] using the flush tank ultrasonic sensor.

    Flush tank logic:
        distance <= FLUSH_TANK_EMPTY_DISTANCE_CM means enough water
        distance >  FLUSH_TANK_EMPTY_DISTANCE_CM means no water
        invalid reading means assume no water, to protect the pump
    """

    flush_tank_distance = read_flush_tank_distance_cm()
    
    if flush_tank_distance >= 0 and flush_tank_distance <= FLUSH_TANK_EMPTY_DISTANCE_CM:
        shared_status["enoughWater"] = True
    else:
        shared_status["enoughWater"] = False

    print("Flush tank water distance:", flush_tank_distance, "cm")

    if flush_tank_distance >= 0 and flush_tank_distance <= FLUSH_TANK_EMPTY_DISTANCE_CM:
        shared_status["enoughWater"] = True
        print("enoughWater: True")
    else:
        shared_status["enoughWater"] = False
        print("enoughWater: False")

def process_flush_close_sequence(current_floor_state):
    """
    Processes the flush and gate-close sequence without blocking the main loop.

    Actual floor switch behavior:
        Floor released / no person      = 0
        Floor pressed / person standing = 1

    Flush tank protection:
        If flush tank ultrasonic distance is greater than 30 cm,
        the flush tank is considered empty.

        In that case:
            - Do not open the solenoid valve
            - Do not start the pump
            - Display NO WATER on the OLED
            - Close the separation gate
    """

    global flush_state
    global flush_state_start_time
    global current_gate_state
    global no_water_display_until

    now = ticks_ms()

    # If someone steps back on the floor during the sequence, stop flushing.
    if current_floor_state == 1 and flush_state != FLUSH_IDLE:
        print("Floor pressed during flush sequence. Stopping flush.")
        cancel_flush_sequence()
        return

    if flush_state == FLUSH_IDLE:
        return

    if flush_state == FLUSH_WAITING:
        if ticks_diff(now, flush_state_start_time) >= FLUSH_WAIT_BEFORE_WATER_MS:

            flush_tank_distance = read_flush_tank_distance_cm()

            print("Flush tank distance:", flush_tank_distance, "cm")

            # If distance is greater than 30 cm, tank is empty
            if flush_tank_distance > FLUSH_TANK_EMPTY_DISTANCE_CM:
                print("NO WATER: Flush tank is empty")
                print("Pump and valve will NOT turn ON")

                water_pump_relay.value(0)
                solenoid_valve_relay.value(0)

                show_no_water_message(flush_tank_distance)
                no_water_display_until = ticks_ms() + NO_WATER_DISPLAY_TIME_MS

                print("Closing separation gate without flushing")
                close_both_servos(delay=0.005)

                current_gate_state = GATE_CLOSED
                shared_status["solenoidValve"] = False
                save_gate_state(GATE_CLOSED)

                flush_state = FLUSH_IDLE
                return

            # If sensor has no echo/error, also protect the pump
            if flush_tank_distance < 0:
                print("NO WATER / SENSOR ERROR: Flush tank reading invalid")
                print("Pump and valve will NOT turn ON")

                water_pump_relay.value(0)
                solenoid_valve_relay.value(0)

                show_no_water_message(flush_tank_distance)
                no_water_display_until = ticks_ms() + NO_WATER_DISPLAY_TIME_MS

                print("Closing separation gate without flushing")
                close_both_servos(delay=0.005)

                current_gate_state = GATE_CLOSED
                shared_status["solenoidValve"] = False
                save_gate_state(GATE_CLOSED)

                flush_state = FLUSH_IDLE
                return

            print("Flush tank has water")
            print("Opening water valve")
            solenoid_valve_relay.value(1)

            flush_state = FLUSH_VALVE_ONLY
            flush_state_start_time = now

    elif flush_state == FLUSH_VALVE_ONLY:
        if ticks_diff(now, flush_state_start_time) >= VALVE_ONLY_TIME_MS:
            print("Starting water pump")
            water_pump_relay.value(1)

            flush_state = FLUSH_PUMP_AND_VALVE
            flush_state_start_time = now

    elif flush_state == FLUSH_PUMP_AND_VALVE:
        if ticks_diff(now, flush_state_start_time) >= PUMP_AND_VALVE_TIME_MS:
            print("Turning pump and valve OFF")
            water_pump_relay.value(0)
            solenoid_valve_relay.value(0)

            flush_state = FLUSH_CLOSING_GATE
            flush_state_start_time = now

    elif flush_state == FLUSH_CLOSING_GATE:
        print("Closing separation gate using servos")

        close_both_servos(delay=0.005)

        current_gate_state = GATE_CLOSED
        shared_status["solenoidValve"] = False
        save_gate_state(GATE_CLOSED)

        flush_state = FLUSH_IDLE
        print("Flush sequence complete")

def init_display():
    display.fill(0)
    display.text("System Starting", 0, 0)
    display.text("Please wait...", 0, 16)
    display.show()
    sleep_ms(1000)
    
def show_welcome_message(name, confidence):
    """
    Shows a short welcome message on the OLED after
    the React dashboard sends POST /recognition.
    """

    display.fill(0)

    display.text("Welcome", 0, 0)

    # SH1106 text is small; split long names into two lines
    if len(name) <= 16:
        display.text(name, 0, 22)
    else:
        display.text(name[:16], 0, 18)
        display.text(name[16:32], 0, 32)

    display.text(str(confidence) + "% match", 0, 52)
    display.show()


def welcome_message_is_active():
    """
    Returns True while the welcome message should still be shown.
    """

    return (
        latest_recognized_name is not None and
        ticks_diff(welcome_display_until, ticks_ms()) > 0
    )

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
    Runs immediately when the floor switch is pressed.

    Floor pressed = 1

    Action:
        Open the separation gate using the two servo motors.
    """

    global current_gate_state

    print("Floor switch pressed")
    print("Opening separation gate using servos")

    # If a flush sequence had started, cancel it because someone is back on the floor
    cancel_flush_sequence()

    if current_gate_state != GATE_OPEN:
        open_both_servos(delay=0.005)
        current_gate_state = GATE_OPEN
        shared_status["solenoidValve"] = True
        save_gate_state(GATE_OPEN)
    else:
        print("Gate already open")

def run_floor_off_to_on_action():
    """
    Runs when the floor switch is released.

    Floor released = 0

    Action:
        Starts the non-blocking flush and close sequence.
    """

    print("Floor switch released")
    print("Starting flush and close sequence")

    start_flush_close_sequence()

def handle_floor_switch_state_change(current_floor_state):
    """
    Uses the live debounced floor switch state to control the separation gate.

    Actual floor switch behavior from testing:
        Floor released / no person      = 0
        Floor pressed / person standing = 1

    Important:
        The gate opens immediately when floor_state becomes 1.

        The flushing sequence does NOT start immediately when floor_state
        becomes 0. Instead, floor_state must stay 0 for more than 3 seconds.
        This prevents flushing when the person is still using the toilet but
        makes small movements that briefly release the floor switches.
    """

    global previous_floor_state
    global floor_release_pending
    global floor_release_start_time

    now = ticks_ms()

    # --------------------------------------------------------
    # CASE 1: Floor is pressed
    # Person is on the toilet/floor switch
    # --------------------------------------------------------
    if current_floor_state == 1:

        # If we were waiting to confirm release, cancel it
        if floor_release_pending:
            print("Floor pressed again before 3 seconds")
            print("Release confirmation cancelled")
            floor_release_pending = False

        # If confirmed previous state was released, this is a real press event
        if previous_floor_state == 0:
            print("Floor switch changed from RELEASED to PRESSED")
            print("Opening separation gate")

            run_floor_on_to_off_action()

            previous_floor_state = 1

        return

    # --------------------------------------------------------
    # CASE 2: Floor is released
    # Possible person has stepped off
    # --------------------------------------------------------
    if current_floor_state == 0:

        # Only start release confirmation if previous confirmed state was pressed
        if previous_floor_state == 1 and not floor_release_pending:
            print("Floor switch appears RELEASED")
            print("Waiting 3 seconds to confirm user has stepped off")

            floor_release_pending = True
            floor_release_start_time = now

        # If release has remained stable for more than 3 seconds
        if floor_release_pending:
            if ticks_diff(now, floor_release_start_time) >= FLOOR_RELEASE_CONFIRM_MS:
                print("Floor has been released for more than 3 seconds")
                print("Confirmed user has stepped off")
                print("Starting flush and close sequence")

                floor_release_pending = False
                previous_floor_state = 0

                run_floor_off_to_on_action()

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
    1. Door changes from OPEN to CLOSED
    2. Then lock switch changes from OFF to ON

    Actual door switch behavior from testing:
        Door open   = 0 = ON
        Door closed = 1 = OFF

    Lock switch behavior:
        Lock ON/pressed = 0
        Lock OFF        = 1
    """

    global previous_door_state
    global previous_lock_state
    global door_opened_before_lock

    # Detect door changing from OPEN to CLOSED
    # Door open = 0, door closed = 1
    if previous_door_state == 0 and door_state == 1:
        door_opened_before_lock = True
        print("Door changed from OPEN to CLOSED")
        print("Waiting for lock switch to change from OFF to ON")

    # Detect lock changing from OFF to ON after door has closed
    if door_opened_before_lock:
        if previous_lock_state == 1 and lock_state == 0:
            print("Lock changed from OFF to ON")
            print("Valid toilet user detected")

            increment_user_count()

            # Reset sequence so this person is only counted once
            door_opened_before_lock = False

    # Cancel sequence if door opens again before locking
    if door_opened_before_lock:
        if previous_door_state == 1 and door_state == 0:
            print("Door opened again before lock was ON")
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

def wifi_status_text(status):
    if status == network.STAT_IDLE:
        return "IDLE"
    elif status == network.STAT_CONNECTING:
        return "CONNECTING"
    elif status == network.STAT_WRONG_PASSWORD:
        return "WRONG PASSWORD"
    elif status == network.STAT_NO_AP_FOUND:
        return "NO AP FOUND"
    elif status == network.STAT_CONNECT_FAIL:
        return "CONNECT FAIL"
    elif status == network.STAT_GOT_IP:
        return "GOT IP"
    else:
        return "UNKNOWN: " + str(status)


def show_wifi_status(line1, line2=""):
    display.fill(0)
    display.text("WiFi Debug", 0, 0)
    display.text(line1[:16], 0, 20)
    display.text(line2[:16], 0, 40)
    display.show()


def connect_to_internet(ssid, password):
    wlan = network.WLAN(network.STA_IF)

    wlan.active(False)
    sleep_ms(500)

    wlan.active(True)
    sleep_ms(1000)

    print("WiFi active:", wlan.active())
    print("Connecting to:", ssid)

    show_wifi_status("Connecting to", ssid)

    if wlan.isconnected():
        ip = wlan.ifconfig()[0]
        print("Already connected")
        print("IP address:", ip)
        show_wifi_status("Connected", ip)
        return True

    wlan.connect(ssid, password)

    max_wait = 30

    while max_wait > 0:
        status = wlan.status()
        print("WiFi status:", status, wifi_status_text(status))

        show_wifi_status(
            wifi_status_text(status),
            "Wait " + str(max_wait)
        )

        if status == network.STAT_GOT_IP:
            ip = wlan.ifconfig()[0]
            print("Connected")
            print("IP address:", ip)
            show_wifi_status("Connected", ip)
            return True

        if status == network.STAT_WRONG_PASSWORD:
            print("Wrong WiFi password")
            show_wifi_status("Wrong password", "")
            return False

        if status == network.STAT_NO_AP_FOUND:
            print("WiFi network not found")
            show_wifi_status("No AP found", ssid)
            return False

        if status == network.STAT_CONNECT_FAIL:
            print("Connection failed")
            show_wifi_status("Connect failed", "")
            return False

        max_wait -= 1
        time.sleep(1)

    print("WiFi connection timed out")
    print("Final WiFi status:", wlan.status(), wifi_status_text(wlan.status()))
    show_wifi_status("WiFi timeout", wifi_status_text(wlan.status()))

    return False

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
        smtp.to(recipient_email_1)
        smtp.to(recipient_email_2)

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


def handle_bulb_control(door_state, lock_state):
    """
    Controls ONLY the bulb relay.

    Actual door switch behavior from testing:
        Door open   = 0 = ON
        Door closed = 1 = OFF

    Lock switch behavior:
        Lock ON/pressed = 0
        Lock OFF        = 1

    Bulb turns ON when:
    1. Door changes from OPEN to CLOSED
    2. Then lock changes from OFF to ON

    Bulb turns OFF when:
    1. Door changes from CLOSED to OPEN
    2. Then door changes from OPEN to CLOSED
    """

    global previous_door_state_for_bulb
    global previous_lock_state_for_bulb
    global waiting_for_lock_to_turn_bulb_on
    global waiting_for_door_to_close_to_turn_bulb_off
    global bulb_is_on

    # --------------------------------------------------------
    # TURN BULB ON SEQUENCE
    # Door OPEN -> CLOSED, then Lock OFF -> ON
    # Door open = 0, Door closed = 1
    # --------------------------------------------------------

    if not bulb_is_on:

        # Step 1: Door changes from OPEN to CLOSED
        if previous_door_state_for_bulb == 0 and door_state == 1:
            waiting_for_lock_to_turn_bulb_on = True
            print("Bulb sequence: Door changed from OPEN to CLOSED")
            print("Waiting for lock switch to change from OFF to ON")

        # Step 2: Lock changes from OFF to ON
        if waiting_for_lock_to_turn_bulb_on:
            if previous_lock_state_for_bulb == 1 and lock_state == 0:
                bulb_relay.value(1)
                bulb_is_on = True
                waiting_for_lock_to_turn_bulb_on = False

                print("Bulb sequence: Lock changed from OFF to ON")
                print("Bulb relay: ON")

        # Cancel ON sequence if door opens again before lock is turned ON
        if waiting_for_lock_to_turn_bulb_on:
            if previous_door_state_for_bulb == 1 and door_state == 0:
                waiting_for_lock_to_turn_bulb_on = False
                print("Bulb ON sequence cancelled because door opened again")

    # --------------------------------------------------------
    # TURN BULB OFF SEQUENCE
    # Door CLOSED -> OPEN, then Door OPEN -> CLOSED
    # This means the person opened and then closed the door when leaving.
    # --------------------------------------------------------

    if bulb_is_on:

        # Step 1: Door changes from CLOSED to OPEN
        if previous_door_state_for_bulb == 1 and door_state == 0:
            waiting_for_door_to_close_to_turn_bulb_off = True
            print("Bulb OFF sequence: Door changed from CLOSED to OPEN")
            print("Waiting for door to close again")

        # Step 2: Door changes from OPEN to CLOSED
        if waiting_for_door_to_close_to_turn_bulb_off:
            if previous_door_state_for_bulb == 0 and door_state == 1:
                bulb_relay.value(0)
                bulb_is_on = False
                waiting_for_door_to_close_to_turn_bulb_off = False

                print("Bulb OFF sequence: Door changed from OPEN to CLOSED")
                print("Bulb relay: OFF")

    previous_door_state_for_bulb = door_state
    previous_lock_state_for_bulb = lock_state

# ============================================================
# DASHBOARD HTTP SERVER
# ============================================================

CORS_HEADERS = (
    "Access-Control-Allow-Origin: *\r\n"
    "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
    "Access-Control-Allow-Headers: Content-Type\r\n"
)


def http_respond(conn, status_line, body=""):
    response = (
        "HTTP/1.1 " + status_line + "\r\n"
        + CORS_HEADERS
        + "Content-Type: application/json\r\n"
        + "Connection: close\r\n"
        + "Content-Length: " + str(len(body)) + "\r\n"
        + "\r\n"
        + body
    )

    conn.send(response.encode())

def handle_recognition_payload(body):
    """
    Handles JSON sent from the React facial recognition dashboard.

    Expected JSON:
    {
        "name": "Mitch Macharia",
        "confidence": 84,
        "ts": 1234567890
    }

    The Pico only shows the welcome message if the recognized name
    is different from the last welcomed person.
    """

    global latest_recognized_name
    global latest_recognition_confidence
    global welcome_display_until
    global last_welcomed_name

    try:
        data = json.loads(body)

        name = data.get("name", "")
        confidence = data.get("confidence", 0)

        if not name:
            return False, "Missing name"

        name = str(name)

        try:
            confidence = int(confidence)
        except:
            confidence = 0

        latest_recognized_name = name
        latest_recognition_confidence = confidence

        print("Recognition received:")
        print("Name:", latest_recognized_name)
        print("Confidence:", latest_recognition_confidence)

        # Do not redisplay welcome message for the same person
        if name == last_welcomed_name:
            print("Same person already welcomed. Welcome message not repeated.")
            return True, "Same person already welcomed"

        # New person detected, show welcome message
        last_welcomed_name = name
        welcome_display_until = ticks_ms() + WELCOME_DISPLAY_TIME_MS

        print("New person welcomed:", name)

        return True, "Recognition received"

    except Exception as e:
        print("Recognition JSON error:", e)
        return False, "Invalid recognition JSON"

def handle_http_client(conn):
    try:
        conn.settimeout(2)

        request = conn.recv(1024)

        if not request:
            return

        request_text = request.decode()
        header_end = request_text.find("\r\n\r\n")

        if header_end == -1:
            http_respond(
                conn,
                "400 Bad Request",
                json.dumps({"error": "bad request"})
            )
            return

        headers_text = request_text[:header_end]
        body = request_text[header_end + 4:]

        lines = headers_text.split("\r\n")
        request_line = lines[0]
        parts = request_line.split(" ")

        if len(parts) < 2:
            http_respond(
                conn,
                "400 Bad Request",
                json.dumps({"error": "bad request"})
            )
            return

        method = parts[0]
        path = parts[1]

        print("REQUEST RECEIVED:", method, path)

        # Read Content-Length for POST body
        content_length = 0

        for line in lines[1:]:
            lower_line = line.lower()

            if lower_line.startswith("content-length:"):
                try:
                    content_length = int(line.split(":", 1)[1].strip())
                except:
                    content_length = 0

        # If body was not fully received in first recv(), read the rest
        while len(body) < content_length:
            more_data = conn.recv(1024)

            if not more_data:
                break

            body += more_data.decode()

        if method == "OPTIONS":
            http_respond(conn, "204 No Content", "")

        elif method == "GET" and path == "/status":
            response_body = json.dumps(shared_status)
            http_respond(conn, "200 OK", response_body)

        elif method == "POST" and path == "/recognition":
            success, message = handle_recognition_payload(body)

            if success:
                http_respond(
                    conn,
                    "200 OK",
                    json.dumps({
                        "ok": True,
                        "message": message,
                        "name": latest_recognized_name,
                        "confidence": latest_recognition_confidence
                    })
                )
            else:
                http_respond(
                    conn,
                    "400 Bad Request",
                    json.dumps({
                        "ok": False,
                        "error": message
                    })
                )

        else:
            http_respond(
                conn,
                "404 Not Found",
                json.dumps({"error": "not found"})
            )

    except Exception as e:
        print("HTTP error:", e)

    finally:
        try:
            conn.close()
        except:
            pass


def start_dashboard_server():
    """
    Starts a small HTTP server on the Pico W.
    The dashboard can read status using:

        GET /status
    """

    try:
        addr = socket.getaddrinfo("0.0.0.0", 80)[0][-1]

        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(addr)
        server.listen(2)

        print("Dashboard server running on port 80")

    except Exception as e:
        print("Failed to start dashboard server:", e)
        return

    while True:
        try:
            conn, client = server.accept()
            handle_http_client(conn)

        except Exception as e:
            print("Dashboard server accept error:", e)

# ============================================================
# FLOOR SWITCH STATE TRACKING
# ============================================================


previous_floor_state = update_floor_switch_debounce(read_combined_floor_state())

# ============================================================
# MAIN LOOP
# ============================================================

init_system()

# Start with separation gate closed
# Move gate gently to known closed position during startup
startup_move_gate_to_known_position()

# If someone is already standing on the floor at startup, open the gate
if update_floor_switch_debounce(read_combined_floor_state()) == 1:
    print("Floor already pressed at startup. Opening gate.")
    open_both_servos(delay=0.005)
    current_gate_state = GATE_OPEN
    shared_status["solenoidValve"] = True
    save_gate_state(GATE_OPEN)

user_count = load_user_count()
print("Saved user count:", user_count)

# Load saved email alert states
urine_email_sent = load_email_state(URINE_EMAIL_STATE_FILE)
poop_email_sent = load_email_state(POOP_EMAIL_STATE_FILE)

print("Saved urine email state:", urine_email_sent)
print("Saved poop email state:", poop_email_sent)

# Connect to WiFi
wifi_connected = connect_to_internet(WIFI_SSID, WIFI_PASSWORD)

if wifi_connected:
    try:
        _thread.start_new_thread(start_dashboard_server, ())
        print("Dashboard server started")
    except Exception as e:
        print("Could not start dashboard server:", e)
else:
    print("WiFi not connected. Dashboard server not started.")

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
    raw_floor_state, door_state, lock_state = read_switch_states()

    # Debounce floor switch before using it for gate control
    floor_state = update_floor_switch_debounce(raw_floor_state)

    handle_user_counting(door_state, lock_state)
    handle_floor_switch_state_change(floor_state)

    # Continue any non-blocking flush/close sequence
    process_flush_close_sequence(floor_state)
    
    # Door and lock sequence controls ONLY the bulb relay
    handle_bulb_control(door_state, lock_state)
    
    # Always update these, even if tank readings are not stable
    shared_status["userCount"] = user_count
    shared_status["bulbRelay"] = bool(bulb_relay.value())
    
    # Update flush tank water status for dashboard
    update_enough_water_status()

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
        
        update_shared_status(urine_tank_level, poop_tank_level)

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
        # Show welcome message if facial recognition was recently received
        if no_water_message_is_active():
            pass

        elif welcome_message_is_active():
            show_welcome_message(
                latest_recognized_name,
                latest_recognition_confidence
            )
        else:
            show_tank_levels(stable_urine_tank_distance, stable_poop_tank_distance)

    else:
        if no_water_message_is_active():
            pass

        elif welcome_message_is_active():
            show_welcome_message(
                latest_recognized_name,
                latest_recognition_confidence
            )
        else:
            display.fill(0)
            display.text("Stabilizing...", 0, 0)
            display.text("Please wait", 0, 20)
            display.show()      

    print("--------------------")
    sleep_ms(200)
