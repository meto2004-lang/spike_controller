"""Program templates and examples shared by both front-ends.

Keeping them here means the desktop app and the browser app generate the same
race program, and the examples drawer shows exactly what the desktop help text
describes.
"""
from __future__ import annotations

RACE_PROGRAM_HEAD = """\
from pybricks.hubs import PrimeHub
from pybricks.pupdevices import Motor, ColorSensor
from pybricks.parameters import Port, Direction, Color
from pybricks.robotics import DriveBase
from pybricks.tools import wait

hub = PrimeHub()
left_motor  = Motor(Port.A, Direction.COUNTERCLOCKWISE)
right_motor = Motor(Port.B)
left_sensor  = ColorSensor(Port.C)
right_sensor = ColorSensor(Port.D)
robot = DriveBase(left_motor, right_motor, 62.4, 110)
robot.use_gyro(True)
hub.imu.reset_heading(0)

# --- start of the run ---
hub.light.on(Color.GREEN)
"""

RACE_PROGRAM_TAIL = """
# --- end of the run ---
robot.stop()
hub.light.on(Color.BLUE)
hub.speaker.beep(900, 200)
"""

# The desktop app splits SETUP_TEMPLATE on the start marker; keep it intact.
SETUP_TEMPLATE = RACE_PROGRAM_HEAD + RACE_PROGRAM_TAIL

STARTER_PROGRAM = """\
# Welcome to Hyena Control.
# Write a Pybricks program here, then press "Download and run" (F5).

from pybricks.hubs import PrimeHub
from pybricks.parameters import Color
from pybricks.tools import wait

hub = PrimeHub()
hub.light.on(Color.GREEN)

for i in range(3):
    print("Hello from SPIKE Prime", i)
    wait(500)
"""

DEMO_PROGRAM = """\
from pybricks.hubs import PrimeHub
from pybricks.pupdevices import Motor
from pybricks.parameters import Port, Direction, Color
from pybricks.robotics import DriveBase
from pybricks.tools import wait

hub = PrimeHub()
left = Motor(Port.A, Direction.COUNTERCLOCKWISE)
right = Motor(Port.B)
robot = DriveBase(left, right, 62.4, 110)
robot.use_gyro(True)

hub.light.on(Color.GREEN)
print("square driving demo")
for side in range(4):
    robot.straight(400)
    robot.turn(90)
    print("corner", side + 1)
robot.stop()
hub.light.on(Color.BLUE)
"""

# Bilingual example list for the examples drawer.
EXAMPLES = [
    {
        "id": "hello",
        "name": {"en": "Hello hub", "ar": "مرحباً بالروبوت"},
        "description": {
            "en": "Print a message and light the status light.",
            "ar": "طباعة رسالة وإضاءة مصباح الحالة.",
        },
        "code": """\
from pybricks.hubs import PrimeHub
from pybricks.parameters import Color

hub = PrimeHub()
hub.light.on(Color.GREEN)
print("Hello, SPIKE Prime!")
""",
    },
    {
        "id": "drive",
        "name": {"en": "Straight and turn", "ar": "تقدم واستدارة"},
        "description": {
            "en": "Drive base setup with a gyro-assisted 90° turn.",
            "ar": "إعداد قاعدة القيادة مع استدارة 90° بمساعدة الجايرو.",
        },
        "code": """\
from pybricks.hubs import PrimeHub
from pybricks.pupdevices import Motor
from pybricks.parameters import Port, Direction
from pybricks.robotics import DriveBase

hub = PrimeHub()
left = Motor(Port.A, Direction.COUNTERCLOCKWISE)
right = Motor(Port.B)
robot = DriveBase(left, right, 62.4, 110)
robot.use_gyro(True)

robot.straight(500)
robot.turn(90)
robot.straight(500)
robot.stop()
""",
    },
    {
        "id": "sensors",
        "name": {"en": "Read line sensors", "ar": "قراءة حساسات الخط"},
        "description": {
            "en": "Sample two colour sensors in a loop.",
            "ar": "قراءة حساسين لونيين في حلقة.",
        },
        "code": """\
from pybricks.hubs import PrimeHub
from pybricks.pupdevices import ColorSensor
from pybricks.parameters import Port
from pybricks.tools import wait

hub = PrimeHub()
left_sensor = ColorSensor(Port.C)
right_sensor = ColorSensor(Port.D)

for _ in range(20):
    print("LEFT", left_sensor.reflection(), "RIGHT", right_sensor.reflection())
    wait(100)
""",
    },
    {
        "id": "training",
        "name": {"en": "Training recorder", "ar": "مسجّل التدريب"},
        "description": {
            "en": "Buttons record movements; CENTER prints the whole run.",
            "ar": "الأزرار تسجل الحركات، وزر الوسط يطبع التسجيل كاملاً.",
        },
        "code": """\
from pybricks.hubs import PrimeHub
from pybricks.parameters import Button
from pybricks.tools import wait

hub = PrimeHub()
started = False
print("Training recorder ready")
print("RIGHT = move, LEFT = sensors, CENTER = print run")

while True:
    pressed = hub.buttons.pressed()
    if Button.CENTER in pressed:
        print("--- start of recording ---")
        started = True
    wait(50)
""",
    },
]
