# ============================================================
# SMART ATTENDANCE SYSTEM
# 8 LECTURES / DAY
# LECTURE IN + TEMPORARY OUT/IN + AUTOMATIC FINAL OUT
# Windows camera support: DirectShow (CAP_DSHOW)
# ============================================================

import os
import json
import sqlite3
import urllib.request
import hashlib
import shutil
import webbrowser
import smtplib
from email.message import EmailMessage
from urllib.parse import quote
from datetime import datetime, time, timedelta

import cv2
import numpy as np
import pandas as pd

import tkinter as tk
from PIL import Image, ImageTk
from tkinter import ttk, messagebox, filedialog, simpledialog


# ============================================================
# SETTINGS
# ============================================================

IMAGE_DIR = "student_images"
MODEL_FILE = "face_model.yml"
LABEL_FILE = "face_labels.npy"

STUDENT_FILE = "students.csv"
TIMETABLE_FILE = "lecture_timetable.json"
ATTENDANCE_FILE = "lecture_attendance.csv"
MOVEMENT_FILE = "lecture_movement.csv"
SQLITE_FILE = "smart_attendance.db"

CASCADE_FILE = "haarcascade_frontalface_default.xml"
CASCADE_URL = (
    "https://raw.githubusercontent.com/opencv/opencv/master/"
    "data/haarcascades/haarcascade_frontalface_default.xml"
)

CONFIDENCE_LIMIT = 70
MAX_LECTURES = 8

# Attendance timing rules
ATTENDANCE_GRACE_MINUTES = 5
FINAL_OUT_WINDOW_MINUTES = 5

# Camera indexes to try.
CAMERA_INDEXES = [0, 1]

# Admin login (change these two values when you want to change the login).
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "1234"


# ============================================================
# AUTOMATIC HOLIDAY DETECTION
# ============================================================

# Official / gazetted holidays used by the calendar for 2026.
AUTOMATIC_GAZETTED_HOLIDAYS_2026 = {
    "2026-01-26": "Republic Day",
    "2026-03-04": "Holi",
    "2026-03-21": "Id-ul-Fitr",
    "2026-03-26": "Ram Navami",
    "2026-03-31": "Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-05-01": "Buddha Purnima",
    "2026-05-27": "Id-ul-Zuha (Bakrid)",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-08-26": "Milad-un-Nabi / Id-e-Milad",
    "2026-09-04": "Janmashtami",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra (Vijay Dashami)",
    "2026-11-08": "Diwali (Deepavali)",
    "2026-11-24": "Guru Nanak's Birthday",
    "2026-12-25": "Christmas Day",
}

AUTOMATIC_REFERENCE_HOLIDAYS_2026 = {
    "2026-01-03": "Hazrat Ali Jayanti",
    "2026-02-15": "Maha Shivaratri",
    "2026-03-03": "Holika Dahan",
    "2026-04-14": "Dr. B. R. Ambedkar Jayanti",
    "2026-08-28": "Raksha Bandhan",
    "2026-10-26": "Maharishi Valmiki Jayanti",
    "2026-11-09": "Govardhan Puja",
    "2026-11-11": "Bhai Dooj",
    "2026-12-23": "Hazrat Ali Jayanti",
}


def automatic_holiday_status(target_date=None):
    """Return (is_holiday, holiday_name, holiday_type) automatically.

    Priority: College Custom > Official/Gazetted > Sunday > Reference.
    The legacy SQLite 'holidays' table is also checked so older manual
    holiday entries continue to work.
    """
    target_date = target_date or datetime.now().date()
    date_str = target_date.strftime("%Y-%m-%d")

    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT holiday_name FROM college_holidays WHERE holiday_date=?",
                (date_str,)
            ).fetchone()
        if row and str(row[0]).strip():
            return True, str(row[0]), "College Custom"
    except Exception:
        pass

    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT Reason FROM holidays WHERE Holiday_Date=?",
                (date_str,)
            ).fetchone()
        if row and str(row[0]).strip():
            return True, str(row[0]), "College Custom"
    except Exception:
        pass

    if target_date.year == 2026:
        if date_str in AUTOMATIC_GAZETTED_HOLIDAYS_2026:
            return True, AUTOMATIC_GAZETTED_HOLIDAYS_2026[date_str], "Official / Gazetted"

    # Sunday is always an automatic weekly holiday.
    if target_date.weekday() == 6:
        return True, "Sunday", "Sunday"

    if target_date.year == 2026 and date_str in AUTOMATIC_REFERENCE_HOLIDAYS_2026:
        return True, AUTOMATIC_REFERENCE_HOLIDAYS_2026[date_str], "Reference / Optional"

    return False, "", "Working Day"


# ============================================================
# CSV COLUMNS
# ============================================================

STUDENT_COLUMNS = [
    "Student_ID",
    "Name",
    "Roll_No",
    "Course",
    "Semester",
    "Section",
    "Parent_Mobile",
    "Parent_Email"
]

ATTENDANCE_COLUMNS = [
    "Student_ID",
    "Name",
    "Course",
    "Date",
    "Lecture_No",
    "Lecture_Start",
    "Lecture_End",
    "In_Time",
    "Out_Time",
    "Status"
]

MOVEMENT_COLUMNS = [
    "Student_ID",
    "Name",
    "Course",
    "Date",
    "Lecture_No",
    "Movement_Type",
    "Time",
    "Reason"
]


# ============================================================
# FILE SETUP
# ============================================================

def migrate_student_schema():
    """Upgrade older students.csv files to the college schema."""
    required = [
        "Student_ID", "Name", "Roll_No",
        "Course", "Semester", "Section",
        "Parent_Mobile", "Parent_Email"
    ]

    if not os.path.exists(STUDENT_FILE):
        return

    try:
        df = pd.read_csv(STUDENT_FILE, dtype=str).fillna("")

        if "Class" in df.columns and "Course" not in df.columns:
            df["Course"] = df["Class"]

        for col in required:
            if col not in df.columns:
                df[col] = ""

        df = df[required]
        df.to_csv(STUDENT_FILE, index=False)
    except Exception:
        pass


def _repair_csv_schema(filename, required_columns):
    """
    Make sure an existing CSV has the columns required by the program.
    Existing matching data is preserved; missing columns are added blank.
    Older common column spellings are also normalized.
    """
    try:
        if not os.path.exists(filename):
            pd.DataFrame(columns=required_columns).to_csv(
                filename, index=False
            )
            return

        df = pd.read_csv(filename, dtype=str).fillna("")

        # Normalize common old column names.
        aliases = {
            "Student ID": "Student_ID",
            "StudentID": "Student_ID",
            "student_id": "Student_ID",
            "Name ": "Name",
            "Lecture": "Lecture_No",
            "Lecture No": "Lecture_No",
            "Lecture_Number": "Lecture_No",
            "Start_Time": "Lecture_Start",
            "End_Time": "Lecture_End",
            "IN_Time": "In_Time",
            "OUT_Time": "Out_Time",
        }

        df.rename(
            columns={
                col: aliases.get(col, col)
                for col in df.columns
            },
            inplace=True
        )

        # Add any missing required columns without deleting existing data.
        for col in required_columns:
            if col not in df.columns:
                df[col] = ""

        # Keep the exact schema expected by the program.
        df = df[required_columns]
        df.to_csv(filename, index=False)

    except Exception as exc:
        print(
            f"Warning: repairing {filename} failed: {exc}"
        )
        # Last-resort clean file so the application can still start.
        pd.DataFrame(
            columns=required_columns
        ).to_csv(filename, index=False)


def initialize_files():
    os.makedirs(IMAGE_DIR, exist_ok=True)

    if not os.path.exists(STUDENT_FILE):
        pd.DataFrame(
            columns=STUDENT_COLUMNS
        ).to_csv(STUDENT_FILE, index=False)

    # IMPORTANT: repair old attendance/movement CSV headers automatically.
    # This prevents KeyError such as: 'Student_ID'.
    _repair_csv_schema(
        ATTENDANCE_FILE,
        ATTENDANCE_COLUMNS
    )

    _repair_csv_schema(
        MOVEMENT_FILE,
        MOVEMENT_COLUMNS
    )


def ensure_cascade():
    if os.path.exists(CASCADE_FILE):
        return True

    print("\nDownloading Haar Cascade...")

    try:
        urllib.request.urlretrieve(
            CASCADE_URL,
            CASCADE_FILE
        )
        print("Haar Cascade downloaded.")
        return True

    except Exception as e:
        print("Could not download Haar Cascade.")
        print("Error:", e)
        return False


# ============================================================
# CAMERA
# ============================================================

def open_camera():
    """
    Windows-safe camera opening.
    First tries DirectShow camera 0, then camera 1.
    """

    for index in CAMERA_INDEXES:
        print(f"Trying camera {index}...")

        cap = cv2.VideoCapture(
            index,
            cv2.CAP_DSHOW
        )

        if cap.isOpened():
            cap.set(
                cv2.CAP_PROP_FRAME_WIDTH,
                640
            )
            cap.set(
                cv2.CAP_PROP_FRAME_HEIGHT,
                480
            )

            # Test a real frame.
            for _ in range(10):
                ok, frame = cap.read()

                if ok and frame is not None:
                    print(f"Camera {index} opened successfully.")
                    return cap

            cap.release()

    print("\nERROR: Camera could not be opened.")
    print("Check:")
    print("1. Camera is connected.")
    print("2. No other program is using the camera.")
    print("3. Windows Camera permission is ON.")
    print("4. Camera works in the Windows Camera app.")

    return None


# ============================================================
# GUI FACE CAPTURE
# ============================================================

def capture_face_samples(student_id, name):
    """Capture face images for a student registered from the GUI."""
    person_dir = os.path.join(
        IMAGE_DIR,
        str(student_id)
    )
    os.makedirs(person_dir, exist_ok=True)

    cap = open_camera()
    if cap is None:
        messagebox.showerror(
            "Camera Error",
            "Camera could not be opened.\n\n"
            "Check camera permission and make sure another app is not using it."
        )
        return False

    if not ensure_cascade():
        cap.release()
        cv2.destroyAllWindows()
        messagebox.showerror(
            "Face Detection Error",
            "Haar Cascade could not be loaded."
        )
        return False

    cascade = cv2.CascadeClassifier(CASCADE_FILE)
    count = 0
    target = 20

    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        detected = cascade.detectMultiScale(
            gray,
            scaleFactor=1.2,
            minNeighbors=5,
            minSize=(100, 100)
        )

        for (x, y, w, h) in detected:
            cv2.rectangle(
                frame,
                (x, y),
                (x + w, y + h),
                (0, 255, 0),
                2
            )

        cv2.putText(
            frame,
            f"Student: {name}",
            (20, 35),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (0, 255, 0),
            2
        )
        cv2.putText(
            frame,
            f"Saved: {count}/{target}   S=Save   Q=Finish",
            (20, 70),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            2
        )

        cv2.imshow("Register Student Face", frame)
        key = cv2.waitKey(1) & 0xFF

        if key == ord("q"):
            break

        if key == ord("s"):
            if len(detected) == 0:
                print("No face detected. Please face the camera.")
                continue

            # Save the detected face crop rather than the whole frame.
            x, y, w, h = max(
                detected,
                key=lambda box: box[2] * box[3]
            )
            face_crop = gray[y:y + h, x:x + w]

            if face_crop.size == 0:
                continue

            path = os.path.join(
                person_dir,
                f"{count}.jpg"
            )
            cv2.imwrite(path, face_crop)
            count += 1
            print(f"Face image saved: {count}")

            if count >= target:
                break

    cap.release()
    cv2.destroyAllWindows()

    if count == 0:
        messagebox.showwarning(
            "Face Registration",
            f"No face images were saved for {name}."
        )
        return False

    messagebox.showinfo(
        "Face Registration Complete",
        f"{count} face images saved for {name}.\n\n"
        "Now use TRAIN FACE MODEL from the main menu."
    )
    return True


# ============================================================
# TIME
# ============================================================

def input_time(prompt):
    while True:
        value = input(prompt).strip().upper()

        try:
            return datetime.strptime(
                value,
                "%I:%M %p"
            ).time()

        except ValueError:
            print(
                "Invalid format. Example: 09:00 AM"
            )


def setup_lectures():
    """
    Enter up to 8 lectures.
    Each lecture has its own IN window and FINAL OUT time.

    Example:
    Lecture 1: 09:00 AM - 09:50 AM
    Lecture 2: 10:00 AM - 10:50 AM
    """

    lectures = []

    print("\n" + "=" * 70)
    print("8 LECTURE TIMETABLE")
    print("=" * 70)

    for number in range(1, MAX_LECTURES + 1):

        print(f"\nLecture {number}")

        start = input_time(
            "Start Time (HH:MM AM/PM): "
        )

        end = input_time(
            "End Time   (HH:MM AM/PM): "
        )

        if start >= end:
            print(
                "End time must be after start time."
            )
            print("Please enter this lecture again.")
            continue

        if lectures:
            previous = lectures[-1]

            if start <= previous["end"]:
                print(
                    "Lecture times cannot overlap."
                )
                print("Please enter this lecture again.")
                continue

        lectures.append({
            "number": number,
            "start": start,
            "end": end
        })

    return lectures



def default_lectures():
    """Create eight empty lecture slots for the GUI timetable editor."""
    empty = datetime.strptime("12:00 AM", "%I:%M %p").time()
    return [
        {"number": i, "start": empty, "end": empty}
        for i in range(1, MAX_LECTURES + 1)
    ]


def load_timetable():
    """Load the eight-lecture timetable saved by the GUI."""
    lectures = default_lectures()
    if not os.path.exists(TIMETABLE_FILE):
        return lectures, False

    try:
        with open(TIMETABLE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        loaded = []
        for item in data:
            start = datetime.strptime(
                item["start"], "%I:%M %p"
            ).time()
            end = datetime.strptime(
                item["end"], "%I:%M %p"
            ).time()
            loaded.append({
                "number": int(item["number"]),
                "start": start,
                "end": end
            })

        if len(loaded) != MAX_LECTURES:
            return lectures, False

        return loaded, True
    except Exception:
        return lectures, False


def save_timetable(lectures):
    """Save the GUI timetable in a small JSON file."""
    data = []
    for lecture in lectures:
        data.append({
            "number": lecture["number"],
            "start": lecture["start"].strftime("%I:%M %p"),
            "end": lecture["end"].strftime("%I:%M %p")
        })

    with open(TIMETABLE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def active_lecture(lectures, current=None):
    if current is None:
        current = datetime.now().time()

    for lecture in lectures:
        if (
            lecture["start"]
            <= current
            <= lecture["end"]
        ):
            return lecture

    return None


def lecture_in_window(lecture, current=None):
    """
    Normal attendance is allowed only from lecture start
    through exactly 5 minutes after lecture start.
    """
    if current is None:
        current = datetime.now().time()

    start_dt = datetime.combine(
        datetime.today(),
        lecture["start"]
    )
    grace_end_dt = start_dt + timedelta(
        minutes=ATTENDANCE_GRACE_MINUTES
    )
    current_dt = datetime.combine(
        datetime.today(),
        current
    )

    return start_dt <= current_dt <= grace_end_dt


def lecture_late_window(lecture, current=None):
    """
    After the 5-minute Present window, the student is Late
    until the lecture ends.
    """
    if current is None:
        current = datetime.now().time()

    start_dt = datetime.combine(
        datetime.today(),
        lecture["start"]
    )
    late_start_dt = start_dt + timedelta(
        minutes=ATTENDANCE_GRACE_MINUTES
    )
    end_dt = datetime.combine(
        datetime.today(),
        lecture["end"]
    )
    current_dt = datetime.combine(
        datetime.today(),
        current
    )

    return late_start_dt < current_dt <= end_dt


def final_out_window(lecture, current=None):
    """
    Final OUT becomes available during the last 5 minutes
    of the lecture.
    """
    if current is None:
        current = datetime.now().time()

    end_dt = datetime.combine(
        datetime.today(),
        lecture["end"]
    )
    final_out_start_dt = end_dt - timedelta(
        minutes=FINAL_OUT_WINDOW_MINUTES
    )
    current_dt = datetime.combine(
        datetime.today(),
        current
    )

    return (
        final_out_start_dt
        <= current_dt
        <= end_dt
    )


def lecture_finished(lecture):
    return datetime.now().time() > lecture["end"]


# ============================================================
# TEMPORARY REASON
# ============================================================

def temporary_reason():
    while True:
        print("\nTemporary OUT Reason")
        print("1. Toilet")
        print("2. Water")

        choice = input(
            "Select reason (1-2): "
        ).strip()

        if choice == "1":
            return "Toilet"

        if choice == "2":
            return "Water"

        print("Please select 1 or 2.")


# ============================================================
# FACE MODEL
# ============================================================

def check_face_module():
    if not hasattr(cv2, "face"):
        print("\nERROR: cv2.face is not available.")
        print(
            "Install with:\n"
            "pip install opencv-contrib-python"
        )
        return False

    return True


def register_student():
    student_id = input(
        "\nStudent ID: "
    ).strip()

    name = input(
        "Student Name: "
    ).strip()

    student_class = input(
        "Class/Section: "
    ).strip()

    if not student_id or not name:
        print("Student ID and Name are required.")
        return

    students = pd.read_csv(
        STUDENT_FILE,
        dtype=str
    ).fillna("")

    if (
        students["Student_ID"]
        .astype(str)
        .eq(student_id)
        .any()
    ):
        print("This Student ID already exists.")
        return

    new_student = pd.DataFrame([{
        "Student_ID": student_id,
        "Name": name,
        "Course": student_class
    }])

    students = pd.concat(
        [students, new_student],
        ignore_index=True
    )

    students.to_csv(
        STUDENT_FILE,
        index=False
    )

    person_dir = os.path.join(
        IMAGE_DIR,
        student_id
    )

    os.makedirs(
        person_dir,
        exist_ok=True
    )

    cap = open_camera()

    if cap is None:
        return

    print("\nREGISTER FACE")
    print("S = save face image")
    print("Q = finish registration")

    count = 0

    while True:
        ok, frame = cap.read()

        if not ok:
            print("Camera frame could not be read.")
            break

        cv2.imshow(
            "Register Student",
            frame
        )

        key = cv2.waitKey(1) & 0xFF

        if key == ord("q"):
            break

        if key == ord("s"):
            gray = cv2.cvtColor(
                frame,
                cv2.COLOR_BGR2GRAY
            )

            path = os.path.join(
                person_dir,
                f"{count}.jpg"
            )

            cv2.imwrite(
                path,
                gray
            )

            count += 1
            print(
                f"Face image saved: {count}"
            )

    cap.release()
    cv2.destroyAllWindows()

    print(
        f"Registration completed. "
        f"{count} images saved."
    )


def train_model():
    if not check_face_module():
        return

    if not ensure_cascade():
        return

    faces = []
    labels = []

    label_map = {}
    label_number = 0

    if not os.path.exists(IMAGE_DIR):
        print("No student image folder found.")
        return

    for student_id in sorted(
        os.listdir(IMAGE_DIR)
    ):
        person_dir = os.path.join(
            IMAGE_DIR,
            student_id
        )

        if not os.path.isdir(person_dir):
            continue

        label_map[label_number] = str(
            student_id
        )

        for filename in os.listdir(
            person_dir
        ):
            path = os.path.join(
                person_dir,
                filename
            )

            image = cv2.imread(
                path,
                cv2.IMREAD_GRAYSCALE
            )

            if image is None:
                continue

            faces.append(image)
            labels.append(label_number)

        label_number += 1

    if not faces:
        print(
            "No face images found."
        )
        return

    recognizer = (
        cv2.face.LBPHFaceRecognizer_create()
    )

    recognizer.train(
        faces,
        np.array(labels)
    )

    recognizer.write(
        MODEL_FILE
    )

    np.save(
        LABEL_FILE,
        label_map
    )

    print("\nFace model trained successfully.")
    print(
        f"Students in model: {len(label_map)}"
    )


def load_recognition():
    if not check_face_module():
        return None

    if not os.path.exists(MODEL_FILE):
        print(
            "Face model not found. "
            "Train the model first."
        )
        return None

    if not os.path.exists(LABEL_FILE):
        print(
            "Face label file not found."
        )
        return None

    if not ensure_cascade():
        return None

    recognizer = (
        cv2.face.LBPHFaceRecognizer_create()
    )

    recognizer.read(
        MODEL_FILE
    )

    labels = np.load(
        LABEL_FILE,
        allow_pickle=True
    ).item()

    students = pd.read_csv(
        STUDENT_FILE,
        dtype=str
    ).fillna("")

    cascade = cv2.CascadeClassifier(
        CASCADE_FILE
    )

    if cascade.empty():
        print(
            "Could not load Haar Cascade."
        )
        return None

    return (
        recognizer,
        labels,
        students,
        cascade
    )



# ============================================================
# SQLITE DATABASE - DAILY / LECTURE-WISE
# ============================================================

def db_connect():
    conn = sqlite3.connect(SQLITE_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_sqlite():
    """Create a persistent SQLite database for students and 8 lectures/day."""
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS students (
                Student_ID TEXT PRIMARY KEY,
                Name TEXT NOT NULL,
                Roll_No TEXT DEFAULT '',
                Course TEXT DEFAULT '',
                Semester TEXT DEFAULT '',
                Section TEXT DEFAULT '',
                Parent_Mobile TEXT DEFAULT '',
                Parent_Email TEXT DEFAULT ''
            )
        """)
        # Safe migrations for older databases.
        student_cols = {r[1] for r in conn.execute("PRAGMA table_info(students)").fetchall()}
        if "Parent_Mobile" not in student_cols:
            conn.execute("ALTER TABLE students ADD COLUMN Parent_Mobile TEXT DEFAULT ''")
        if "Parent_Email" not in student_cols:
            conn.execute("ALTER TABLE students ADD COLUMN Parent_Email TEXT DEFAULT ''")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS lecture_sessions (
                Attendance_Date TEXT NOT NULL,
                Lecture_No INTEGER NOT NULL,
                Lecture_Start TEXT NOT NULL,
                Lecture_End TEXT NOT NULL,
                PRIMARY KEY (Attendance_Date, Lecture_No)
            )
        """)
        # Critical rule: one row per Student + Date + Lecture.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                Student_ID TEXT NOT NULL,
                Attendance_Date TEXT NOT NULL,
                Lecture_No INTEGER NOT NULL,
                Name TEXT NOT NULL,
                Course TEXT DEFAULT '',
                Lecture_Start TEXT DEFAULT '',
                Lecture_End TEXT DEFAULT '',
                In_Time TEXT DEFAULT '',
                Out_Time TEXT DEFAULT '',
                Status TEXT DEFAULT '',
                PRIMARY KEY (Student_ID, Attendance_Date, Lecture_No)
            )
        """)
        # Movement is an event table: many OUT/IN events per lecture are allowed.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS movement (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Student_ID TEXT NOT NULL,
                Attendance_Date TEXT NOT NULL,
                Lecture_No INTEGER NOT NULL,
                Name TEXT NOT NULL,
                Course TEXT DEFAULT '',
                Movement_Type TEXT NOT NULL,
                Movement_Time TEXT NOT NULL,
                Reason TEXT DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_att_day_lecture ON attendance(Attendance_Date, Lecture_No)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mov_day_lecture ON movement(Attendance_Date, Lecture_No)")
        conn.execute("""CREATE TABLE IF NOT EXISTS user_accounts (
            Username TEXT PRIMARY KEY, Role TEXT NOT NULL, Password_Hash TEXT NOT NULL, Created_At TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            ID INTEGER PRIMARY KEY AUTOINCREMENT, Event_Time TEXT NOT NULL, Username TEXT DEFAULT '', Role TEXT DEFAULT '', Action TEXT NOT NULL, Details TEXT DEFAULT ''
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS holidays (
            Holiday_Date TEXT PRIMARY KEY, Reason TEXT DEFAULT ''
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS notification_settings (
            Setting_Key TEXT PRIMARY KEY, Setting_Value TEXT DEFAULT ''
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS notification_history (
            ID INTEGER PRIMARY KEY AUTOINCREMENT,
            Event_Time TEXT NOT NULL,
            Student_ID TEXT DEFAULT '',
            Student_Name TEXT DEFAULT '',
            Channel TEXT NOT NULL,
            Recipient TEXT DEFAULT '',
            Subject TEXT DEFAULT '',
            Message TEXT DEFAULT '',
            Status TEXT DEFAULT '',
            Details TEXT DEFAULT ''
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notification_history_time ON notification_history(Event_Time DESC)")
        conn.commit()


def sync_students_sqlite():
    try:
        df=pd.read_csv(STUDENT_FILE,dtype=str).fillna('')
        required=['Student_ID','Name','Roll_No','Course','Semester','Section','Parent_Mobile','Parent_Email']
        for c in required:
            if c not in df.columns: df[c]=''
        with db_connect() as conn:
            for _,r in df[required].iterrows():
                conn.execute("""
                    INSERT INTO students(Student_ID,Name,Roll_No,Course,Semester,Section,Parent_Mobile,Parent_Email)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(Student_ID) DO UPDATE SET
                    Name=excluded.Name,Roll_No=excluded.Roll_No,Course=excluded.Course,
                    Semester=excluded.Semester,Section=excluded.Section,
                    Parent_Mobile=excluded.Parent_Mobile,Parent_Email=excluded.Parent_Email
                """, tuple(str(r[c]) for c in required))
            conn.commit()
    except Exception as exc:
        print('SQLite student sync warning:',exc)


def sync_timetable_sqlite(lectures):
    with db_connect() as conn:
        for lec in lectures:
            conn.execute("""
                INSERT INTO lecture_sessions(Attendance_Date,Lecture_No,Lecture_Start,Lecture_End)
                VALUES(?,?,?,?)
                ON CONFLICT(Attendance_Date,Lecture_No) DO UPDATE SET
                Lecture_Start=excluded.Lecture_Start,Lecture_End=excluded.Lecture_End
            """, (today_string(),int(lec['number']),lec['start'].strftime('%H:%M'),lec['end'].strftime('%H:%M')))
        conn.commit()


def sync_attendance_sqlite():
    try:
        df=get_attendance_df()
        with db_connect() as conn:
            for _,r in df.iterrows():
                conn.execute("""
                    INSERT INTO attendance
                    (Student_ID,Attendance_Date,Lecture_No,Name,Course,Lecture_Start,Lecture_End,In_Time,Out_Time,Status)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(Student_ID,Attendance_Date,Lecture_No) DO UPDATE SET
                    Name=excluded.Name,Course=excluded.Course,Lecture_Start=excluded.Lecture_Start,
                    Lecture_End=excluded.Lecture_End,In_Time=excluded.In_Time,Out_Time=excluded.Out_Time,
                    Status=excluded.Status
                """, (
                    str(r.get('Student_ID','')),str(r.get('Date','')),int(r.get('Lecture_No',0) or 0),
                    str(r.get('Name','')),str(r.get('Course','')),str(r.get('Lecture_Start','')),
                    str(r.get('Lecture_End','')),str(r.get('In_Time','')),str(r.get('Out_Time','')),
                    str(r.get('Status',''))))
            conn.commit()
    except Exception as exc:
        print('SQLite attendance sync warning:',exc)


def sync_movement_sqlite():
    try:
        df=get_movement_df()
        with db_connect() as conn:
            conn.execute('DELETE FROM movement')
            for _,r in df.iterrows():
                conn.execute("""
                    INSERT INTO movement(Student_ID,Attendance_Date,Lecture_No,Name,Course,Movement_Type,Movement_Time,Reason)
                    VALUES(?,?,?,?,?,?,?,?)
                """, (
                    str(r.get('Student_ID','')),str(r.get('Date','')),int(r.get('Lecture_No',0) or 0),
                    str(r.get('Name','')),str(r.get('Course','')),str(r.get('Movement_Type','')),
                    str(r.get('Time','')),str(r.get('Reason',''))))
            conn.commit()
    except Exception as exc:
        print('SQLite movement sync warning:',exc)


def initialize_sqlite_layer():
    initialize_sqlite()
    sync_students_sqlite()
    sync_attendance_sqlite()
    sync_movement_sqlite()

# ============================================================
# ATTENDANCE DATABASE HELPERS
# ============================================================

def today_string():
    return datetime.now().strftime(
        "%Y-%m-%d"
    )


def get_attendance_df():
    _repair_csv_schema(
        ATTENDANCE_FILE,
        ATTENDANCE_COLUMNS
    )
    return pd.read_csv(
        ATTENDANCE_FILE,
        dtype=str
    ).fillna("")


def get_movement_df():
    _repair_csv_schema(
        MOVEMENT_FILE,
        MOVEMENT_COLUMNS
    )
    return pd.read_csv(
        MOVEMENT_FILE,
        dtype=str
    ).fillna("")


def find_lecture_record(
    student_id,
    lecture_number
):
    df = get_attendance_df()

    rows = df[
        (df["Student_ID"] == str(student_id))
        &
        (df["Date"] == today_string())
        &
        (
            df["Lecture_No"]
            == str(lecture_number)
        )
    ]

    return df, rows


# ============================================================
# FINAL IN
# ============================================================

def mark_lecture_in(
    student_id,
    name,
    student_class,
    lecture,
    status
):
    df, rows = find_lecture_record(
        student_id,
        lecture["number"]
    )

    now = datetime.now()

    # Same student + same lecture:
    # never create a second lecture attendance row.
    if not rows.empty:
        index = rows.index[-1]

        if str(
            df.loc[index, "In_Time"]
        ).strip():
            return False

        df.loc[
            index,
            "In_Time"
        ] = now.strftime(
            "%H:%M:%S"
        )

        df.loc[
            index,
            "Status"
        ] = status

        df.to_csv(
            ATTENDANCE_FILE,
            index=False
        )
        sync_attendance_sqlite()

        print(
            f"IN: {name} | "
            f"Lecture {lecture['number']}"
        )

        automatic_low_attendance_notification(student_id)
        return True

    row = {
        "Student_ID": student_id,
        "Name": name,
        "Course": student_class,
        "Date": today_string(),
        "Lecture_No": lecture["number"],
        "Lecture_Start": lecture["start"].strftime(
            "%H:%M"
        ),
        "Lecture_End": lecture["end"].strftime(
            "%H:%M"
        ),
        "In_Time": now.strftime(
            "%H:%M:%S"
        ),
        "Out_Time": "",
        "Status": status
    }

    df = pd.concat(
        [
            df,
            pd.DataFrame([row])
        ],
        ignore_index=True
    )

    df.to_csv(
        ATTENDANCE_FILE,
        index=False
    )
    sync_attendance_sqlite()

    print(
        f"IN: {name} | "
        f"Lecture {lecture['number']} | "
        f"Status: {status} | "
        f"{now.strftime('%I:%M:%S %p')}"
    )

    automatic_low_attendance_notification(student_id)
    return True


# ============================================================
# FINAL OUT
# ============================================================

def mark_final_out(
    student_id,
    lecture
):
    df, rows = find_lecture_record(
        student_id,
        lecture["number"]
    )

    if rows.empty:
        return False

    index = rows.index[-1]

    if not str(
        df.loc[index, "In_Time"]
    ).strip():
        return False

    if str(
        df.loc[index, "Out_Time"]
    ).strip():
        return False

    # Final OUT is the lecture's configured END time,
    # not the time when the teacher happens to press a key.
    df.loc[
        index,
        "Out_Time"
    ] = lecture["end"].strftime(
        "%H:%M:%S"
    )

    df.to_csv(
        ATTENDANCE_FILE,
        index=False
    )
    sync_attendance_sqlite()

    print(
        f"FINAL OUT: {df.loc[index, 'Name']} | "
        f"Lecture {lecture['number']} | "
        f"{lecture['end'].strftime('%I:%M %p')}"
    )

    return True


def finalize_finished_lectures(
    lectures
):
    """
    Automatically closes every lecture that has ended.
    This means every lecture has its own FINAL OUT.
    """

    df = get_attendance_df()
    changed = False
    today = today_string()

    for lecture in lectures:
        if not lecture_finished(
            lecture
        ):
            continue

        rows = df[
            (df["Date"] == today)
            &
            (
                df["Lecture_No"]
                == str(lecture["number"])
            )
            &
            (
                df["In_Time"]
                .astype(str)
                .str.strip()
                != ""
            )
            &
            (
                df["Out_Time"]
                .astype(str)
                .str.strip()
                == ""
            )
        ]

        for index in rows.index:
            df.loc[
                index,
                "Out_Time"
            ] = lecture["end"].strftime(
                "%H:%M:%S"
            )

            changed = True

    if changed:
        df.to_csv(
            ATTENDANCE_FILE,
            index=False
        )
        sync_attendance_sqlite()


# ============================================================
# TEMPORARY MOVEMENT
# ============================================================

def get_current_movement_state(
    student_id,
    lecture_number
):
    movements = get_movement_df()

    rows = movements[
        (movements["Student_ID"] == str(student_id))
        &
        (movements["Date"] == today_string())
        &
        (
            movements["Lecture_No"]
            == str(lecture_number)
        )
    ]

    if rows.empty:
        return "IN"

    last_type = str(
        rows.iloc[-1]["Movement_Type"]
    ).upper()

    if last_type == "OUT":
        return "OUT"

    return "IN"


def save_movement(
    student_id,
    name,
    student_class,
    lecture,
    movement_type,
    reason
):
    df = get_movement_df()

    row = {
        "Student_ID": student_id,
        "Name": name,
        "Course": student_class,
        "Date": today_string(),
        "Lecture_No": lecture["number"],
        "Movement_Type": movement_type,
        "Time": datetime.now().strftime(
            "%H:%M:%S"
        ),
        "Reason": reason
    }

    df = pd.concat(
        [
            df,
            pd.DataFrame([row])
        ],
        ignore_index=True
    )

    df.to_csv(
        MOVEMENT_FILE,
        index=False
    )
    sync_movement_sqlite()

    print(
        f"{movement_type}: {name} | "
        f"Lecture {lecture['number']} | "
        f"{reason}"
    )


def temporary_out(
    student_id,
    name,
    student_class,
    lecture
):
    state = get_current_movement_state(
        student_id,
        lecture["number"]
    )

    if state == "OUT":
        print(
            f"{name} is already TEMP OUT."
        )
        return False

    reason = temporary_reason()

    save_movement(
        student_id,
        name,
        student_class,
        lecture,
        "OUT",
        reason
    )

    return True


def temporary_in(
    student_id,
    name,
    student_class,
    lecture
):
    state = get_current_movement_state(
        student_id,
        lecture["number"]
    )

    if state != "OUT":
        print(
            f"{name} is not currently TEMP OUT."
        )
        return False

    save_movement(
        student_id,
        name,
        student_class,
        lecture,
        "IN",
        "Returned"
    )

    return True


# ============================================================
# CURRENT FACE RECOGNITION
# ============================================================

def recognize_faces(
    frame,
    recognizer,
    labels,
    students,
    cascade
):
    gray = cv2.cvtColor(
        frame,
        cv2.COLOR_BGR2GRAY
    )

    detected = cascade.detectMultiScale(
        gray,
        scaleFactor=1.2,
        minNeighbors=5,
        minSize=(80, 80)
    )

    results = []

    for x, y, w, h in detected:

        face = gray[
            y:y+h,
            x:x+w
        ]

        try:
            label, confidence = (
                recognizer.predict(face)
            )
        except Exception:
            continue

        if confidence > CONFIDENCE_LIMIT:
            continue

        student_id = str(
            labels.get(label, "")
        ).strip()

        rows = students[
            students["Student_ID"]
            == student_id
        ]

        if rows.empty:
            continue

        student = rows.iloc[0]

        results.append({
            "student_id": student_id,
            "name": student["Name"],
            "class": student["Course"],
            "box": (x, y, w, h),
            "confidence": confidence
        })

    return results


# ============================================================
# START ATTENDANCE
# ============================================================

def start_attendance(lectures):
    """
    ONE menu option handles:
        1. Lecture IN
        2. Temporary OUT
        3. Temporary IN
        4. Automatic FINAL OUT

    Temporary OUT is activated by pressing T while the
    student's face is visible. There is NO separate
    Temporary Attendance menu option.
    """

    # HARD HOLIDAY BLOCK: do not initialize the camera or attendance
    # engine when today is an automatic/configured holiday.
    is_holiday, holiday_name, holiday_type = automatic_holiday_status()
    if is_holiday:
        try:
            messagebox.showinfo(
                "Holiday - Attendance Disabled",
                f"Today is a holiday.\n\n{holiday_name}\nType: {holiday_type}\n\nStart Attendance is disabled for today."
            )
        except Exception:
            pass
        return

    setup = load_recognition()

    if setup is None:
        return

    (
        recognizer,
        labels,
        students,
        cascade
    ) = setup

    cap = open_camera()

    if cap is None:
        return

    print("\n" + "=" * 70)
    print("START ATTENDANCE")
    print("=" * 70)
    print("Normal lecture IN: face appears during lecture.")
    print("Temporary OUT: press T while that student is visible.")
    print("Final OUT: press F during the last 5 minutes, or it will be available automatically.")
    print("Temporary IN: the same student returns after going OUT.")
    print("Temporary OUT reason: Toilet / Water.")
    print("FINAL OUT window: starts 5 minutes before lecture end and ends at lecture end.")
    print("Q = stop attendance")
    print("=" * 70)

    # Face must disappear before it can be treated as a new
    # appearance. This prevents duplicate IN.
    visible_last_frame = {}

    # T creates a pending temporary OUT request.
    temp_request = False

    # Prevent repeatedly processing T while held.
    previous_key = -1

    while True:
        finalize_finished_lectures(
            lectures
        )

        now = datetime.now().time()
        lecture = active_lecture(
            lectures,
            now
        )

        ok, frame = cap.read()

        if not ok or frame is None:
            print(
                "Camera frame could not be read."
            )
            break

        recognized = recognize_faces(
            frame,
            recognizer,
            labels,
            students,
            cascade
        )

        visible_now = set()

        for person in recognized:
            student_id = person["student_id"]
            name = person["name"]
            student_class = person["class"]

            visible_now.add(
                student_id
            )

            x, y, w, h = person["box"]

            # ------------------------------------------------
            # LECTURE IN: PRESENT FOR FIRST 5 MINUTES,
            # LATE AFTER 5 MINUTES.
            # ------------------------------------------------
            if lecture is not None:

                if not visible_last_frame.get(
                    student_id,
                    False
                ):
                    current_time = datetime.now().time()

                    if lecture_in_window(
                        lecture,
                        current_time
                    ):
                        mark_lecture_in(
                            student_id,
                            name,
                            student_class,
                            lecture,
                            "Present"
                        )

                    elif lecture_late_window(
                        lecture,
                        current_time
                    ):
                        mark_lecture_in(
                            student_id,
                            name,
                            student_class,
                            lecture,
                            "Late"
                        )

            # ------------------------------------------------
            # TEMPORARY IN AFTER RETURN
            # ------------------------------------------------
            if lecture is not None:
                state = get_current_movement_state(
                    student_id,
                    lecture["number"]
                )

                if (
                    state == "OUT"
                    and not visible_last_frame.get(
                        student_id,
                        False
                    )
                ):
                    temporary_in(
                        student_id,
                        name,
                        student_class,
                        lecture
                    )

            # ------------------------------------------------
            # FINAL OUT: AVAILABLE ONLY IN LAST 5 MINUTES
            # ------------------------------------------------
            if (
                lecture is not None
                and final_out_window(
                    lecture,
                    datetime.now().time()
                )
                and not visible_last_frame.get(
                    student_id,
                    False
                )
            ):
                df, rows = find_lecture_record(
                    student_id,
                    lecture["number"]
                )

                if not rows.empty:
                    row_index = rows.index[-1]

                    if (
                        str(
                            df.loc[
                                row_index,
                                "In_Time"
                            ]
                        ).strip()
                        and not str(
                            df.loc[
                                row_index,
                                "Out_Time"
                            ]
                        ).strip()
                    ):
                        mark_final_out(
                            student_id,
                            lecture
                        )

            # ------------------------------------------------
            # TEMPORARY OUT REQUEST
            # ------------------------------------------------
            if (
                temp_request
                and lecture is not None
            ):
                state = get_current_movement_state(
                    student_id,
                    lecture["number"]
                )

                if state == "IN":
                    temporary_out(
                        student_id,
                        name,
                        student_class,
                        lecture
                    )

                    temp_request = False

            cv2.rectangle(
                frame,
                (x, y),
                (x+w, y+h),
                (0, 255, 0),
                2
            )

            text = (
                f"{name} "
                f"({person['confidence']:.0f})"
            )

            cv2.putText(
                frame,
                text,
                (x, y - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.60,
                (0, 255, 0),
                2
            )

        # Update face visibility.
        for student_id in list(
            visible_last_frame.keys()
        ):
            if student_id not in visible_now:
                visible_last_frame[
                    student_id
                ] = False

        for student_id in visible_now:
            visible_last_frame[
                student_id
            ] = True

        # ----------------------------------------------------
        # SCREEN INFORMATION
        # ----------------------------------------------------

        if lecture is None:
            lecture_text = (
                "NO ACTIVE LECTURE"
            )
        else:
            if lecture_in_window(
                lecture,
                datetime.now().time()
            ):
                window_text = "PRESENT WINDOW"
            elif lecture_late_window(
                lecture,
                datetime.now().time()
            ):
                window_text = "LATE WINDOW"
            elif final_out_window(
                lecture,
                datetime.now().time()
            ):
                window_text = "FINAL OUT WINDOW"
            else:
                window_text = "LECTURE RUNNING"

            lecture_text = (
                f"LECTURE {lecture['number']} | "
                f"{lecture['start'].strftime('%I:%M %p')} - "
                f"{lecture['end'].strftime('%I:%M %p')} | "
                f"{window_text}"
            )

        cv2.putText(
            frame,
            lecture_text,
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            2
        )

        cv2.putText(
            frame,
            "T = Temp OUT | F = Final OUT | Q = Quit",
            (10, 60),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            2
        )

        if temp_request:
            cv2.putText(
                frame,
                "TEMP OUT REQUESTED - SHOW STUDENT",
                (10, 90),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 255, 255),
                2
            )

        cv2.imshow(
            "SMART ATTENDANCE SYSTEM",
            frame
        )

        key = cv2.waitKey(1) & 0xFF

        if key == ord("q"):
            break

        # F = Final OUT for the visible student.
        if (
            key == ord("f")
            and previous_key != ord("f")
        ):
            if lecture is None:
                print(
                    "No active lecture. "
                    "Final OUT is unavailable."
                )
            elif not final_out_window(
                lecture,
                datetime.now().time()
            ):
                print(
                    "Final OUT is available only "
                    "during the last 5 minutes of the lecture."
                )
            else:
                for person in recognized:
                    student_id = person["student_id"]
                    mark_final_out(
                        student_id,
                        lecture
                    )

        if (
            key == ord("t")
            and previous_key != ord("t")
        ):
            if lecture is None:
                print(
                    "No active lecture. "
                    "Temporary OUT cannot be recorded."
                )
            else:
                temp_request = True
                print(
                    "\nTemporary OUT requested."
                )
                print(
                    "Show the student's face to the camera."
                )

        previous_key = key

    cap.release()
    cv2.destroyAllWindows()

    # Final safety close for lectures that already ended.
    finalize_finished_lectures(
        lectures
    )


# ============================================================
# REPORTS
# ============================================================

def attendance_report():
    df = get_attendance_df()

    print("\n" + "=" * 120)
    print("LECTURE ATTENDANCE REPORT")
    print("=" * 120)

    if df.empty:
        print("No attendance records.")
    else:
        print(
            df.to_string(
                index=False
            )
        )


def today_report():
    df = get_attendance_df()

    result = df[
        df["Date"] == today_string()
    ]

    print("\n" + "=" * 120)
    print("TODAY'S LECTURE ATTENDANCE")
    print("=" * 120)

    if result.empty:
        print("No attendance records today.")
    else:
        print(
            result.to_string(
                index=False
            )
        )


def movement_report():
    df = get_movement_df()

    print("\n" + "=" * 120)
    print("TEMPORARY OUT / IN REPORT")
    print("=" * 120)

    if df.empty:
        print("No movement records.")
    else:
        print(
            df.to_string(
                index=False
            )
        )


def student_list():
    df = pd.read_csv(
        STUDENT_FILE,
        dtype=str
    ).fillna("")

    print("\n" + "=" * 80)
    print("STUDENT LIST")
    print("=" * 80)

    if df.empty:
        print("No students registered.")
    else:
        print(
            df.to_string(
                index=False
            )
        )


# ============================================================
# MAIN MENU
# ============================================================


# ============================================================
# COLORFUL MODERN DASHBOARD
# ============================================================

def _hash_password(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def _get_user_accounts():
    with db_connect() as conn:
        return conn.execute("SELECT Username, Role, Password_Hash FROM user_accounts").fetchall()

def _users_configured():
    rows = _get_user_accounts()
    roles = {r[1] for r in rows}
    return {"Admin", "Teacher", "Viewer"}.issubset(roles)

def _save_user_accounts(accounts):
    with db_connect() as conn:
        conn.execute("DELETE FROM user_accounts")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for role, username, password in accounts:
            conn.execute("INSERT INTO user_accounts(Username,Role,Password_Hash,Created_At) VALUES(?,?,?,?)",
                         (username.strip(), role, _hash_password(password), now))
        conn.commit()

def setup_user_accounts(root):
    if _users_configured():
        return True
    win=tk.Toplevel(root); win.title("Initial User Setup"); win.geometry("560x620"); win.resizable(False,False); win.configure(bg="#08111F")
    win.transient(root); win.grab_set(); result={"ok":False}
    tk.Label(win,text="SMART ATTENDANCE SYSTEM",font=("Segoe UI",21,"bold"),fg="#FFFFFF",bg="#08111F").pack(pady=(28,5))
    tk.Label(win,text="Create Admin, Teacher and Viewer accounts",font=("Segoe UI",10),fg="#AFC2D8",bg="#08111F").pack(pady=(0,18))
    form=tk.Frame(win,bg="#10243D"); form.pack(fill="x",padx=35,pady=5)
    entries={}
    roles=[("Admin","#FF4D5A"),("Teacher","#FFB020"),("Viewer","#00C2FF")]
    for i,(role,color) in enumerate(roles):
        tk.Label(form,text=role.upper(),font=("Segoe UI",12,"bold"),fg=color,bg="#10243D").grid(row=i*3,column=0,columnspan=2,sticky="w",padx=18,pady=(18,7))
        tk.Label(form,text="Username",fg="#FFFFFF",bg="#10243D").grid(row=i*3+1,column=0,sticky="w",padx=18)
        u=tk.Entry(form,font=("Segoe UI",10)); u.grid(row=i*3+1,column=1,sticky="ew",padx=18,pady=3)
        tk.Label(form,text="Password",fg="#FFFFFF",bg="#10243D").grid(row=i*3+2,column=0,sticky="w",padx=18)
        pw=tk.Entry(form,font=("Segoe UI",10),show="*"); pw.grid(row=i*3+2,column=1,sticky="ew",padx=18,pady=3)
        entries[role]=(u,pw)
    form.columnconfigure(1,weight=1)
    status=tk.Label(win,text="",fg="#FFB020",bg="#08111F"); status.pack(pady=7)
    def save():
        accounts=[]; seen=set()
        for role,(u,pw) in entries.items():
            username=u.get().strip(); password=pw.get()
            if len(username)<3 or len(password)<4:
                status.config(text=f"{role}: username 3+ chars, password 4+ chars required."); return
            if username.lower() in seen: status.config(text="Usernames must be unique."); return
            seen.add(username.lower()); accounts.append((role,username,password))
        _save_user_accounts(accounts); result["ok"]=True; win.destroy()
    tk.Button(win,text="✓ SAVE & CONTINUE",command=save,font=("Segoe UI",11,"bold"),fg="#FFFFFF",bg="#16C784",relief="flat",padx=28,pady=11).pack(pady=14)
    win.protocol("WM_DELETE_WINDOW",lambda:None); root.wait_window(win); return result["ok"]


def audit_event(username, role, action, details=""):
    try:
        with db_connect() as conn:
            conn.execute("INSERT INTO audit_log(Event_Time,Username,Role,Action,Details) VALUES(?,?,?,?,?)",
                         (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),username or "",role or "",action,details)); conn.commit()
    except Exception: pass


# ============================================================
# PARENT / STUDENT NOTIFICATION CENTER
# ============================================================

NOTIFICATION_DEFAULTS = {
    "LOW_ATTENDANCE_THRESHOLD": "75",
    "AUTO_LOW_ATTENDANCE": "1",
    "AUTO_NOTIFICATION_CHANNEL": "SMS",
    "AUTO_ALERT_ONCE_PER_DAY": "1",
    "WHATSAPP_ENABLED": "1",
    "SMS_ENABLED": "0",
    "EMAIL_ENABLED": "0",
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "",
    "SMTP_PASSWORD": "",
    "SMTP_FROM": "",
    "TWILIO_ACCOUNT_SID": "",
    "TWILIO_AUTH_TOKEN": "",
    "TWILIO_FROM": "",
}


def get_notification_setting(key, default=None):
    if default is None:
        default = NOTIFICATION_DEFAULTS.get(key, "")
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT Setting_Value FROM notification_settings WHERE Setting_Key=?",
                (key,)
            ).fetchone()
        return str(row[0]) if row else str(default)
    except Exception:
        return str(default)


def set_notification_setting(key, value):
    with db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO notification_settings(Setting_Key,Setting_Value) VALUES(?,?)",
            (key, str(value))
        )
        conn.commit()


def save_notification_history(student_id, student_name, channel, recipient, subject, message, status, details=""):
    try:
        with db_connect() as conn:
            conn.execute("""
                INSERT INTO notification_history
                (Event_Time,Student_ID,Student_Name,Channel,Recipient,Subject,Message,Status,Details)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(student_id), str(student_name), str(channel), str(recipient),
                str(subject), str(message), str(status), str(details)
            ))
            conn.commit()
    except Exception as exc:
        print("Notification history warning:", exc)


def notification_history_df(limit=500):
    try:
        with db_connect() as conn:
            return pd.read_sql_query(
                "SELECT Event_Time,Student_ID,Student_Name,Channel,Recipient,Subject,Message,Status,Details "
                "FROM notification_history ORDER BY ID DESC LIMIT ?", conn, params=(int(limit),)
            ).fillna("")
    except Exception:
        return pd.DataFrame(columns=["Event_Time","Student_ID","Student_Name","Channel","Recipient","Subject","Message","Status","Details"])


def _student_notification_message(student, attendance_percent, threshold):
    name = str(student.get("Name", "Student")).strip() or "Student"
    sid = str(student.get("Student_ID", "")).strip()
    return (
        f"Dear Parent,\n\n"
        f"Attendance Alert for {name} (Student ID: {sid}).\n"
        f"Current attendance: {attendance_percent:.1f}%\n"
        f"Required threshold: {threshold:.1f}%\n\n"
        f"The attendance is currently below the configured threshold. "
        f"Please ensure regular attendance.\n\n"
        f"Smart Attendance System"
    )


def calculate_student_attendance(student_id):
    try:
        att = get_attendance_df()
        if att.empty:
            return 0.0
        sessions = len(att[["Date", "Lecture_No"]].drop_duplicates()) if "Date" in att.columns and "Lecture_No" in att.columns else 0
        if sessions <= 0:
            return 0.0
        rows = att[att["Student_ID"].astype(str) == str(student_id)]
        good = rows[rows["Status"].astype(str).isin(["Present", "Late"])]
        return len(good) / sessions * 100.0
    except Exception:
        return 0.0


def _notification_already_sent_today(student_id, channel):
    """Prevent duplicate automatic alerts for the same student/channel/day."""
    try:
        today_prefix = datetime.now().strftime("%Y-%m-%d") + "%"
        with db_connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM notification_history
                   WHERE Student_ID=? AND Channel=? AND Status='SENT'
                     AND Details LIKE 'AUTO_LOW_ATTENDANCE%'
                     AND Event_Time LIKE ? LIMIT 1""",
                (str(student_id), str(channel), today_prefix)
            ).fetchone()
        return row is not None
    except Exception:
        return False


def automatic_low_attendance_notification(student_id):
    """
    Automatically notify a student's parent when attendance falls below
    the configured threshold. Runs after a successful lecture IN.

    Supported automatic channels:
      SMS   -> Twilio (fully automatic)
      Email -> SMTP (fully automatic)
      WhatsApp -> opens WhatsApp Web with a pre-filled message; the user
                  must press Send because WhatsApp Web does not provide a
                  generic unattended desktop send API.
    """
    try:
        if get_notification_setting("AUTO_LOW_ATTENDANCE", "1") != "1":
            return

        threshold = float(get_notification_setting("LOW_ATTENDANCE_THRESHOLD", "75") or 75)
        pct = calculate_student_attendance(student_id)
        if pct >= threshold:
            return

        students = pd.read_csv(STUDENT_FILE, dtype=str).fillna("")
        row = students[students["Student_ID"].astype(str) == str(student_id)]
        if row.empty:
            return
        student = row.iloc[0]

        name = str(student.get("Name", "Student")).strip() or "Student"
        mobile = str(student.get("Parent_Mobile", "")).strip()
        email = str(student.get("Parent_Email", "")).strip()
        message = _student_notification_message(student, pct, threshold)
        subject = f"Attendance Alert - {name}"

        channels = get_notification_setting("AUTO_NOTIFICATION_CHANNEL", "SMS")
        channels = [c.strip().lower() for c in channels.replace(";", ",").split(",") if c.strip()]
        once_per_day = get_notification_setting("AUTO_ALERT_ONCE_PER_DAY", "1") == "1"

        for channel in channels:
            if channel == "sms":
                if get_notification_setting("SMS_ENABLED", "0") != "1":
                    continue
                if once_per_day and _notification_already_sent_today(student_id, "SMS"):
                    continue
                ok, detail = send_sms_notification(student_id, name, mobile, message)
                if ok:
                    save_notification_history(student_id, name, "SMS", mobile, subject, message, "SENT",
                                              "AUTO_LOW_ATTENDANCE: automatic threshold alert.")

            elif channel == "email":
                if get_notification_setting("EMAIL_ENABLED", "0") != "1":
                    continue
                if once_per_day and _notification_already_sent_today(student_id, "Email"):
                    continue
                ok, detail = send_email_notification(student_id, name, email, subject, message)
                if ok:
                    save_notification_history(student_id, name, "Email", email, subject, message, "SENT",
                                              "AUTO_LOW_ATTENDANCE: automatic threshold alert.")

            elif channel == "whatsapp":
                if get_notification_setting("WHATSAPP_ENABLED", "0") != "1":
                    continue
                if once_per_day and _notification_already_sent_today(student_id, "WhatsApp"):
                    continue
                # WhatsApp Web requires a user click on Send.
                ok, detail = send_whatsapp_notification(student_id, name, mobile, message)
                if ok:
                    save_notification_history(student_id, name, "WhatsApp", mobile, subject, message, "OPENED",
                                              "AUTO_LOW_ATTENDANCE: WhatsApp Web opened; user must press Send.")
    except Exception as exc:
        print("Automatic low-attendance notification warning:", exc)


def send_whatsapp_notification(student_id, student_name, mobile, message):
    mobile = "".join(ch for ch in str(mobile) if ch.isdigit() or ch == "+")
    if mobile.startswith("+"):
        mobile = mobile[1:]
    if not mobile:
        status = "FAILED"
        details = "Parent mobile number is missing."
        save_notification_history(student_id, student_name, "WhatsApp", "", "", message, status, details)
        return False, details
    try:
        url = f"https://wa.me/{mobile}?text={quote(message)}"
        webbrowser.open(url)
        save_notification_history(student_id, student_name, "WhatsApp", mobile, "", message, "OPENED", "WhatsApp Web opened with pre-filled message; final Send is performed by the user.")
        return True, "WhatsApp Web opened with the message ready to send."
    except Exception as exc:
        save_notification_history(student_id, student_name, "WhatsApp", mobile, "", message, "FAILED", str(exc))
        return False, str(exc)


def send_email_notification(student_id, student_name, email, subject, message):
    email = str(email).strip()
    if not email:
        details = "Parent email is missing."
        save_notification_history(student_id, student_name, "Email", "", subject, message, "FAILED", details)
        return False, details
    host = get_notification_setting("SMTP_HOST")
    port = int(get_notification_setting("SMTP_PORT", "587") or 587)
    user = get_notification_setting("SMTP_USER")
    password = get_notification_setting("SMTP_PASSWORD")
    sender = get_notification_setting("SMTP_FROM") or user
    if not host or not user or not password or not sender:
        details = "SMTP settings are incomplete. Open Notification Settings first."
        save_notification_history(student_id, student_name, "Email", email, subject, message, "FAILED", details)
        return False, details
    try:
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = email
        msg["Subject"] = subject
        msg.set_content(message)
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        save_notification_history(student_id, student_name, "Email", email, subject, message, "SENT", "SMTP delivery accepted by server.")
        return True, "Email sent successfully."
    except Exception as exc:
        save_notification_history(student_id, student_name, "Email", email, subject, message, "FAILED", str(exc))
        return False, str(exc)


def send_sms_notification(student_id, student_name, mobile, message):
    mobile = str(mobile).strip()
    if not mobile:
        details = "Parent mobile number is missing."
        save_notification_history(student_id, student_name, "SMS", "", "", message, "FAILED", details)
        return False, details
    sid = get_notification_setting("TWILIO_ACCOUNT_SID")
    token = get_notification_setting("TWILIO_AUTH_TOKEN")
    from_number = get_notification_setting("TWILIO_FROM")
    if not sid or not token or not from_number:
        details = "Twilio SMS settings are incomplete. Open Notification Settings first."
        save_notification_history(student_id, student_name, "SMS", mobile, "", message, "FAILED", details)
        return False, details
    try:
        from twilio.rest import Client
        client = Client(sid, token)
        result = client.messages.create(body=message, from_=from_number, to=mobile)
        save_notification_history(student_id, student_name, "SMS", mobile, "", message, "SENT", f"Twilio SID: {result.sid}")
        return True, "SMS sent successfully."
    except ImportError:
        details = "Twilio package is not installed. Run: py -3.11 -m pip install twilio"
        save_notification_history(student_id, student_name, "SMS", mobile, "", message, "FAILED", details)
        return False, details
    except Exception as exc:
        save_notification_history(student_id, student_name, "SMS", mobile, "", message, "FAILED", str(exc))
        return False, str(exc)


class SmartAttendanceDashboard:
    def __init__(self, root, lectures, timetable_configured=False):
        self.root = root
        self.lectures = lectures
        self.timetable_configured = timetable_configured
        self.selected_lecture = 0
        self.admin_authenticated = False
        self.logged_username = ""
        self.logged_role = ""

        self.bg = "#F4F7FB"
        self.sidebar = "#172554"
        self.blue = "#2563EB"
        self.green = "#16A34A"
        self.orange = "#F59E0B"
        self.red = "#DC2626"
        self.purple = "#7C3AED"
        self.text = "#172033"
        self.muted = "#64748B"
        self.white = "#FFFFFF"

        self.root.title("Smart Attendance System (SQLite)")
        self.root.attributes("-fullscreen", False)
        self.root.state("zoomed")
        self.root.resizable(True, True)
        self.root.geometry("1400x850")
        self.root.minsize(1100, 700)
        self.root.configure(bg=self.bg)
        self.root.bind("<F11>", lambda e: self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen")))
        self.root.protocol("WM_DELETE_WINDOW", self.close_main_window)
        self.root.bind("<Escape>", lambda e: self.close_main_window())

        self.setup_style()
        self.build_ui()
        self.root.after(100, self.update_clock)
        self.refresh_dashboard()

    def update_clock(self):
        # Header widgets may not exist during the first UI build tick.
        if not hasattr(self, "header_sub"):
            self.root.after(100, self.update_clock)
            return
        now=datetime.now()
        if hasattr(self,"date_label"): self.date_label.configure(text=now.strftime("%A, %d %B %Y"))
        if hasattr(self,"clock_label"): self.clock_label.configure(text=now.strftime("%I:%M:%S %p"))
        self.root.after(1000,self.update_clock)

    def _admin_required(self):
        return self.authenticate_admin()

    def show_analytics(self):
        if not self.logged_role: self.authenticate_role()
        if not self.logged_role: return
        try:
            with db_connect() as conn:
                total=conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
                present=conn.execute("SELECT COUNT(*) FROM attendance WHERE Attendance_Date=? AND In_Time<>''",(datetime.now().strftime('%Y-%m-%d'),)).fetchone()[0]
                late=conn.execute("SELECT COUNT(*) FROM attendance WHERE Attendance_Date=? AND Status='Late'",(datetime.now().strftime('%Y-%m-%d'),)).fetchone()[0]
            messagebox.showinfo("Smart Analytics",f"Students: {total}\nToday's Present/IN records: {present}\nToday's Late records: {late}",parent=self.root)
        except Exception as e: messagebox.showerror("Analytics Error",str(e),parent=self.root)

    def show_low_attendance_alerts(self):
        if not self.logged_role: self.authenticate_role()
        if not self.logged_role: return
        try:
            with db_connect() as conn:
                rows=conn.execute("SELECT Student_ID,Name,Course,COUNT(*) FROM attendance WHERE In_Time<>'' GROUP BY Student_ID,Name,Course").fetchall()
                sessions=conn.execute("SELECT COUNT(*) FROM lecture_sessions").fetchone()[0] or 1
            low=[]
            for sid,name,course,n in rows:
                pct=n/sessions*100
                if pct<75: low.append(f"{sid}  {name}  {pct:.1f}%")
            messagebox.showinfo("Low Attendance", "No students below 75%." if not low else "Students below 75%:\n\n"+"\n".join(low[:40]),parent=self.root)
        except Exception as e: messagebox.showerror("Alert Error",str(e),parent=self.root)

    def show_teacher_dashboard(self):
        if not self.logged_role: self.authenticate_role()
        if self.logged_role not in ("Admin","Teacher"): messagebox.showwarning("Restricted","Teacher or Admin login required.",parent=self.root); return
        self.show_analytics()

    def manage_holidays(self):
        if not self._admin_required(): return
        d=tk.simpledialog.askstring("Holiday", "Enter holiday date YYYY-MM-DD:", parent=self.root)
        if not d: return
        reason=tk.simpledialog.askstring("Holiday", "Reason:", parent=self.root) or "Holiday"
        with db_connect() as conn:
            conn.execute("INSERT OR REPLACE INTO holidays(Holiday_Date,Reason) VALUES(?,?)",(d,reason)); conn.commit()
        audit_event(self.logged_username,self.logged_role,"HOLIDAY_SET",f"{d}: {reason}")
        messagebox.showinfo("Holiday",f"{d} marked as holiday.",parent=self.root)

    def backup_now(self):
        if not self._admin_required(): return
        os.makedirs("backups",exist_ok=True); stamp=datetime.now().strftime("%Y%m%d_%H%M%S"); target=os.path.join("backups",f"smart_attendance_{stamp}.db"); shutil.copy2(SQLITE_FILE,target)
        audit_event(self.logged_username,self.logged_role,"BACKUP",target); messagebox.showinfo("Backup Complete",target,parent=self.root)

    def show_audit_log(self):
        if not self._admin_required(): return
        with db_connect() as conn: rows=conn.execute("SELECT Event_Time,Username,Role,Action,Details FROM audit_log ORDER BY ID DESC LIMIT 300").fetchall()
        win=tk.Toplevel(self.root); win.title("Activity / Audit Log"); win.geometry("1000x600"); win.configure(bg=self.bg)
        tree=ttk.Treeview(win,columns=("time","user","role","action","details"),show="headings")
        for c,h in zip(tree["columns"],("Time","Username","Role","Action","Details")): tree.heading(c,text=h); tree.column(c,width=180)
        for r in rows: tree.insert("","end",values=r)
        tree.pack(fill="both",expand=True,padx=15,pady=15); tk.Button(win,text="✕ CLOSE WINDOW",command=win.destroy,bg=self.red,fg=self.white,relief="flat",padx=20,pady=8).pack(pady=(0,12))

    def show_system_health(self):
        checks=[("SQLite",os.path.exists(SQLITE_FILE)),("Student CSV",os.path.exists(STUDENT_FILE)),("Face Model",os.path.exists(MODEL_FILE)),("Cascade",os.path.exists(CASCADE_FILE)),("8 Lectures",len(self.lectures)==8)]
        msg="\n".join(("🟢" if ok else "🔴")+" "+name for name,ok in checks); messagebox.showinfo("System Health",msg,parent=self.root)

    def generate_pdf_report(self):
        if not self._admin_required(): return
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.platypus import SimpleDocTemplate,Paragraph,Table,TableStyle
            from reportlab.lib import colors
        except ImportError:
            messagebox.showerror("PDF", "Install ReportLab: pip install reportlab",parent=self.root); return
        path=filedialog.asksaveasfilename(parent=self.root,defaultextension=".pdf",filetypes=[("PDF","*.pdf")],initialfile="attendance_report.pdf")
        if not path:return
        df=get_attendance_df(); data=[list(df.columns)]+df.astype(str).values.tolist() if not df.empty else [["No attendance records"]]
        doc=SimpleDocTemplate(path,pagesize=A4); table=Table(data,repeatRows=1); table.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.4,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#172554")),("TEXTCOLOR",(0,0),(-1,0),colors.white)])); doc.build([Paragraph("SMART ATTENDANCE SYSTEM - ATTENDANCE REPORT",__import__('reportlab.lib.styles',fromlist=['getSampleStyleSheet']).getSampleStyleSheet()["Title"]),table]); audit_event(self.logged_username,self.logged_role,"PDF_EXPORT",path); messagebox.showinfo("PDF Created",path,parent=self.root)

    def generate_student_qr(self):
        if not self._admin_required(): return
        try: import qrcode
        except ImportError: messagebox.showerror("QR", "Install qrcode: pip install qrcode[pil]",parent=self.root); return
        os.makedirs("student_qr",exist_ok=True); df=pd.read_csv(STUDENT_FILE,dtype=str).fillna("")
        for _,r in df.iterrows(): qrcode.make(str(r["Student_ID"])).save(os.path.join("student_qr",f"{r['Student_ID']}.png"))
        messagebox.showinfo("QR Codes","Student QR codes generated in student_qr folder.",parent=self.root)

    def qr_backup_attendance(self):
        if self.logged_role not in ("Admin","Teacher"):
            self.authenticate_role();
        if self.logged_role not in ("Admin","Teacher"): return
        messagebox.showinfo("QR Backup","QR backup module is ready. Use generated student QR codes with a QR scanner integration.",parent=self.root)

    def tamper_protection_info(self):
        messagebox.showinfo("Tamper Protection","Attendance changes are restricted by role. Admin actions and logins are recorded in SQLite audit_log.",parent=self.root)


    def delete_registered_student(self):
        """Admin-only registered student deletion."""
        if not (
            getattr(self, "logged_role", None) == "Admin"
            and getattr(self, "admin_authenticated", False)
        ):
            if not self.authenticate_admin():
                return

        win = tk.Toplevel(self.root)
        win.title("Delete Registered Student")
        win.geometry("950x650")
        win.minsize(800, 550)
        win.configure(bg=self.bg)
        win.transient(self.root)

        tk.Label(
            win, text="🗑  DELETE REGISTERED STUDENT",
            font=("Segoe UI", 20, "bold"),
            fg=self.white, bg="#06101D", pady=14
        ).pack(fill="x")

        top = tk.Frame(win, bg=self.panel)
        top.pack(fill="x", padx=16, pady=12)

        search_var = tk.StringVar()
        tk.Label(
            top, text="Search ID / Name / Roll No:",
            font=("Segoe UI", 10, "bold"),
            fg=self.white, bg=self.panel
        ).pack(side="left", padx=10, pady=10)

        search_entry = tk.Entry(
            top, textvariable=search_var, width=35,
            font=("Segoe UI", 11), bg="white", fg="black"
        )
        search_entry.pack(side="left", padx=5, pady=10, ipady=5)

        table_frame = tk.Frame(win, bg=self.bg)
        table_frame.pack(fill="both", expand=True, padx=16, pady=5)

        cols = ("id", "name", "roll", "course", "semester", "section")
        tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse")
        for c, h, w in [
            ("id","STUDENT ID",140), ("name","NAME",220), ("roll","ROLL NO",120),
            ("course","COURSE",120), ("semester","SEMESTER",100), ("section","SECTION",100)
        ]:
            tree.heading(c, text=h)
            tree.column(c, width=w, anchor="center")
        tree.column("name", anchor="w")

        sb = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        status = tk.StringVar(value="")
        tk.Label(win, textvariable=status, fg=self.muted, bg=self.bg,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=18, pady=5)

        def load():
            for item in tree.get_children():
                tree.delete(item)
            try:
                with db_connect() as conn:
                    tables = [r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()]
                    table = next((t for t in ("students","registered_students","student_records","student_data","employees") if t in tables), None)
                    if not table:
                        status.set("Student registration table not found.")
                        return
                    cols_info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                    dbcols = [r[1] for r in cols_info]
                    def pick(names):
                        for n in names:
                            for c in dbcols:
                                if c.lower() == n.lower():
                                    return c
                        return None
                    idc = pick(["Student_ID","StudentID","ID","Employee_ID"]) or dbcols[0]
                    namec = pick(["Name","Student_Name","Full_Name"]) or (dbcols[1] if len(dbcols)>1 else dbcols[0])
                    rollc = pick(["Roll_No","RollNo","Roll_Number"])
                    coursec = pick(["Course","Department"])
                    semc = pick(["Semester","Sem"])
                    secc = pick(["Section"])
                    rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY "{namec}"').fetchall()
                    idx = {c:i for i,c in enumerate(dbcols)}
                    key = search_var.get().strip().lower()
                    count = 0
                    for row in rows:
                        vals = [
                            str(row[idx[idc]]) if idc in idx else "",
                            str(row[idx[namec]]) if namec in idx else "",
                            str(row[idx[rollc]]) if rollc and rollc in idx else "",
                            str(row[idx[coursec]]) if coursec and coursec in idx else "",
                            str(row[idx[semc]]) if semc and semc in idx else "",
                            str(row[idx[secc]]) if secc and secc in idx else ""
                        ]
                        if key and key not in " ".join(vals).lower():
                            continue
                        tree.insert("", "end", values=vals)
                        count += 1
                    status.set(f"{count} student(s) found.")
            except Exception as exc:
                messagebox.showerror("Student List Error", str(exc), parent=win)

        def delete_selected():
            sel = tree.selection()
            if not sel:
                messagebox.showwarning("Delete Student", "Select a student first.", parent=win)
                return
            vals = tree.item(sel[0], "values")
            sid, name = str(vals[0]).strip(), str(vals[1]).strip()
            if not messagebox.askyesno(
                "Confirm Delete",
                f"Permanently delete this student?\n\nStudent ID: {sid}\nName: {name}",
                parent=win
            ):
                return
            try:
                with db_connect() as conn:
                    tables = [r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()]
                    table = next((t for t in ("students","registered_students","student_records","student_data","employees") if t in tables), None)
                    if not table:
                        raise RuntimeError("Student registration table not found.")
                    dbcols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
                    idc = next((c for c in dbcols if c.lower() in {"student_id","studentid","id","employee_id"}), None)
                    if not idc:
                        raise RuntimeError("Student ID column not found.")
                    conn.execute(f'DELETE FROM "{table}" WHERE "{idc}"=?', (sid,))
                    for at in ("attendance","attendance_records","attendance_logs"):
                        exists = conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(at,)
                        ).fetchone()
                        if exists:
                            acols = [r[1] for r in conn.execute(f'PRAGMA table_info("{at}")').fetchall()]
                            aid = next((c for c in acols if c.lower() in {"student_id","studentid","employee_id","employeeid"}), None)
                            if aid:
                                try:
                                    conn.execute(f'DELETE FROM "{at}" WHERE "{aid}"=?', (sid,))
                                except Exception:
                                    pass
                    conn.commit()
                try:
                    audit_event(self.logged_username, self.logged_role, "DELETE_STUDENT", f"{sid} - {name}")
                except Exception:
                    pass
                load()
                messagebox.showinfo("Deleted", f"Student deleted successfully.\n\n{sid} - {name}", parent=win)
            except Exception as exc:
                messagebox.showerror("Delete Student Error", str(exc), parent=win)

        search_entry.bind("<KeyRelease>", lambda e: load())

        buttons = tk.Frame(win, bg=self.bg)
        buttons.pack(fill="x", padx=16, pady=12)
        tk.Button(
            buttons, text="🗑 DELETE SELECTED STUDENT",
            command=delete_selected, font=("Segoe UI", 10, "bold"),
            fg=self.white, bg=self.red, activebackground="#D93644",
            relief="flat", bd=0, padx=18, pady=10, cursor="hand2"
        ).pack(side="left", padx=4)
        tk.Button(
            buttons, text="🔄 REFRESH", command=load,
            font=("Segoe UI", 10, "bold"), fg=self.white, bg=self.blue,
            activebackground="#0E63D8", relief="flat", bd=0,
            padx=18, pady=10, cursor="hand2"
        ).pack(side="left", padx=4)
        tk.Button(
            buttons, text="✕ CLOSE", command=win.destroy,
            font=("Segoe UI", 10, "bold"), fg=self.white, bg=self.red,
            activebackground="#D93644", relief="flat", bd=0,
            padx=20, pady=10, cursor="hand2"
        ).pack(side="right", padx=4)
        load()
        search_entry.focus_set()

    def show_holiday_calendar(self):
        """Holiday Calendar with Add button directly below Date and Name fields."""
        try:
            with db_connect() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS college_holidays (
                        holiday_date TEXT PRIMARY KEY,
                        holiday_name TEXT NOT NULL
                    )
                """)
                conn.commit()
        except Exception as exc:
            messagebox.showerror("Holiday Calendar", f"Database error:\n{exc}", parent=self.root)
            return

        win = tk.Toplevel(self.root)
        win.title("College Holiday Calendar")
        win.geometry("1250x800")
        win.minsize(1000, 650)
        win.configure(bg=self.bg)
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", win.destroy)

        tk.Label(
            win, text="🏖  COLLEGE HOLIDAY CALENDAR",
            font=("Segoe UI", 20, "bold"), fg=self.white,
            bg="#06101D", pady=14
        ).pack(fill="x")

        input_box = tk.Frame(
            win, bg=self.panel, highlightthickness=1,
            highlightbackground="#27415F"
        )
        input_box.pack(fill="x", padx=16, pady=(14, 8))

        tk.Label(
            input_box, text="HOLIDAY DATE (DD-MM-YYYY)",
            font=("Segoe UI", 10, "bold"),
            fg=self.white, bg=self.panel
        ).grid(row=0, column=0, padx=(16, 8), pady=(14, 6), sticky="w")

        date_entry = tk.Entry(
            input_box, width=20, font=("Segoe UI", 11),
            bg="white", fg="black", insertbackground="black"
        )
        date_entry.grid(row=0, column=1, padx=6, pady=(14, 6), ipady=5)

        tk.Label(
            input_box, text="HOLIDAY NAME",
            font=("Segoe UI", 10, "bold"),
            fg=self.white, bg=self.panel
        ).grid(row=0, column=2, padx=(22, 8), pady=(14, 6), sticky="w")

        name_entry = tk.Entry(
            input_box, width=35, font=("Segoe UI", 11),
            bg="white", fg="black", insertbackground="black"
        )
        name_entry.grid(row=0, column=3, padx=6, pady=(14, 6), ipady=5)

        year_var = tk.IntVar(value=datetime.now().year)
        status_var = tk.StringVar(value="")

        def refresh():
            for item in tree.get_children():
                tree.delete(item)
            try:
                selected_year = int(year_var.get())
            except Exception:
                selected_year = datetime.now().year
                year_var.set(selected_year)

            rows = {}
            if selected_year == 2026:
                for d, n in AUTOMATIC_GAZETTED_HOLIDAYS_2026.items():
                    rows[d] = (d, n, "Official / Gazetted")
                for d, n in AUTOMATIC_REFERENCE_HOLIDAYS_2026.items():
                    rows.setdefault(d, (d, n, "Reference / Optional"))

            import calendar
            for month in range(1, 13):
                for day in range(1, calendar.monthrange(selected_year, month)[1] + 1):
                    dt = datetime(selected_year, month, day)
                    if dt.weekday() == 6:
                        d = dt.strftime("%Y-%m-%d")
                        rows.setdefault(d, (d, "Sunday", "Sunday"))

            try:
                with db_connect() as conn:
                    custom = conn.execute(
                        "SELECT holiday_date, holiday_name FROM college_holidays "
                        "ORDER BY holiday_date"
                    ).fetchall()
                for d, n in custom:
                    d = str(d)
                    if d.startswith(f"{selected_year}-"):
                        rows[d] = (d, str(n), "College Custom")
            except Exception:
                pass

            for d in sorted(rows):
                dt = datetime.strptime(d, "%Y-%m-%d")
                _, holiday, typ = rows[d]
                tree.insert(
                    "", "end",
                    values=(dt.strftime("%d-%m-%Y"), dt.strftime("%A"), holiday, typ)
                )
            status_var.set(f"{len(rows)} entries • {selected_year}")

        def add_custom():
            if not (
                getattr(self, "logged_role", None) == "Admin"
                and getattr(self, "admin_authenticated", False)
            ):
                if not self.authenticate_admin():
                    return

            raw_date = date_entry.get().strip()
            holiday_name = name_entry.get().strip()

            if not raw_date:
                messagebox.showwarning("Holiday Date", "Please enter the holiday date.", parent=win)
                return
            if not holiday_name:
                messagebox.showwarning("Holiday Name", "Please enter the holiday name.", parent=win)
                return

            parsed_date = None
            for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
                try:
                    parsed_date = datetime.strptime(raw_date, fmt)
                    break
                except ValueError:
                    pass

            if parsed_date is None:
                messagebox.showerror(
                    "Invalid Date",
                    "Use DD-MM-YYYY.\nExample: 15-08-2026",
                    parent=win
                )
                return

            holiday_date = parsed_date.strftime("%Y-%m-%d")
            try:
                with db_connect() as conn:
                    conn.execute("""
                        INSERT INTO college_holidays (holiday_date, holiday_name)
                        VALUES (?, ?)
                        ON CONFLICT(holiday_date)
                        DO UPDATE SET holiday_name = excluded.holiday_name
                    """, (holiday_date, holiday_name))
                    conn.commit()

                try:
                    audit_event(
                        self.logged_username, self.logged_role,
                        "ADD_HOLIDAY", f"{holiday_date} - {holiday_name}"
                    )
                except Exception:
                    pass

                year_var.set(parsed_date.year)
                date_entry.delete(0, "end")
                name_entry.delete(0, "end")
                refresh()

                messagebox.showinfo(
                    "Holiday Added",
                    f"Holiday added successfully!\n\n"
                    f"Date: {parsed_date.strftime('%d-%m-%Y')}\n"
                    f"Holiday: {holiday_name}",
                    parent=win
                )
            except Exception as exc:
                messagebox.showerror("Holiday Save Error", str(exc), parent=win)

        # ADD BUTTON: directly below both input boxes.
        tk.Button(
            input_box,
            text="➕  ADD HOLIDAY",
            command=add_custom,
            font=("Segoe UI", 11, "bold"),
            fg=self.white, bg=self.green,
            activebackground="#0FA968",
            activeforeground=self.white,
            relief="flat", bd=0,
            padx=32, pady=10, cursor="hand2"
        ).grid(row=1, column=0, columnspan=4, pady=(4, 14))

        control = tk.Frame(win, bg=self.bg)
        control.pack(fill="x", padx=16, pady=(0, 6))

        tk.Label(
            control, text="YEAR:", font=("Segoe UI", 10, "bold"),
            fg=self.white, bg=self.bg
        ).pack(side="left", padx=(4, 6))

        tk.Spinbox(
            control, from_=2024, to=2035,
            textvariable=year_var, width=7,
            font=("Segoe UI", 11, "bold"), justify="center",
            command=refresh
        ).pack(side="left")

        tk.Label(
            control, textvariable=status_var,
            font=("Segoe UI", 9, "bold"),
            fg=self.muted, bg=self.bg
        ).pack(side="left", padx=18)

        table_frame = tk.Frame(win, bg=self.bg)
        table_frame.pack(fill="both", expand=True, padx=16, pady=6)

        columns = ("date", "day", "holiday", "type")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        for col, heading, width in [
            ("date", "DATE", 150), ("day", "DAY", 150),
            ("holiday", "HOLIDAY", 500), ("type", "TYPE", 200)
        ]:
            tree.heading(col, text=heading)
            tree.column(col, width=width, anchor="center")
        tree.column("holiday", anchor="w")

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def delete_selected():
            selected = tree.selection()
            if not selected:
                messagebox.showinfo("Delete Holiday", "Select a College Custom holiday first.", parent=win)
                return
            vals = tree.item(selected[0], "values")
            if len(vals) < 4 or vals[3] != "College Custom":
                messagebox.showwarning(
                    "Protected Holiday",
                    "Only College Custom holidays can be deleted.",
                    parent=win
                )
                return
            try:
                d = datetime.strptime(vals[0], "%d-%m-%Y").strftime("%Y-%m-%d")
                if not messagebox.askyesno("Delete Holiday", f"Delete {vals[2]} on {vals[0]}?", parent=win):
                    return
                with db_connect() as conn:
                    conn.execute("DELETE FROM college_holidays WHERE holiday_date=?", (d,))
                    conn.commit()
                refresh()
            except Exception as exc:
                messagebox.showerror("Delete Holiday Error", str(exc), parent=win)

        bottom = tk.Frame(win, bg=self.bg)
        bottom.pack(fill="x", padx=16, pady=(4, 14))

        tk.Button(
            bottom, text="🗑 DELETE CUSTOM HOLIDAY",
            command=delete_selected, font=("Segoe UI", 10, "bold"),
            fg=self.white, bg=self.red, activebackground="#D93644",
            relief="flat", bd=0, padx=18, pady=9, cursor="hand2"
        ).pack(side="left", padx=4)

        tk.Button(
            bottom, text="🔄 REFRESH", command=refresh,
            font=("Segoe UI", 10, "bold"), fg=self.white, bg=self.blue,
            activebackground="#0E63D8", relief="flat", bd=0,
            padx=18, pady=9, cursor="hand2"
        ).pack(side="left", padx=4)

        tk.Button(
            bottom, text="✕ CLOSE", command=win.destroy,
            font=("Segoe UI", 10, "bold"), fg=self.white, bg=self.red,
            activebackground="#D93644", relief="flat", bd=0,
            padx=20, pady=9, cursor="hand2"
        ).pack(side="right", padx=4)

        refresh()
        date_entry.focus_set()


    # ============================================================
    # NEXT-GEN ADVANCED FEATURES CENTER
    # ============================================================

    def show_advanced_features_center(self):
        if not self.logged_role:
            self.authenticate_role()
        if not self.logged_role:
            return

        win = tk.Toplevel(self.root)
        win.title("Smart Attendance — Advanced Features Center")
        win.geometry("1250x760")
        win.minsize(1050, 650)
        win.configure(bg=self.bg)

        tk.Label(
            win, text="🚀  ADVANCED FEATURES CENTER",
            font=("Segoe UI", 20, "bold"),
            fg=self.white, bg="#08111F", pady=16
        ).pack(fill="x")

        tk.Label(
            win,
            text="Performance • Alerts • Profiles • Search • Export • Backup • Settings",
            font=("Segoe UI", 10, "bold"),
            fg="#AFC2D8", bg="#08111F"
        ).pack(fill="x", pady=(0, 12))

        bar = tk.Frame(win, bg=self.bg)
        bar.pack(fill="x", padx=18, pady=8)

        def feature_button(text, cmd, color, parent=None):
            host = parent if parent is not None else bar
            return tk.Button(
                host, text=text, command=cmd, font=("Segoe UI", 9, "bold"),
                fg=self.white, bg=color, activebackground=color,
                activeforeground=self.white, relief="flat", bd=0,
                padx=12, pady=9, cursor="hand2"
            )

        tk.Button(
            bar, text="✕ CLOSE", command=win.destroy,
            font=("Segoe UI", 9, "bold"), fg=self.white, bg=self.red,
            activebackground="#B91C1C", relief="flat", bd=0,
            padx=18, pady=9, cursor="hand2"
        ).pack(side="right", padx=5)

        # Dedicated styles for the Advanced Features Center.
        # This prevents Windows/VS Code theme defaults from making the
        # Performance table white or the selected tab unreadable.
        adv_style = ttk.Style(win)
        try:
            adv_style.theme_use("clam")
        except Exception:
            pass
        adv_style.configure(
            "Advanced.Treeview",
            background="#0B4FB3",
            fieldbackground="#0B4FB3",
            foreground="#FFFFFF",
            rowheight=32,
            borderwidth=0,
            font=("Segoe UI", 10, "bold")
        )
        adv_style.configure(
            "Advanced.Treeview.Heading",
            background="#061A4A",
            foreground="#FFFFFF",
            font=("Segoe UI", 10, "bold"),
            padding=7
        )
        adv_style.map(
            "Advanced.Treeview",
            background=[("selected", "#FFFFFF")],
            foreground=[("selected", "#000000")]
        )
        adv_style.configure(
            "Advanced.TNotebook",
            background=self.bg,
            borderwidth=0,
            tabmargins=[2, 5, 2, 0]
        )
        adv_style.configure(
            "Advanced.TNotebook.Tab",
            background="#15304F",
            foreground="#FFFFFF",
            padding=[16, 9],
            font=("Segoe UI", 9, "bold")
        )
        adv_style.map(
            "Advanced.TNotebook.Tab",
            background=[("selected", "#1677FF")],
            foreground=[("selected", "#FFFFFF")]
        )

        # Left/center notebook
        nb = ttk.Notebook(win, style="Advanced.TNotebook")
        nb.pack(fill="both", expand=True, padx=18, pady=(5, 18))

        perf_tab = tk.Frame(nb, bg=self.bg)
        search_tab = tk.Frame(nb, bg=self.bg)
        tools_tab = tk.Frame(nb, bg=self.bg)
        settings_tab = tk.Frame(nb, bg=self.bg)
        nb.add(perf_tab, text="📊 Performance")
        nb.add(search_tab, text="🔍 Search / Export")
        nb.add(tools_tab, text="🛡 Tools")
        nb.add(settings_tab, text="⚙ Admin Settings")

        # ---------- Performance ----------
        top = tk.Frame(perf_tab, bg=self.bg)
        top.pack(fill="x", padx=12, pady=12)
        threshold_var = tk.StringVar(value="75")
        tk.Label(top, text="Low attendance threshold %:",
                 fg=self.text, bg=self.bg, font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Entry(top, textvariable=threshold_var, width=6).pack(side="left", padx=8)

        perf_frame = tk.Frame(perf_tab, bg=self.bg)
        perf_frame.pack(fill="both", expand=True, padx=12, pady=5)

        pcols = ("Student ID", "Name", "Roll No", "Present %", "Late %", "Absent %", "Temp OUT", "Lectures")
        ptree = ttk.Treeview(perf_frame, columns=pcols, show="headings", style="Advanced.Treeview")
        for c in pcols:
            ptree.heading(c, text=c)
        widths = [105, 180, 100, 90, 80, 90, 90, 80]
        for c,w in zip(pcols,widths):
            ptree.column(c, width=w, anchor="center" if "%" in c or c in ("Temp OUT","Lectures") else "w")
        psy = ttk.Scrollbar(perf_frame, orient="vertical", command=ptree.yview)
        ptree.configure(yscrollcommand=psy.set)
        ptree.pack(side="left", fill="both", expand=True)
        psy.pack(side="right", fill="y")

        def performance_rows():
            students = pd.read_csv(STUDENT_FILE, dtype=str).fillna("")
            att = get_attendance_df()
            if att.empty:
                return []
            if "Date" in att.columns and "Lecture_No" in att.columns:
                sessions = max(1, len(att[["Date","Lecture_No"]].drop_duplicates()))
            else:
                sessions = max(1, len(att))
            result=[]
            for _,s in students.iterrows():
                sid=str(s.get("Student_ID",""))
                name=str(s.get("Name",""))
                roll=str(s.get("Roll_No",s.get("Roll No","")))
                rows=att[att["Student_ID"].astype(str)==sid]
                total_in=len(rows[rows["In_Time"].astype(str).str.strip()!=""]) if "In_Time" in rows.columns else 0
                present=len(rows[rows["Status"].astype(str)=="Present"]) if "Status" in rows.columns else 0
                late=len(rows[rows["Status"].astype(str)=="Late"]) if "Status" in rows.columns else 0
                temp_count=0
                try:
                    mov=get_movement_df()
                    m=mov[mov["Student_ID"].astype(str)==sid]
                    temp_count=len(m[m["Movement_Type"].astype(str)=="OUT"])
                except Exception:
                    pass
                absent=max(0,sessions-present-late)
                result.append((sid,name,roll,
                               f"{present/sessions*100:.1f}%",
                               f"{late/sessions*100:.1f}%",
                               f"{absent/sessions*100:.1f}%",
                               str(temp_count), str(sessions)))
            return result

        def refresh_performance():
            for i in ptree.get_children(): ptree.delete(i)
            try:
                rows=performance_rows()
                limit=float(threshold_var.get() or 75)
                for r in rows:
                    if float(r[3].rstrip("%")) < limit:
                        ptree.insert("", "end", values=r, tags=("low",))
                    else:
                        ptree.insert("", "end", values=r)
                ptree.tag_configure("low", background="#FEE2E2")
            except Exception as e:
                messagebox.showerror("Performance Error", str(e), parent=win)

        feature_button("🔄 REFRESH", refresh_performance, self.blue).pack(side="left", padx=5)
        feature_button("🔔 LOW ATTENDANCE ALERT", lambda: self.show_low_attendance_alerts(), self.orange).pack(side="left", padx=5)
        feature_button("📈 GRAPH", lambda: self.show_attendance_graph(), self.purple).pack(side="left", padx=5)
        feature_button("🧑‍🎓 STUDENT PROFILE", lambda: self.show_student_profile(), self.green).pack(side="left", padx=5)
        refresh_performance()

        # ---------- Search / Export ----------
        search_top=tk.Frame(search_tab,bg=self.bg); search_top.pack(fill="x",padx=12,pady=12)
        q=tk.StringVar()
        status_q=tk.StringVar(value="All")
        tk.Label(search_top,text="Search:",fg=self.text,bg=self.bg,font=("Segoe UI",10,"bold")).pack(side="left")
        qe=tk.Entry(search_top,textvariable=q,width=28); qe.pack(side="left",padx=7)
        tk.Label(search_top,text="Status:",fg=self.text,bg=self.bg,font=("Segoe UI",10,"bold")).pack(side="left",padx=(15,5))
        ttk.Combobox(search_top,textvariable=status_q,values=["All","Present","Late","Absent","TEMP OUT","Final OUT"],state="readonly",width=12).pack(side="left")
        sframe=tk.Frame(search_tab,bg=self.bg); sframe.pack(fill="both",expand=True,padx=12,pady=5)
        scols=("Date","Lecture","Student ID","Name","Status","IN","OUT")
        stree=ttk.Treeview(sframe,columns=scols,show="headings",style="Advanced.Treeview")
        for c in scols: stree.heading(c,text=c)
        for c,w in zip(scols,[105,70,110,190,100,100,100]): stree.column(c,width=w)
        sy=ttk.Scrollbar(sframe,orient="vertical",command=stree.yview); stree.configure(yscrollcommand=sy.set)
        stree.pack(side="left",fill="both",expand=True); sy.pack(side="right",fill="y")

        def load_search():
            for i in stree.get_children(): stree.delete(i)
            students=pd.read_csv(STUDENT_FILE,dtype=str).fillna("")
            names=dict(zip(students["Student_ID"].astype(str),students["Name"].astype(str))) if "Student_ID" in students.columns else {}
            att=get_attendance_df()
            term=q.get().strip().lower(); wanted=status_q.get()
            for _,r in att.iterrows():
                sid=str(r.get("Student_ID","")); name=names.get(sid,"")
                status=str(r.get("Status","Absent"))
                if term and term not in " ".join([str(r.get("Date","")),str(r.get("Lecture_No","")),sid,name,status]).lower():
                    continue
                if wanted=="Final OUT" and not str(r.get("Out_Time","")).strip(): continue
                if wanted not in ("All","Final OUT") and status!=wanted: continue
                stree.insert("", "end", values=(r.get("Date",""),r.get("Lecture_No",""),sid,name,status,r.get("In_Time",""),r.get("Out_Time","")))

        feature_button("🔎 SEARCH", load_search, self.blue, search_top).pack(side="left", padx=8)
        feature_button("📤 CSV EXPORT", lambda:self.export_advanced_search(stree, "csv"), self.green, search_top).pack(side="left", padx=5)
        feature_button("📊 EXCEL EXPORT", lambda:self.export_advanced_search(stree, "xlsx"), self.orange, search_top).pack(side="left", padx=5)
        feature_button("📄 PDF EXPORT", lambda:self.export_advanced_search(stree, "pdf"), self.purple, search_top).pack(side="left", padx=5)
        load_search()

        # ---------- Tools ----------
        tools_inner=tk.Frame(tools_tab,bg=self.bg); tools_inner.pack(fill="both",expand=True,padx=25,pady=25)
        tool_defs=[
            ("💾 BACKUP NOW",self.backup_now,self.green),
            ("♻ RESTORE BACKUP",self.restore_backup,self.orange),
            ("📜 AUDIT LOG",self.show_audit_log,self.blue),
            ("📱 NOTIFICATION CENTER",self.show_notification_center,self.purple),
            ("🛡 FACE QUALITY CHECK",self.face_quality_check,self.cyan),
            ("🚫 DUPLICATE PROTECTION",self.duplicate_protection_info,self.red),
            ("⏱ LIVE LECTURE STATUS",self.show_live_lecture_status,self.blue),
            ("📅 ACADEMIC CALENDAR",self.show_academic_calendar,self.green),
            ("🧑‍🎓 STUDENT PROFILE",self.show_student_profile,self.purple),
        ]
        for i,(txt,cmd,col) in enumerate(tool_defs):
            feature_button(txt, cmd, col, tools_inner).grid(row=i//3, column=i%3, padx=10, pady=10, sticky="ew")
        for c in range(3): tools_inner.columnconfigure(c,weight=1)

        # ---------- Admin Settings ----------
        settings_outer = tk.Frame(settings_tab, bg=self.bg)
        settings_outer.pack(fill="both", expand=True, padx=18, pady=18)

        if self.logged_role not in ("Admin",):
            lock_box = tk.Frame(settings_outer, bg=self.panel)
            lock_box.pack(fill="x", padx=20, pady=20)
            tk.Label(
                lock_box, text="🔒  ADMIN LOGIN REQUIRED",
                font=("Segoe UI", 16, "bold"),
                fg=self.red, bg=self.panel
            ).pack(pady=(22, 8))
            tk.Label(
                lock_box, text="Login as Admin to edit system settings.",
                font=("Segoe UI", 10, "bold"),
                fg=self.muted, bg=self.panel
            ).pack(pady=(0, 14))
            tk.Button(
                lock_box, text="🔐 ADMIN LOGIN",
                command=lambda: (self.authenticate_admin(), win.destroy()),
                font=("Segoe UI", 10, "bold"),
                fg=self.white, bg=self.blue, activebackground=self.blue,
                activeforeground=self.white, relief="flat", bd=0,
                padx=24, pady=10, cursor="hand2"
            ).pack(pady=(0, 22))
        else:
            settings_box=tk.Frame(settings_outer,bg=self.panel)
            settings_box.pack(fill="x",padx=20,pady=20)
            settings=[
                ("Attendance grace minutes", "ATTENDANCE_GRACE_MINUTES", str(ATTENDANCE_GRACE_MINUTES)),
                ("Final OUT window minutes", "FINAL_OUT_WINDOW_MINUTES", str(FINAL_OUT_WINDOW_MINUTES)),
                ("Low attendance threshold", "LOW_ATTENDANCE_THRESHOLD", "75"),
                ("Backup retention count", "BACKUP_RETENTION", "30"),
            ]
            entries={}
            for i,(label,key,val) in enumerate(settings):
                tk.Label(settings_box,text=label,fg=self.white,bg=self.panel,font=("Segoe UI",10,"bold")).grid(row=i,column=0,sticky="w",padx=18,pady=10)
                e=tk.Entry(settings_box,width=12); e.insert(0,val); e.grid(row=i,column=1,padx=18,pady=10); entries[key]=e
            def save_settings():
                try:
                    vals={k:int(e.get()) for k,e in entries.items()}
                    with db_connect() as conn:
                        conn.execute("CREATE TABLE IF NOT EXISTS system_settings(Key TEXT PRIMARY KEY, Value TEXT)")
                        for k,v in vals.items():
                            conn.execute("INSERT OR REPLACE INTO system_settings(Key,Value) VALUES(?,?)",(k,str(v)))
                        conn.commit()
                    audit_event(self.logged_username,self.logged_role,"SETTINGS_UPDATE",str(vals))
                    messagebox.showinfo("Settings","Settings saved. Current session timing constants remain unchanged until restart.",parent=win)
                except Exception as e: messagebox.showerror("Settings Error",str(e),parent=win)
            feature_button("💾 SAVE SETTINGS", save_settings, self.green, settings_box).pack(pady=15)
            feature_button("🔒 LOCK ADMIN", self.admin_lock_toggle, self.red, settings_box).pack(pady=(0, 15))

    def show_attendance_graph(self):
        if not self.logged_role:
            self.authenticate_role()
        if not self.logged_role: return
        win=tk.Toplevel(self.root); win.title("Attendance Graphs"); win.geometry("1000x650"); win.configure(bg=self.bg)
        canvas=tk.Canvas(win,bg=self.white,highlightthickness=1,highlightbackground="#CBD5E1")
        canvas.pack(fill="both",expand=True,padx=18,pady=18)
        try:
            att=get_attendance_df()
            counts={"Present":0,"Late":0,"Absent":0}
            if not att.empty:
                for s in counts: counts[s]=int((att["Status"].astype(str)==s).sum())
            total=max(1,sum(counts.values()))
            w,h=900,520
            canvas.create_text(450,35,text="Attendance Status Overview",font=("Segoe UI",18,"bold"),fill="#172033")
            colors={"Present":"#16A34A","Late":"#F59E0B","Absent":"#DC2626"}
            x=90
            for label,val in counts.items():
                bar_h=int((val/total)*350)
                canvas.create_rectangle(x,450-bar_h,x+140,450,fill=colors[label],outline="")
                canvas.create_text(x+70,470,text=f"{label}\n{val} ({val/total*100:.1f}%)",font=("Segoe UI",10,"bold"),fill="#172033")
                x+=240
        except Exception as e:
            messagebox.showerror("Graph Error",str(e),parent=win)

    def show_student_profile(self):
        if not self.logged_role:
            self.authenticate_role()
        if not self.logged_role: return
        students=pd.read_csv(STUDENT_FILE,dtype=str).fillna("")
        win=tk.Toplevel(self.root); win.title("Student Profile"); win.geometry("900x650"); win.configure(bg=self.bg)
        top=tk.Frame(win,bg=self.bg); top.pack(fill="x",padx=18,pady=15)
        ids=students["Student_ID"].astype(str).tolist() if "Student_ID" in students.columns else []
        sid=tk.StringVar(value=ids[0] if ids else "")
        ttk.Combobox(top,textvariable=sid,values=ids,state="readonly",width=20).pack(side="left",padx=5)
        body=tk.Frame(win,bg=self.panel); body.pack(fill="both",expand=True,padx=18,pady=10)
        textw=tk.Text(body,font=("Consolas",11),bg="#071522",fg=self.white,wrap="word")
        textw.pack(fill="both",expand=True,padx=15,pady=15)
        def load():
            textw.delete("1.0","end")
            row=students[students["Student_ID"].astype(str)==sid.get()]
            if row.empty: return
            r=row.iloc[0]
            att=get_attendance_df(); a=att[att["Student_ID"].astype(str)==sid.get()] if not att.empty else pd.DataFrame()
            present=int((a["Status"].astype(str)=="Present").sum()) if not a.empty else 0
            late=int((a["Status"].astype(str)=="Late").sum()) if not a.empty else 0
            total=max(1,len(a)); pct=(present+late)/total*100
            lines=[f"STUDENT ID : {r.get('Student_ID','')}",f"NAME       : {r.get('Name','')}",f"ROLL NO    : {r.get('Roll_No',r.get('Roll No',''))}",
                   f"COURSE     : {r.get('Course','')}",f"SEMESTER   : {r.get('Semester','')}",f"SECTION    : {r.get('Section','')}",
                   "",f"Recorded IN : {present+late}",f"Present     : {present}",f"Late        : {late}",f"Attendance  : {pct:.1f}%", "",
                   "Recent attendance:"]
            if not a.empty:
                for _,rr in a.tail(20).iterrows():
                    lines.append(f"{rr.get('Date','')}  L{rr.get('Lecture_No','')}  {rr.get('Status','')}  IN {rr.get('In_Time','')} OUT {rr.get('Out_Time','')}")
            textw.insert("1.0","\n".join(lines))
        tk.Button(
            top, text="🔄 LOAD PROFILE", command=load,
            font=("Segoe UI", 9, "bold"), fg=self.white, bg=self.blue,
            activebackground=self.blue, relief="flat", bd=0,
            padx=12, pady=9, cursor="hand2"
        ).pack(side="left", padx=5)
        load()

    def export_advanced_search(self, tree, kind):
        if not self.logged_role:
            return
        rows=[tree.item(i,"values") for i in tree.get_children()]
        if not rows:
            messagebox.showinfo("Export","No records to export.",parent=self.root); return
        if kind=="csv":
            path=filedialog.asksaveasfilename(defaultextension=".csv",filetypes=[("CSV","*.csv")],parent=self.root)
            if path: pd.DataFrame(rows,columns=[tree.heading(c,"text") for c in tree["columns"]]).to_csv(path,index=False)
        elif kind=="xlsx":
            path=filedialog.asksaveasfilename(defaultextension=".xlsx",filetypes=[("Excel","*.xlsx")],parent=self.root)
            if path:
                pd.DataFrame(rows,columns=[tree.heading(c,"text") for c in tree["columns"]]).to_excel(path,index=False)
        else:
            path=filedialog.asksaveasfilename(defaultextension=".pdf",filetypes=[("PDF","*.pdf")],parent=self.root)
            if path:
                try:
                    from reportlab.lib.pagesizes import A4, landscape
                    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
                    from reportlab.lib import colors
                    doc=SimpleDocTemplate(path,pagesize=landscape(A4))
                    data=[ [tree.heading(c,"text") for c in tree["columns"]] ] + [list(r) for r in rows]
                    tab=Table(data,repeatRows=1)
                    tab.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.4,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#172554")),("TEXTCOLOR",(0,0),(-1,0),colors.white)]))
                    doc.build([tab])
                except Exception as e:
                    messagebox.showerror("PDF Export",str(e),parent=self.root); return
        audit_event(self.logged_username,self.logged_role,"ADVANCED_EXPORT",kind)

    def restore_backup(self):
        if not self._admin_required(): return
        path=filedialog.askopenfilename(initialdir="backups",filetypes=[("SQLite DB","*.db"),("All files","*.*")],parent=self.root)
        if not path: return
        if not messagebox.askyesno("Restore Backup","Current database will be replaced. Continue?",parent=self.root): return
        try:
            self.root.after_cancel(getattr(self,"_backup_refresh_job",None)) if getattr(self,"_backup_refresh_job",None) else None
            shutil.copy2(path,SQLITE_FILE)
            audit_event(self.logged_username,self.logged_role,"RESTORE_BACKUP",path)
            messagebox.showinfo("Restore Complete","Backup restored. Restart the application to reload all data.",parent=self.root)
        except Exception as e: messagebox.showerror("Restore Error",str(e),parent=self.root)

    def show_notification_center(self):
        if not self.logged_role:
            self.authenticate_role()
        if not self.logged_role:
            return

        win = tk.Toplevel(self.root)
        win.title("Parent / Student Notification Center")
        win.geometry("1180x760")
        win.minsize(980, 650)
        win.configure(bg=self.bg)
        win.transient(self.root)

        header = tk.Frame(win, bg="#06101D", height=90)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="📱  PARENT / STUDENT NOTIFICATION CENTER",
                 font=("Segoe UI", 18, "bold"), fg=self.white, bg="#06101D").pack(side="left", padx=22, pady=20)
        tk.Button(header, text="✕ CLOSE", command=win.destroy,
                  font=("Segoe UI", 9, "bold"), fg=self.white, bg=self.red,
                  activebackground="#B91C1C", relief="flat", bd=0,
                  padx=18, pady=8, cursor="hand2").pack(side="right", padx=18, pady=25)

        body = tk.Frame(win, bg=self.bg)
        body.pack(fill="both", expand=True, padx=18, pady=15)

        left = tk.Frame(body, bg=self.panel)
        left.pack(side="left", fill="y", padx=(0, 10))
        right = tk.Frame(body, bg=self.bg)
        right.pack(side="left", fill="both", expand=True)

        tk.Label(left, text="STUDENTS", font=("Segoe UI", 12, "bold"), fg=self.white, bg=self.panel).pack(padx=15, pady=(15, 8))
        students = pd.read_csv(STUDENT_FILE, dtype=str).fillna("") if os.path.exists(STUDENT_FILE) else pd.DataFrame(columns=STUDENT_COLUMNS)
        if "Parent_Mobile" not in students.columns: students["Parent_Mobile"] = ""
        if "Parent_Email" not in students.columns: students["Parent_Email"] = ""
        ids = students["Student_ID"].astype(str).tolist() if "Student_ID" in students.columns else []
        selected_id = tk.StringVar(value=ids[0] if ids else "")
        combo = ttk.Combobox(left, textvariable=selected_id, values=ids, state="readonly", width=24)
        combo.pack(padx=15, pady=6)
        info = tk.Text(left, width=34, height=14, bg="#071522", fg=self.white, relief="flat", wrap="word")
        info.pack(padx=15, pady=10)

        message_frame = tk.Frame(right, bg=self.panel)
        message_frame.pack(fill="x", pady=(0, 10))
        tk.Label(message_frame, text="Notification Message", font=("Segoe UI", 11, "bold"), fg=self.white, bg=self.panel).pack(anchor="w", padx=15, pady=(12, 5))
        msg_box = tk.Text(message_frame, height=10, bg="#071522", fg=self.white, insertbackground=self.white, relief="flat", wrap="word")
        msg_box.pack(fill="x", padx=15, pady=(0, 15))

        status_var = tk.StringVar(value="Ready")
        tk.Label(right, textvariable=status_var, font=("Segoe UI", 10, "bold"), fg=self.cyan, bg=self.bg).pack(anchor="w", pady=(0, 8))

        def current_student():
            row = students[students["Student_ID"].astype(str) == selected_id.get()]
            return row.iloc[0] if not row.empty else None

        def refresh_info(*_):
            info.delete("1.0", "end")
            st = current_student()
            if st is None:
                return
            pct = calculate_student_attendance(st.get("Student_ID", ""))
            lines = [
                f"Name: {st.get('Name','')}",
                f"Student ID: {st.get('Student_ID','')}",
                f"Roll No: {st.get('Roll_No','')}",
                f"Course: {st.get('Course','')}",
                f"Semester: {st.get('Semester','')}",
                f"Section: {st.get('Section','')}",
                f"Parent Mobile: {st.get('Parent_Mobile','')}",
                f"Parent Email: {st.get('Parent_Email','')}",
                f"Attendance: {pct:.1f}%",
            ]
            info.insert("1.0", "\n".join(lines))
            threshold = float(get_notification_setting("LOW_ATTENDANCE_THRESHOLD", "75") or 75)
            msg_box.delete("1.0", "end")
            msg_box.insert("1.0", _student_notification_message(st, pct, threshold))
            status_var.set("LOW ATTENDANCE" if pct < threshold else "Attendance is above threshold")

        combo.bind("<<ComboboxSelected>>", refresh_info)

        # Responsive action area: keep notification buttons visible instead of
        # letting a long horizontal row run outside the window.
        actions = tk.Frame(right, bg=self.bg)
        actions.pack(fill="x", pady=4)
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_columnconfigure(1, weight=1)
        actions.grid_columnconfigure(2, weight=1)

        def send_channel(channel):
            st = current_student()
            if st is None:
                messagebox.showwarning("Notification", "Select a student first.", parent=win)
                return
            sid = str(st.get("Student_ID", "")); name = str(st.get("Name", ""))
            mobile = str(st.get("Parent_Mobile", "")); email = str(st.get("Parent_Email", ""))
            message = msg_box.get("1.0", "end").strip()
            subject = f"Attendance Alert - {name}"
            if not message:
                messagebox.showwarning("Notification", "Message is empty.", parent=win)
                return
            if channel == "WhatsApp":
                ok, detail = send_whatsapp_notification(sid, name, mobile, message)
            elif channel == "SMS":
                ok, detail = send_sms_notification(sid, name, mobile, message)
            else:
                ok, detail = send_email_notification(sid, name, email, subject, message)
            status_var.set(f"{channel}: {detail}")
            if ok:
                messagebox.showinfo(channel, detail, parent=win)
            else:
                messagebox.showerror(channel, detail, parent=win)

        channel_buttons = []
        for col, (text, channel, color) in enumerate([
            ("🟢 WHATSAPP", "WhatsApp", self.green),
            ("📲 SMS", "SMS", self.blue),
            ("✉ EMAIL", "Email", self.purple),
        ]):
            btn = tk.Button(actions, text=text, command=lambda c=channel: send_channel(c),
                            font=("Segoe UI", 9, "bold"), fg=self.white, bg=color,
                            relief="flat", bd=0, padx=12, pady=9, cursor="hand2")
            btn.grid(row=0, column=col, sticky="ew", padx=5, pady=4)
            channel_buttons.append(btn)

        def send_all_low_attendance_notifications():
            """Send an automatic alert to every student below the threshold."""
            try:
                threshold = float(get_notification_setting("LOW_ATTENDANCE_THRESHOLD", "75") or 75)
            except Exception:
                threshold = 75.0

            sent_count = 0
            low_count = 0
            try:
                all_students = pd.read_csv(STUDENT_FILE, dtype=str).fillna("")
                for _, st in all_students.iterrows():
                    sid = str(st.get("Student_ID", "")).strip()
                    if not sid:
                        continue
                    pct = calculate_student_attendance(sid)
                    if pct >= threshold:
                        continue
                    low_count += 1
                    before = len(notification_history_df(10000))
                    automatic_low_attendance_notification(sid)
                    after_df = notification_history_df(10000)
                    if len(after_df) > before:
                        sent_count += 1

                audit_event(
                    self.logged_username, self.logged_role,
                    "AUTO_LOW_ATTENDANCE_BULK",
                    f"Below={low_count}, Attempts={sent_count}, Threshold={threshold}"
                )
                status_var.set(f"Processed {low_count} low-attendance student(s).")
                messagebox.showinfo(
                    "Automatic Parent Alerts",
                    f"Low-attendance students: {low_count}\n\nNotification attempts: {sent_count}\n\nCheck Notification History for delivery status.",
                    parent=win
                )
            except Exception as exc:
                messagebox.showerror("Notification Error", str(exc), parent=win)


        def generate_low_alerts():
            threshold = float(get_notification_setting("LOW_ATTENDANCE_THRESHOLD", "75") or 75)
            low = []
            for _, st in students.iterrows():
                pct = calculate_student_attendance(st.get("Student_ID", ""))
                if pct < threshold:
                    low.append((str(st.get("Student_ID", "")), str(st.get("Name", "")), pct))
            if not low:
                status_var.set("No low-attendance students found.")
                messagebox.showinfo("Low Attendance", "No students are below the configured threshold.", parent=win)
                return
            selected = low[0]
            selected_id.set(selected[0])
            refresh_info()
            status_var.set(f"Found {len(low)} low-attendance student(s).")
            audit_event(self.logged_username, self.logged_role, "LOW_ATTENDANCE_ALERT_GENERATED", f"Count={len(low)} Threshold={threshold}")

        tk.Button(actions, text="🔔 GENERATE LOW ATTENDANCE ALERTS", command=generate_low_alerts,
                  font=("Segoe UI", 9, "bold"), fg=self.white, bg=self.orange,
                  relief="flat", bd=0, padx=12, pady=9, cursor="hand2").grid(
                      row=1, column=0, columnspan=2, sticky="ew", padx=5, pady=4)
        tk.Button(actions, text="📲 SEND ALL LOW ATTENDANCE", command=send_all_low_attendance_notifications,
                  font=("Segoe UI", 9, "bold"), fg=self.white, bg="#0EA5E9",
                  relief="flat", bd=0, padx=12, pady=9, cursor="hand2").grid(
                      row=1, column=2, sticky="ew", padx=5, pady=4)

        def show_history():
            hwin = tk.Toplevel(win)
            hwin.title("Notification History")
            hwin.geometry("1100x620")
            hwin.configure(bg=self.bg)
            frame = tk.Frame(hwin, bg=self.bg)
            frame.pack(fill="both", expand=True, padx=15, pady=15)
            cols = ("Time", "Student ID", "Name", "Channel", "Recipient", "Status", "Details")
            tree = ttk.Treeview(frame, columns=cols, show="headings")
            widths = [145, 90, 150, 90, 150, 90, 320]
            for c, w in zip(cols, widths):
                tree.heading(c, text=c); tree.column(c, width=w, anchor="center" if c in ("Channel", "Status") else "w")
            sy = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            sx = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
            tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
            tree.pack(side="top", fill="both", expand=True); sy.pack(side="right", fill="y"); sx.pack(side="bottom", fill="x")
            df = notification_history_df()
            for _, r in df.iterrows():
                tree.insert("", "end", values=(r.get("Event_Time",""), r.get("Student_ID",""), r.get("Student_Name",""), r.get("Channel",""), r.get("Recipient",""), r.get("Status",""), r.get("Details","")))
            tk.Button(hwin, text="✕ CLOSE", command=hwin.destroy, bg=self.red, fg=self.white, relief="flat", padx=22, pady=8).pack(pady=10)

        def notification_settings():
            sw = tk.Toplevel(win)
            sw.title("Notification Settings")
            sw.geometry("760x720")
            sw.configure(bg=self.bg)
            sw.transient(win)
            outer = tk.Frame(sw, bg=self.bg); outer.pack(fill="both", expand=True, padx=18, pady=18)
            canvas = tk.Canvas(outer, bg=self.bg, highlightthickness=0)
            sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
            form = tk.Frame(canvas, bg=self.bg)
            canvas.create_window((0,0), window=form, anchor="nw")
            canvas.configure(yscrollcommand=sb.set)
            canvas.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
            entries = {}
            defs = [
                ("LOW_ATTENDANCE_THRESHOLD", "Low attendance threshold %", "75"),
                ("AUTO_LOW_ATTENDANCE", "Enable automatic low-attendance alerts (1/0)", "1"),
                ("AUTO_NOTIFICATION_CHANNEL", "Automatic channel: SMS, Email, WhatsApp (comma-separated)", "SMS"),
                ("AUTO_ALERT_ONCE_PER_DAY", "Send automatic alert only once per student per day (1/0)", "1"),
                ("WHATSAPP_ENABLED", "WhatsApp enabled (1/0)", "1"),
                ("SMS_ENABLED", "SMS enabled (1/0)", "0"),
                ("EMAIL_ENABLED", "Email enabled (1/0)", "0"),
                ("SMTP_HOST", "SMTP host", "smtp.gmail.com"),
                ("SMTP_PORT", "SMTP port", "587"),
                ("SMTP_USER", "SMTP username/email", ""),
                ("SMTP_PASSWORD", "SMTP password / app password", ""),
                ("SMTP_FROM", "Email sender", ""),
                ("TWILIO_ACCOUNT_SID", "Twilio Account SID", ""),
                ("TWILIO_AUTH_TOKEN", "Twilio Auth Token", ""),
                ("TWILIO_FROM", "Twilio From number", ""),
            ]
            for i, (key, label, default) in enumerate(defs):
                tk.Label(form, text=label, fg=self.white, bg=self.bg, font=("Segoe UI", 10, "bold")).grid(row=i, column=0, sticky="w", padx=10, pady=8)
                e = tk.Entry(form, width=46, bg="#071522", fg=self.white, insertbackground=self.white)
                e.insert(0, get_notification_setting(key, default))
                if "PASSWORD" in key or "TOKEN" in key:
                    e.configure(show="*")
                e.grid(row=i, column=1, sticky="ew", padx=10, pady=8, ipady=4)
                entries[key] = e
            form.columnconfigure(1, weight=1)
            def save():
                try:
                    for key, e in entries.items(): set_notification_setting(key, e.get().strip())
                    audit_event(self.logged_username, self.logged_role, "NOTIFICATION_SETTINGS_UPDATE", "Parent notification settings updated")
                    messagebox.showinfo("Notification Settings", "Settings saved successfully.", parent=sw)
                    sw.destroy()
                except Exception as exc:
                    messagebox.showerror("Settings Error", str(exc), parent=sw)
            tk.Button(form, text="💾 SAVE SETTINGS", command=save, bg=self.green, fg=self.white, relief="flat", padx=25, pady=10).grid(row=len(defs), column=0, pady=20)
            tk.Button(form, text="✕ CLOSE", command=sw.destroy, bg=self.red, fg=self.white, relief="flat", padx=25, pady=10).grid(row=len(defs), column=1, pady=20, sticky="e")
            form.update_idletasks(); canvas.configure(scrollregion=canvas.bbox("all"))

        tk.Button(actions, text="⚙ NOTIFICATION SETTINGS", command=notification_settings,
                  font=("Segoe UI", 9, "bold"), fg=self.white, bg="#475569",
                  relief="flat", bd=0, padx=12, pady=9, cursor="hand2").grid(
                      row=2, column=0, sticky="ew", padx=5, pady=4)
        tk.Button(actions, text="📜 NOTIFICATION HISTORY", command=show_history,
                  font=("Segoe UI", 9, "bold"), fg=self.white, bg="#334155",
                  relief="flat", bd=0, padx=12, pady=9, cursor="hand2").grid(
                      row=2, column=1, sticky="ew", padx=5, pady=4)

        if ids:
            refresh_info()

    def face_quality_check(self):
        if not self.logged_role:
            self.authenticate_role()
        if not self.logged_role: return
        messagebox.showinfo(
            "Face Recognition Quality Check",
            "Quality rules enabled:\n\n"
            "• Multiple faces → attendance is not marked.\n"
            "• Low-confidence recognition → recognition is rejected.\n"
            "• Camera/frame quality is checked before recognition.\n"
            "• CONFIDENCE_LIMIT = %s\n\n"
            "For best accuracy, use good lighting and keep one face in frame." % CONFIDENCE_LIMIT,
            parent=self.root
        )

    def duplicate_protection_info(self):
        if not self.logged_role:
            self.authenticate_role()
        if not self.logged_role: return
        try:
            att=get_attendance_df()
            dup=att[att.duplicated(subset=["Date","Lecture_No","Student_ID"],keep=False)] if not att.empty else pd.DataFrame()
            msg="No duplicate lecture attendance records found." if dup.empty else f"{len(dup)} duplicate rows detected. Existing attendance logic should reject repeat IN for the same lecture."
        except Exception as e: msg=str(e)
        messagebox.showinfo("Duplicate Attendance Protection",msg,parent=self.root)

    def show_live_lecture_status(self):
        win=tk.Toplevel(self.root); win.title("Live Lecture Status"); win.geometry("800x600"); win.configure(bg=self.bg)
        tree=ttk.Treeview(win,columns=("Lecture","Time","Status"),show="headings")
        for c in ("Lecture","Time","Status"): tree.heading(c,text=c)
        tree.column("Lecture",width=150); tree.column("Time",width=250); tree.column("Status",width=300)
        tree.pack(fill="both",expand=True,padx=20,pady=20)
        def refresh():
            for i in tree.get_children(): tree.delete(i)
            now=datetime.now().time()
            for l in self.lectures:
                st,_,_=self.timing_state(l)
                tree.insert("", "end", values=(f"Lecture {l['number']}",f"{l['start'].strftime('%I:%M %p')} - {l['end'].strftime('%I:%M %p')}",st))
            win.after(1500,refresh)
        refresh()

    def show_academic_calendar(self):
        win=tk.Toplevel(self.root); win.title("Academic Calendar"); win.geometry("1050x700"); win.configure(bg=self.bg)
        tree=ttk.Treeview(win,columns=("Date","Day","Event","Type"),show="headings")
        for c in ("Date","Day","Event","Type"): tree.heading(c,text=c)
        tree.column("Date",width=130); tree.column("Day",width=120); tree.column("Event",width=500); tree.column("Type",width=180)
        tree.pack(fill="both",expand=True,padx=18,pady=18)
        year=datetime.now().year
        rows=[]
        for month in range(1,13):
            import calendar
            for day in range(1,calendar.monthrange(year,month)[1]+1):
                d=datetime(year,month,day)
                ih,name,typ=automatic_holiday_status(d.date())
                if ih: rows.append((d.strftime("%Y-%m-%d"),d.strftime("%A"),name,typ))
        for r in rows: tree.insert("", "end", values=r)
        tk.Label(win,text="Use the existing timetable for lectures; this calendar shows holidays/events detected by the system.",fg=self.muted,bg=self.bg).pack(pady=(0,10))
        tk.Button(win,text="✕ CLOSE",command=win.destroy,bg=self.red,fg=self.white,relief="flat",padx=25,pady=9).pack(pady=(0,15))

    def close_main_window(self):
        """Ask for confirmation before closing the main dashboard."""
        if messagebox.askyesno(
            "Exit Smart Attendance System",
            "Do you want to close Smart Attendance System?",
            parent=self.root
        ):
            self.root.destroy()

    def authenticate_role(self, required=None):
        win=tk.Toplevel(self.root); win.title("Login"); win.geometry("430x360"); win.resizable(False,False); win.configure(bg="#08111F"); win.transient(self.root); win.grab_set()
        result={"ok":False}
        tk.Label(win,text="🔐  USER LOGIN",font=("Segoe UI",20,"bold"),fg=self.white,bg="#08111F").pack(pady=(26,8))
        form=tk.Frame(win,bg="#10243D"); form.pack(fill="x",padx=35,pady=8)
        tk.Label(form,text="Username",fg=self.white,bg="#10243D").pack(anchor="w",padx=18,pady=(18,5))
        user=tk.Entry(form,font=("Segoe UI",11)); user.pack(fill="x",padx=18,pady=(0,12),ipady=6)
        tk.Label(form,text="Password",fg=self.white,bg="#10243D").pack(anchor="w",padx=18,pady=5)
        pwd=tk.Entry(form,font=("Segoe UI",11),show="*"); pwd.pack(fill="x",padx=18,pady=(0,18),ipady=6)
        def login():
            username=user.get().strip(); ph=_hash_password(pwd.get())
            with db_connect() as conn:
                row=conn.execute("SELECT Username,Role FROM user_accounts WHERE Username=? AND Password_Hash=?",(username,ph)).fetchone()
            if not row:
                messagebox.showerror("Access Denied","Invalid username or password.",parent=win); pwd.delete(0,"end"); return
            role=row[1]
            if required and role != required:
                messagebox.showwarning("Access Restricted",f"{required} login required.",parent=win); return
            if required in (None,"Admin") or role=="Admin": self.admin_authenticated=(role=="Admin")
            self.logged_username=username; self.logged_role=role; result["ok"]=True
            audit_event(username,role,"LOGIN",f"Role={role}"); self.refresh_admin_indicator(); win.destroy()
        b=tk.Frame(win,bg="#08111F"); b.pack(pady=12)
        tk.Button(b,text="✓ LOGIN",command=login,font=("Segoe UI",10,"bold"),fg=self.white,bg=self.green,relief="flat",padx=30,pady=10).pack(side="left",padx=6)
        tk.Button(b,text="CANCEL",command=win.destroy,font=("Segoe UI",10,"bold"),fg=self.white,bg=self.red,relief="flat",padx=24,pady=10).pack(side="left",padx=6)
        user.focus_set(); win.bind("<Return>",lambda e:login()); win.bind("<Escape>",lambda e:win.destroy()); self.root.wait_window(win); return result["ok"]

    def authenticate_admin(self):
        if self.admin_authenticated and self.logged_role=="Admin": return True
        return self.authenticate_role("Admin")

    def admin_lock_toggle(self):
        if self.admin_authenticated:
            self.admin_authenticated=False; audit_event(self.logged_username,self.logged_role,"ADMIN_LOCK","Admin access locked"); self.refresh_admin_indicator(); return
        self.authenticate_admin()

    def refresh_admin_indicator(self):
        if hasattr(self,"live_status"):
            role=self.logged_role.upper() if self.logged_role else "NOT LOGGED IN"
            self.live_status.configure(text=f"● {role}  •  {'ADMIN UNLOCKED' if self.admin_authenticated else 'ADMIN LOCKED'}",
                                       fg=self.green if self.admin_authenticated else self.orange)
        if hasattr(self,"admin_lock_btn"):
            self.admin_lock_btn.configure(text="🔓  ADMIN LOGIN UNLOCKED" if self.admin_authenticated else "🔒  ADMIN LOGIN LOCKED",
                                          bg=self.green if self.admin_authenticated else self.red)

    def login_gui(self):
        self.authenticate_role()

    def admin_login_gui(self):
        self.authenticate_role("Admin")

    def admin_register_student_gui(self):
        if self.authenticate_admin(): self.register_student_gui()

    def admin_open_timetable(self):
        if self.authenticate_admin(): self.open_timetable()

    def admin_train_model_gui(self):
        if self.authenticate_admin(): self.train_model_gui()

    def setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # Attendance table: use dashboard colors instead of the default white area.
        style.configure(
            "Treeview",
            background="#0B4FB3",
            foreground="#FFFFFF",
            rowheight=34,
            fieldbackground="#0B4FB3",
            borderwidth=0,
            font=("Segoe UI", 10, "bold")
        )

        style.configure(
            "Treeview.Heading",
            background="#061A4A",
            foreground="#FFFFFF",
            font=("Segoe UI Semibold", 10),
            padding=8
        )

        style.map(
            "Treeview",
            background=[("selected", "#16C7FF")],
            foreground=[("selected", "#06101D")]
        )

    def label(self, parent, text, size=10, weight="normal",
              fg=None, bg=None):
        return tk.Label(
            parent,
            text=text,
            font=("Segoe UI", size, weight),
            fg=fg or self.text,
            bg=bg or self.bg
        )

    def build_ui(self):
        """ATM/kiosk-style main dashboard."""
        self.root.configure(bg="#08111F")
        self.bg = "#08111F"
        self.panel = "#10243D"
        self.panel2 = "#15304F"
        self.blue = "#1677FF"
        self.green = "#16C784"
        self.orange = "#FFB020"
        self.red = "#FF4D5A"
        self.purple = "#9B6CFF"
        self.cyan = "#00C2FF"
        self.white = "#FFFFFF"
        self.muted = "#AFC2D8"
        self.text = self.white

        top = tk.Frame(self.root, bg="#06101D", height=105)
        top.pack(fill="x"); top.pack_propagate(False)
        left=tk.Frame(top,bg="#06101D"); left.pack(side="left",fill="y",padx=(24,8))
        tk.Label(left,text="🎓",font=("Segoe UI Emoji",27),fg=self.cyan,bg="#06101D").pack(side="left",padx=(0,8))
        title_box=tk.Frame(left,bg="#06101D"); title_box.pack(side="left",pady=13)
        tk.Label(title_box,text="SMART ATTENDANCE SYSTEM",font=("Segoe UI",20,"bold"),fg=self.white,bg="#06101D").pack(anchor="w")
        tk.Label(title_box,text="COLLEGE PORTAL  •  8 LECTURES / DAY",font=("Segoe UI",9,"bold"),fg=self.cyan,bg="#06101D").pack(anchor="w")
        right=tk.Frame(top,bg="#06101D"); right.pack(side="right",fill="y",padx=(8,18))
        controls=tk.Frame(right,bg="#06101D"); controls.pack(side="right",pady=12)
        self.admin_lock_btn=tk.Button(controls,text="🔒  ADMIN LOGIN LOCKED",command=self.admin_lock_toggle,font=("Segoe UI",9,"bold"),fg=self.white,bg=self.red,relief="flat",bd=0,padx=12,pady=7,cursor="hand2"); self.admin_lock_btn.pack(side="left",padx=5)
        close_btn=tk.Button(controls,text="✕ CLOSE APP",command=self.close_main_window,font=("Segoe UI",9,"bold"),fg=self.white,bg="#E74C3C",activebackground="#C0392B",relief="flat",bd=0,padx=12,pady=7,cursor="hand2"); close_btn.pack(side="left",padx=5)
        clock_box=tk.Frame(right,bg="#06101D"); clock_box.pack(side="right",padx=8,pady=10)
        self.date_label=tk.Label(clock_box,text="",font=("Segoe UI",10,"bold"),fg=self.cyan,bg="#06101D"); self.date_label.pack(anchor="e")
        self.clock_label=tk.Label(clock_box,text="",font=("Segoe UI",16,"bold"),fg=self.white,bg="#06101D"); self.clock_label.pack(anchor="e")
        self.login_btn=tk.Button(top,text="🔐 LOGIN",command=self.login_gui,font=("Segoe UI",9,"bold"),fg=self.white,bg=self.blue,relief="flat",bd=0,padx=13,pady=7,cursor="hand2"); self.login_btn.pack(side="right",padx=7,pady=32)

        # ============================================================
        # MAIN DASHBOARD SCROLL AREA
        # Keep the existing dashboard content and Holiday Calendar
        # exactly as they are, but allow the main dashboard to scroll
        # vertically on smaller screens.
        # ============================================================
        main_area = tk.Frame(self.root, bg=self.bg)
        main_area.pack(fill="both", expand=True)

        self.main_canvas = tk.Canvas(
            main_area,
            bg=self.bg,
            highlightthickness=0,
            bd=0
        )

        self.main_scrollbar = tk.Scrollbar(
            main_area,
            orient="vertical",
            command=self.main_canvas.yview,
            width=18,
            bg="#1688E8",
            activebackground="#35B7FF",
            troughcolor="#061A2E",
            relief="flat",
            bd=0,
            highlightthickness=0
        )

        self.main_canvas.configure(
            yscrollcommand=self.main_scrollbar.set
        )

        self.main_canvas.pack(side="left", fill="both", expand=True)
        self.main_scrollbar.pack(side="right", fill="y")

        content = tk.Frame(self.main_canvas, bg=self.bg)
        content_window = self.main_canvas.create_window(
            (0, 0),
            window=content,
            anchor="nw"
        )

        def update_main_scrollregion(event=None):
            self.main_canvas.configure(
                scrollregion=self.main_canvas.bbox("all")
            )

        def resize_main_content(event):
            self.main_canvas.itemconfigure(
                content_window,
                width=event.width
            )
            update_main_scrollregion()

        content.bind("<Configure>", update_main_scrollregion)
        self.main_canvas.bind("<Configure>", resize_main_content)

        def main_mousewheel(event):
            bbox = self.main_canvas.bbox("all")
            if bbox and bbox[3] > self.main_canvas.winfo_height():
                self.main_canvas.yview_scroll(
                    int(-1 * (event.delta / 120)), "units"
                )

        self.main_canvas.bind_all("<MouseWheel>", main_mousewheel)
        self.main_canvas.bind_all(
            "<Button-4>",
            lambda e: self.main_canvas.yview_scroll(-1, "units")
        )
        self.main_canvas.bind_all(
            "<Button-5>",
            lambda e: self.main_canvas.yview_scroll(1, "units")
        )

        # Existing layout uses this variable for all dashboard widgets.
        content.configure(padx=35, pady=22)

        tk.Label(content, text="MAIN MENU", font=("Segoe UI",24,"bold"),
                 fg=self.white, bg=self.bg).pack(pady=(0,3))
        tk.Label(content, text="Select an option", font=("Segoe UI",10),
                 fg=self.muted, bg=self.bg).pack(pady=(0,16))

        menu = tk.Frame(content, bg=self.bg)
        menu.pack(fill="x", pady=(0,15))

        def menu_btn(text, cmd, color, r, c):
            b=tk.Button(menu, text=text, command=cmd, font=("Segoe UI",11,"bold"),
                         fg=self.white, bg=color, activebackground=color,
                         activeforeground=self.white, relief="flat", bd=0,
                         cursor="hand2", padx=18, pady=13)
            b.grid(row=r,column=c,padx=6,pady=6,sticky="nsew")
            return b

        actions=[
            ("👨‍🎓  STUDENT\nREGISTRATION", self.admin_register_student_gui, self.blue),
            ("📷  START\nATTENDANCE", self.start_attendance_gui, self.green),
            ("📚  8 LECTURES\nTIMETABLE", self.admin_open_timetable, self.purple),
            ("📊  ATTENDANCE\nREPORT", self.show_attendance_report, self.cyan),
            ("🚶  TEMP OUT / IN\nREPORT", self.show_movement_report, self.orange),
            ("🧠  TRAIN FACE\nMODEL", self.admin_train_model_gui, "#E04B8B")]
        for i,(txt,cmd,col) in enumerate(actions):
            menu_btn(txt,cmd,col,i//3,i%3)
        for c in range(3): menu.columnconfigure(c,weight=1)

        # Compact feature toolbar: the original dashboard remains unchanged;
        # these three buttons expose the new upgrades without replacing the main menu.
        feature_bar=tk.Frame(content,bg=self.bg)
        feature_bar.pack(fill="x",pady=(0,10))
        feature_bar.columnconfigure(0,weight=1)
        feature_bar.columnconfigure(1,weight=1)
        feature_bar.columnconfigure(2,weight=1)

        def feature_btn(text, command, color, col):
            b=tk.Button(
                feature_bar, text=text, command=command,
                font=("Segoe UI",9,"bold"), fg=self.white, bg=color,
                activebackground=color, activeforeground=self.white,
                relief="flat", bd=0, cursor="hand2", padx=10, pady=8
            )
            b.grid(row=0,column=col,padx=5,sticky="ew")
            return b

        feature_btn("👤  STUDENT SUMMARY", self.show_student_summary, self.blue, 0)
        feature_btn("📅  HISTORY / EXPORT", self.show_history_export, self.purple, 1)
        feature_btn("🔐  LOGIN", self.login_gui, self.red, 2)

        advanced=tk.Frame(content,bg=self.bg); advanced.pack(fill="x",pady=(0,10))
        advanced.columnconfigure(0,weight=1); advanced.columnconfigure(1,weight=1); advanced.columnconfigure(2,weight=1); advanced.columnconfigure(3,weight=1); advanced.columnconfigure(4,weight=1)
        for i,(txt,cmd,col) in enumerate([("📊 ANALYTICS",self.show_analytics,self.blue),("⚠ LOW ATTENDANCE",self.show_low_attendance_alerts,self.orange),("👨‍🏫 TEACHER",self.show_teacher_dashboard,self.green),("🟢 HEALTH",self.show_system_health,self.cyan),("🏖 HOLIDAY CALENDAR",self.show_holiday_calendar,self.purple),("🗑 DELETE REGISTERED STUDENT",self.delete_registered_student,self.red)]):
            tk.Button(advanced,text=txt,command=cmd,font=("Segoe UI",8,"bold"),fg=self.white,bg=col,relief="flat",padx=7,pady=7).grid(row=0,column=i,padx=3,sticky="ew")
        advanced2=tk.Frame(content,bg=self.bg); advanced2.pack(fill="x",pady=(0,10))
        advanced2.columnconfigure(0,weight=1); advanced2.columnconfigure(1,weight=1); advanced2.columnconfigure(2,weight=1); advanced2.columnconfigure(3,weight=1); advanced2.columnconfigure(4,weight=1)
        for i,(txt,cmd,col) in enumerate([("📷 QR BACKUP",self.qr_backup_attendance,"#0EA5E9"),("🧾 GENERATE QR",self.generate_student_qr,self.purple),("📄 PDF REPORT",self.generate_pdf_report,"#E04B8B"),("🕵 AUDIT LOG",self.show_audit_log,"#475569"),("💾 BACKUP NOW",self.backup_now,"#0F766E")]):
            tk.Button(advanced2,text=txt,command=cmd,font=("Segoe UI",8,"bold"),fg=self.white,bg=col,relief="flat",padx=7,pady=7).grid(row=0,column=i,padx=3,sticky="ew")
        advanced3=tk.Frame(content,bg=self.bg); advanced3.pack(fill="x",pady=(0,10)); tk.Button(advanced3,text="🛡 TAMPER PROTECTION",command=self.tamper_protection_info,font=("Segoe UI",8,"bold"),fg=self.white,bg="#334155",relief="flat",padx=10,pady=7).pack(side="left",fill="x",expand=True,padx=(0,4)); tk.Button(advanced3,text="🚀 ADVANCED FEATURES CENTER",command=self.show_advanced_features_center,font=("Segoe UI",8,"bold"),fg=self.white,bg="#0F766E",relief="flat",padx=10,pady=7).pack(side="left",fill="x",expand=True,padx=(4,0))

        info=tk.Frame(content,bg=self.panel,highlightthickness=1,highlightbackground="#24466A")
        info.pack(fill="x", pady=(0,10))
        self.holiday_status_label=tk.Label(
            info, text="", font=("Segoe UI",9,"bold"),
            fg=self.green, bg=self.panel, padx=10, pady=7
        )
        self.holiday_status_label.pack(side="right", padx=6)
        self.lecture_title=tk.Label(info,text="",font=("Segoe UI",15,"bold"),fg=self.white,bg=self.panel)
        self.lecture_title.pack(side="left",padx=18,pady=11)
        self.live_status=tk.Label(
            info, text="● LIVE", font=("Segoe UI",10,"bold"),
            fg=self.green, bg=self.panel, padx=10, pady=7
        )
        self.live_status.pack(side="right",padx=(0,8))
        self.window_badge=tk.Label(info,text="",font=("Segoe UI",10,"bold"),padx=14,pady=7)
        self.window_badge.pack(side="right",padx=18)

        tabs=tk.Frame(content,bg=self.bg)
        tabs.pack(fill="x",pady=(0,8))
        self.lecture_buttons=[]
        for i,lecture in enumerate(self.lectures):
            b=tk.Button(tabs,text=f"L{i+1}",command=lambda n=i:self.select_lecture(n),
                        font=("Segoe UI",9,"bold"),fg=self.white,
                        bg=self.blue if i==0 else self.panel2,
                        activebackground=self.blue,relief="flat",bd=0,padx=12,pady=7)
            b.pack(side="left",padx=3)
            self.lecture_buttons.append(b)

        stats=tk.Frame(content,bg=self.bg)
        stats.pack(fill="x",pady=(0,10))
        self.stat_values={}
        for i,(title,color) in enumerate([
            ("PRESENT",self.green),("LATE",self.orange),
            ("TEMP OUT",self.purple),("FINAL OUT",self.red)]):
            card=tk.Frame(stats,bg=self.panel,highlightthickness=1,highlightbackground="#24466A")
            card.grid(row=0,column=i,sticky="ew",padx=4)
            stats.columnconfigure(i,weight=1)
            tk.Label(card,text=title,font=("Segoe UI",8,"bold"),fg=self.muted,bg=self.panel).pack(anchor="w",padx=14,pady=(7,0))
            val=tk.Label(card,text="0",font=("Segoe UI",18,"bold"),fg=color,bg=self.panel)
            val.pack(anchor="w",padx=14,pady=(0,7))
            self.stat_values[title]=val

        table_frame=tk.Frame(content,bg=self.panel)
        table_frame.pack(fill="both",expand=True)
        cols=("id","name","roll","course","semester","section","in","out","status")
        self.tree=ttk.Treeview(table_frame,columns=cols,show="headings")
        heads={"id":"Student ID","name":"Name","roll":"Roll No","course":"Course","semester":"Sem","section":"Sec","in":"IN","out":"OUT","status":"Status"}
        widths={"id":85,"name":145,"roll":70,"course":120,"semester":50,"section":45,"in":80,"out":80,"status":135}
        for col in cols:
            self.tree.heading(col,text=heads[col]); self.tree.column(col,width=widths[col],anchor="center")
        self.tree.tag_configure("present", background=self.green, foreground="#FFFFFF")
        self.tree.tag_configure("late", background=self.orange, foreground="#FFFFFF")
        self.tree.tag_configure("out", background=self.purple, foreground="#FFFFFF")
        self.tree.tag_configure("closed", background=self.red, foreground="#FFFFFF")
        scroll=ttk.Scrollbar(table_frame,orient="vertical",command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left",fill="both",expand=True)
        scroll.pack(side="right",fill="y")

        footer=tk.Frame(self.root,bg="#06101D",height=42)
        footer.pack(fill="x")
        footer.pack_propagate(False)
        tk.Label(footer,text="ESC  Exit  •  F11  Full Screen  •  Close Window available on every screen  •  SQLite: lecture-wise records  •  Reports / Search / Export / Admin",
                 font=("Segoe UI",8),fg=self.muted,bg="#06101D").pack(side="left",padx=25,pady=12)

        self.root.after(100, self.update_clock)
        self.refresh_admin_indicator()

    def make_stat(self, parent, title, value, color, column):
        card = tk.Frame(
            parent,
            bg=self.white,
            highlightthickness=1,
            highlightbackground="#E2E8F0"
        )
        card.grid(
            row=0,
            column=column,
            sticky="ew",
            padx=(0 if column == 0 else 8, 8)
        )

        parent.grid_columnconfigure(
            column,
            weight=1
        )

        tk.Frame(
            card,
            bg=color,
            width=6
        ).pack(
            side="left",
            fill="y"
        )

        inner = tk.Frame(
            card,
            bg=self.white
        )
        inner.pack(
            fill="both",
            expand=True,
            padx=14,
            pady=10
        )

        self.label(
            inner,
            title,
            9,
            "bold",
            self.muted,
            self.white
        ).pack(anchor="w")

        value_label = self.label(
            inner,
            value,
            21,
            "bold",
            color,
            self.white
        )
        value_label.pack(anchor="w")

        self.stat_values[title] = value_label

    def nav_button(self, text, command):
        b = tk.Button(
            self.side,
            text=text,
            command=command,
            anchor="w",
            font=("Segoe UI", 10, "bold"),
            fg="#E2E8F0",
            bg=self.sidebar,
            activebackground="#1E3A8A",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            padx=25,
            pady=12,
            cursor="hand2"
        )
        b.pack(
            fill="x",
            padx=10,
            pady=2
        )

    def action_button(self, parent, text, command, color):
        b = tk.Button(
            parent,
            text=text,
            command=command,
            font=("Segoe UI", 10, "bold"),
            fg="#FFFFFF",
            bg=color,
            activebackground=color,
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            pady=11,
            cursor="hand2"
        )
        b.pack(
            fill="x",
            pady=5
        )

    def select_lecture(self, index):
        self.selected_lecture = index

        for i, b in enumerate(
            self.lecture_buttons
        ):
            if i == index:
                b.configure(
                    bg=self.blue,
                    fg="#FFFFFF"
                )
            else:
                b.configure(
                    bg="#E2E8F0",
                    fg=self.text
                )

        self.refresh_dashboard()

    def get_selected_lecture(self):
        if not self.lectures:
            return None

        return self.lectures[
            self.selected_lecture
        ]

    def timing_state(self, lecture):
        if not self.timetable_configured:
            return (
                "TIMETABLE NOT SET",
                "#243B53",
                self.cyan
            )

        now = datetime.now().time()

        if lecture_in_window(
            lecture,
            now
        ):
            return (
                "PRESENT WINDOW",
                "#DCFCE7",
                self.green
            )

        if final_out_window(
            lecture,
            now
        ):
            return (
                "FINAL OUT WINDOW",
                "#FEE2E2",
                self.red
            )

        if lecture_late_window(
            lecture,
            now
        ):
            return (
                "LATE WINDOW",
                "#FEF3C7",
                self.orange
            )

        if now > lecture["end"]:
            return (
                "LECTURE CLOSED",
                "#E2E8F0",
                self.muted
            )

        return (
            "LECTURE RUNNING",
            "#DBEAFE",
            self.blue
        )

    def refresh_dashboard(self):
        lecture = self.get_selected_lecture()

        if lecture is None:
            return

        state, bg, fg = self.timing_state(
            lecture
        )

        # Live countdown/status indicator.
        now_dt=datetime.now()
        if state=="PRESENT WINDOW":
            target=datetime.combine(now_dt.date(),lecture["start"])+timedelta(minutes=ATTENDANCE_GRACE_MINUTES)
            remaining=max(0,int((target-now_dt).total_seconds()))
            state=f"PRESENT WINDOW  •  {remaining//60:02d}:{remaining%60:02d} LEFT"
        elif state=="LATE WINDOW":
            target=datetime.combine(now_dt.date(),lecture["end"])-timedelta(minutes=FINAL_OUT_WINDOW_MINUTES)
            remaining=max(0,int((target-now_dt).total_seconds()))
            state=f"LATE WINDOW  •  FINAL OUT IN {remaining//60:02d}:{remaining%60:02d}"
        elif state=="FINAL OUT WINDOW":
            target=datetime.combine(now_dt.date(),lecture["end"])
            remaining=max(0,int((target-now_dt).total_seconds()))
            state=f"FINAL OUT OPEN  •  {remaining//60:02d}:{remaining%60:02d} LEFT"

        # Automatic holiday detection: show today's status on the dashboard.
        try:
            is_holiday, holiday_name, holiday_type = automatic_holiday_status()
            if is_holiday:
                self.holiday_status_label.configure(
                    text=f"🏖 HOLIDAY: {holiday_name}",
                    fg=self.orange
                )
            else:
                self.holiday_status_label.configure(
                    text="🟢 WORKING DAY",
                    fg=self.green
                )
        except Exception:
            self.holiday_status_label.configure(text="", fg=self.muted)

        if not self.timetable_configured:
            self.lecture_title.configure(
                text="8-LECTURE TIMETABLE  •  NOT SET"
            )
        else:
            self.lecture_title.configure(
                text=(
                    f"Lecture {lecture['number']}   "
                    f"{lecture['start'].strftime('%I:%M %p')} "
                    f"– "
                    f"{lecture['end'].strftime('%I:%M %p')}"
                )
            )

        self.window_badge.configure(
            text=state,
            bg=bg,
            fg=fg
        )

        for i, b in enumerate(
            self.lecture_buttons
        ):
            if i == self.selected_lecture:
                b.configure(
                    bg=self.blue,
                    fg="#FFFFFF"
                )
            else:
                b.configure(
                    bg="#E2E8F0",
                    fg=self.text
                )

        self.refresh_student_table(
            lecture
        )

        self.root.after(
            1500,
            self.refresh_dashboard
        )

    def refresh_student_table(self, lecture):
        for item in self.tree.get_children():
            self.tree.delete(item)

        students = pd.read_csv(
            STUDENT_FILE,
            dtype=str
        ).fillna("")

        attendance = get_attendance_df()
        movement = get_movement_df()

        today = today_string()

        lecture_att = attendance[
            (attendance["Date"] == today)
            &
            (
                attendance["Lecture_No"]
                == str(lecture["number"])
            )
        ]

        lecture_mov = movement[
            (movement["Date"] == today)
            &
            (
                movement["Lecture_No"]
                == str(lecture["number"])
            )
        ]

        present = 0
        late = 0
        temp_out = 0
        final_out = 0

        for _, student in students.iterrows():
            sid = str(
                student["Student_ID"]
            )

            rows = lecture_att[
                lecture_att["Student_ID"]
                == sid
            ]

            in_time = ""
            out_time = ""
            status = "Absent"
            tag = "closed"

            if not rows.empty:
                row = rows.iloc[-1]

                in_time = str(
                    row["In_Time"]
                )

                out_time = str(
                    row["Out_Time"]
                )

                status = str(
                    row["Status"]
                )

                if status == "Present":
                    present += 1
                    tag = "present"
                elif status == "Late":
                    late += 1
                    tag = "late"

                if out_time.strip():
                    final_out += 1

            mov_rows = lecture_mov[
                lecture_mov["Student_ID"]
                == sid
            ]

            current_movement = "IN"

            if not mov_rows.empty:
                last = mov_rows.iloc[-1]

                if str(
                    last["Movement_Type"]
                ) == "OUT":
                    current_movement = (
                        "TEMP OUT - "
                        + str(last["Reason"])
                    )
                    temp_out += 1
                    tag = "out"

            if status == "Absent":
                display_status = "ABSENT"
            elif current_movement != "IN":
                display_status = current_movement
            elif status == "Late":
                display_status = "LATE"
            elif out_time.strip():
                display_status = "FINAL OUT"
            else:
                display_status = "PRESENT"

            self.tree.insert(
                "",
                "end",
                values=(
                    sid,
                    student["Name"],
                    student["Roll_No"],
                    student["Course"],
                    student["Semester"],
                    student["Section"],
                    in_time,
                    out_time,
                    display_status
                ),
                tags=(tag,)
            )

        self.stat_values[
            "PRESENT"
        ].configure(
            text=str(present)
        )

        self.stat_values[
            "LATE"
        ].configure(
            text=str(late)
        )

        self.stat_values[
            "TEMP OUT"
        ].configure(
            text=str(temp_out)
        )

        self.stat_values[
            "FINAL OUT"
        ].configure(
            text=str(final_out)
        )

    def update_clock(self):
        """Safely update the dashboard date/time without depending on header_sub."""
        now = datetime.now()

        # These widgets are created by build_ui(). Guard them so the clock
        # cannot crash during startup or while a window is being rebuilt.
        if hasattr(self, "date_label") and self.date_label.winfo_exists():
            self.date_label.configure(
                text=now.strftime("%A, %d %B %Y")
            )

        if hasattr(self, "clock_label") and self.clock_label.winfo_exists():
            self.clock_label.configure(
                text=now.strftime("%I:%M:%S %p")
            )

        self.root.after(1000, self.update_clock)

    def open_timetable(self):
        """ATM-style timetable editor with a reliable hour/minute/AM-PM picker."""
        win = tk.Toplevel(self.root)
        win.title("8 Lecture Timetable")
        win.geometry("980x760")
        win.configure(bg=self.bg)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", win.destroy)

        header = tk.Frame(win, bg="#06101D", height=92)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(
            header,
            text="📚  8 LECTURE TIMETABLE",
            font=("Segoe UI", 20, "bold"),
            fg=self.white,
            bg="#06101D"
        ).pack(anchor="w", padx=28, pady=(18, 3))

        tk.Label(
            header,
            text="Choose every lecture Start and End time • No long scrolling list",
            font=("Segoe UI", 9),
            fg=self.cyan,
            bg="#06101D"
        ).pack(anchor="w", padx=30)

        card = tk.Frame(
            win,
            bg=self.panel,
            highlightthickness=1,
            highlightbackground="#24466A"
        )
        card.pack(fill="both", expand=True, padx=28, pady=20)

        tk.Label(
            card,
            text="Lecture             Start Time                         End Time",
            font=("Segoe UI", 11, "bold"),
            fg=self.white,
            bg=self.panel
        ).pack(anchor="w", padx=28, pady=(18, 10))

        form = tk.Frame(card, bg=self.panel)
        form.pack(fill="both", expand=True, padx=28)

        start_vars = []
        end_vars = []

        def parse_time(value):
            return datetime.strptime(value.strip(), "%I:%M %p").time()

        def choose_time(target_var, title):
            """Reliable picker: separate Hour, Minute and AM/PM controls."""
            picker = tk.Toplevel(win)
            picker.title(title)
            picker.geometry("460x300")
            picker.configure(bg="#0B1726")
            picker.resizable(False, False)
            picker.transient(win)
            picker.grab_set()
            picker.protocol("WM_DELETE_WINDOW", picker.destroy)

            tk.Label(
                picker,
                text=title,
                font=("Segoe UI", 16, "bold"),
                fg=self.white,
                bg="#0B1726"
            ).pack(pady=(18, 12))

            current = target_var.get().strip()
            try:
                current_dt = datetime.strptime(current, "%I:%M %p")
                default_h = current_dt.strftime("%I")
                default_m = current_dt.strftime("%M")
                default_ampm = current_dt.strftime("%p")
            except ValueError:
                default_h, default_m, default_ampm = "09", "00", "AM"

            controls = tk.Frame(picker, bg="#0B1726")
            controls.pack(pady=10)

            hour_var = tk.StringVar(value=default_h)
            minute_var = tk.StringVar(value=default_m)
            ampm_var = tk.StringVar(value=default_ampm)

            def make_box(parent, variable, values, width):
                box = ttk.Combobox(
                    parent,
                    textvariable=variable,
                    values=values,
                    state="readonly",
                    width=width,
                    font=("Segoe UI", 13)
                )
                box.pack(side="left", padx=6, ipady=6)
                return box

            make_box(controls, hour_var, [f"{h:02d}" for h in range(1, 13)], 5)
            tk.Label(controls, text=":", font=("Segoe UI", 18, "bold"),
                     fg=self.white, bg="#0B1726").pack(side="left")
            make_box(controls, minute_var,
                     [f"{m:02d}" for m in range(0, 60, 5)], 5)
            make_box(controls, ampm_var, ["AM", "PM"], 5)

            tk.Label(
                picker,
                text="Hour     :     Minute     AM/PM",
                font=("Segoe UI", 9),
                fg=self.muted,
                bg="#0B1726"
            ).pack(pady=(0, 12))

            def apply_time():
                value = f"{hour_var.get()}:{minute_var.get()} {ampm_var.get()}"
                try:
                    datetime.strptime(value, "%I:%M %p")
                except ValueError:
                    messagebox.showerror("Invalid Time", "Please select a valid time.", parent=picker)
                    return
                target_var.set(value)
                picker.destroy()

            actions = tk.Frame(picker, bg="#0B1726")
            actions.pack(pady=8)

            tk.Button(
                actions,
                text="✓  SET TIME",
                command=apply_time,
                font=("Segoe UI", 11, "bold"),
                fg=self.white,
                bg=self.green,
                activebackground=self.green,
                relief="flat",
                bd=0,
                padx=28,
                pady=10,
                cursor="hand2"
            ).pack(side="left", padx=7)

            tk.Button(
                actions,
                text="CANCEL",
                command=picker.destroy,
                font=("Segoe UI", 10, "bold"),
                fg=self.white,
                bg=self.red,
                activebackground=self.red,
                relief="flat",
                bd=0,
                padx=22,
                pady=10,
                cursor="hand2"
            ).pack(side="left", padx=7)

        for i in range(MAX_LECTURES):
            row = tk.Frame(form, bg=self.panel)
            row.pack(fill="x", pady=4)

            tk.Label(
                row,
                text=f"LECTURE {i + 1}",
                font=("Segoe UI", 10, "bold"),
                fg=self.white,
                bg=self.panel,
                width=14,
                anchor="w"
            ).pack(side="left")

            start_var = tk.StringVar()
            end_var = tk.StringVar()
            start_vars.append(start_var)
            end_vars.append(end_var)

            if self.timetable_configured and i < len(self.lectures):
                start_var.set(self.lectures[i]["start"].strftime("%I:%M %p"))
                end_var.set(self.lectures[i]["end"].strftime("%I:%M %p"))
            else:
                # Useful defaults, editable for every lecture.
                default_start_minutes = 9 * 60 + i * 60
                default_end_minutes = default_start_minutes + 50
                def fmt(total):
                    h = (total // 60) % 24
                    m = total % 60
                    suffix = "AM" if h < 12 else "PM"
                    hh = h % 12 or 12
                    return f"{hh:02d}:{m:02d} {suffix}"
                start_var.set(fmt(default_start_minutes))
                end_var.set(fmt(default_end_minutes))

            tk.Button(
                row,
                textvariable=start_var,
                command=lambda v=start_var, n=i + 1: choose_time(v, f"Lecture {n} - Start Time"),
                font=("Segoe UI", 10, "bold"),
                fg=self.white,
                bg="#163B63",
                activebackground="#1D4F80",
                relief="flat",
                bd=0,
                width=18,
                padx=6,
                pady=7,
                cursor="hand2"
            ).pack(side="left", padx=8)

            tk.Label(
                row,
                text="→",
                font=("Segoe UI", 13, "bold"),
                fg=self.cyan,
                bg=self.panel
            ).pack(side="left", padx=8)

            tk.Button(
                row,
                textvariable=end_var,
                command=lambda v=end_var, n=i + 1: choose_time(v, f"Lecture {n} - End Time"),
                font=("Segoe UI", 10, "bold"),
                fg=self.white,
                bg="#163B63",
                activebackground="#1D4F80",
                relief="flat",
                bd=0,
                width=18,
                padx=6,
                pady=7,
                cursor="hand2"
            ).pack(side="left", padx=8)

        tk.Label(
            card,
            text=(
                "🟢 First 5 minutes = PRESENT    "
                "🟠 After 5 minutes = LATE    "
                "🔴 Last 5 minutes = FINAL OUT"
            ),
            font=("Segoe UI", 9, "bold"),
            fg=self.muted,
            bg=self.panel
        ).pack(pady=(8, 10))

        def save_gui_timetable():
            new_lectures = []
            previous_end = None

            try:
                for i in range(MAX_LECTURES):
                    start = parse_time(start_vars[i].get())
                    end = parse_time(end_vars[i].get())

                    if start >= end:
                        messagebox.showerror(
                            "Invalid Time",
                            f"Lecture {i + 1}: End time must be after Start time.",
                            parent=win
                        )
                        return

                    if previous_end is not None and start < previous_end:
                        messagebox.showerror(
                            "Overlapping Lectures",
                            f"Lecture {i + 1} starts before Lecture {i} ends.\n\n"
                            f"Lecture {i} ends at {previous_end.strftime('%I:%M %p')}.",
                            parent=win
                        )
                        return

                    new_lectures.append({
                        "number": i + 1,
                        "start": start,
                        "end": end
                    })
                    previous_end = end

                save_timetable(new_lectures)
                sync_timetable_sqlite(new_lectures)
                self.lectures = new_lectures
                self.timetable_configured = True
                self.selected_lecture = 0
                win.destroy()
                self.refresh_dashboard()
                messagebox.showinfo(
                    "Timetable Saved",
                    "All 8 lecture timings have been saved successfully.",
                    parent=self.root
                )
            except ValueError:
                messagebox.showerror(
                    "Invalid Time",
                    "Please set valid Start and End times for all 8 lectures.",
                    parent=win
                )

        actions = tk.Frame(card, bg=self.panel)
        actions.pack(pady=(0, 16))

        tk.Button(
            actions,
            text="💾  SAVE 8 LECTURES",
            command=save_gui_timetable,
            font=("Segoe UI", 11, "bold"),
            fg=self.white,
            bg=self.green,
            activebackground=self.green,
            relief="flat",
            bd=0,
            padx=24,
            pady=11,
            cursor="hand2"
        ).pack(side="left", padx=8)

        tk.Button(
            actions,
            text="←  BACK",
            command=win.destroy,
            font=("Segoe UI", 10, "bold"),
            fg=self.white,
            bg=self.red,
            activebackground=self.red,
            relief="flat",
            bd=0,
            padx=22,
            pady=11,
            cursor="hand2"
        ).pack(side="left", padx=8)

    def show_dashboard(self):
        self.refresh_dashboard()

    def start_attendance_gui(self):
        # Block the START ATTENDANCE button before timetable/camera setup.
        is_holiday, holiday_name, holiday_type = automatic_holiday_status()
        if is_holiday:
            messagebox.showinfo(
                "🏖 Holiday - Attendance Disabled",
                f"Today is a holiday.\n\n{holiday_name}\nType: {holiday_type}\n\nSTART ATTENDANCE cannot be used today.",
                parent=self.root
            )
            return

        if not self.timetable_configured:
            messagebox.showwarning(
                "Timetable Required",
                "Please set all 8 lecture times first from\n"
                "📚 8 LECTURES TIMETABLE.",
                parent=self.root
            )
            self.open_timetable()
            return

        # The existing camera attendance engine runs in a
        # separate OpenCV window. Closing it returns here.
        self.root.withdraw()

        try:
            start_attendance(
                self.lectures
            )
        finally:
            self.root.deiconify()
            self.refresh_dashboard()

    def register_student_gui(self):
        """Modern college registration form with dropdowns."""
        win = tk.Toplevel(self.root)
        win.title("Student Registration")
        win.geometry("700x790")
        win.configure(bg="#F4F7FB")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        header = tk.Frame(win, bg="#172554", height=105)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(
            header,
            text="STUDENT REGISTRATION",
            font=("Segoe UI", 21, "bold"),
            fg="#FFFFFF",
            bg="#172554"
        ).pack(anchor="w", padx=30, pady=(20, 2))

        tk.Label(
            header,
            text="College Student  -  Face Registration",
            font=("Segoe UI", 10),
            fg="#BFDBFE",
            bg="#172554"
        ).pack(anchor="w", padx=30)

        card = tk.Frame(
            win,
            bg="#FFFFFF",
            highlightthickness=1,
            highlightbackground="#DDE5F0"
        )
        card.pack(fill="both", expand=True, padx=35, pady=25)

        form = tk.Frame(card, bg="#FFFFFF")
        form.pack(fill="both", expand=True, padx=30, pady=25)

        def field_label(text, row):
            tk.Label(
                form,
                text=text,
                font=("Segoe UI", 10, "bold"),
                fg="#334155",
                bg="#FFFFFF"
            ).grid(row=row, column=0, sticky="w", pady=(5, 3))

        def entry_box(row):
            entry = tk.Entry(
                form,
                font=("Segoe UI", 10),
                bg="#F8FAFC",
                fg="#172033",
                relief="flat",
                highlightthickness=1,
                highlightbackground="#CBD5E1",
                highlightcolor="#2563EB"
            )
            entry.grid(row=row, column=1, sticky="ew", padx=(18, 0), ipady=7, pady=4)
            return entry

        field_label("Student ID", 0)
        student_id_entry = entry_box(0)

        field_label("Name", 1)
        name_entry = entry_box(1)

        field_label("Roll No", 2)
        roll_entry = entry_box(2)

        def combo_box(row, values):
            combo = ttk.Combobox(
                form,
                values=values,
                state="readonly",
                font=("Segoe UI", 10)
            )
            combo.grid(row=row, column=1, sticky="ew", padx=(18, 0), ipady=5, pady=4)
            if values:
                combo.current(0)
            return combo

        field_label("Course", 3)
        course_combo = combo_box(
            3,
            [
                "B.Sc Physics",
                "B.Sc Mathematics",
                "B.Sc Chemistry",
                "B.Sc Computer Science",
                "B.A",
                "B.Com",
                "B.Tech",
                "BCA",
                "M.Sc",
                "M.A",
                "M.Com",
                "Other"
            ]
        )

        field_label("Semester", 4)
        semester_combo = combo_box(4, ["1", "2", "3", "4", "5", "6", "7", "8"])

        field_label("Section", 5)
        section_combo = combo_box(5, ["A", "B", "C", "D", "E", "F"])

        field_label("Parent Mobile", 6)
        parent_mobile_entry = entry_box(6)

        field_label("Parent Email", 7)
        parent_email_entry = entry_box(7)

        form.grid_columnconfigure(1, weight=1)

        def submit_registration():
            student_id = student_id_entry.get().strip()
            name = name_entry.get().strip()
            roll_no = roll_entry.get().strip()
            course = course_combo.get().strip()
            semester = semester_combo.get().strip()
            section = section_combo.get().strip()
            parent_mobile = parent_mobile_entry.get().strip()
            parent_email = parent_email_entry.get().strip()

            if not all([student_id, name, roll_no, course, semester, section]):
                messagebox.showwarning(
                    "Missing Details",
                    "Please fill all student details.",
                    parent=win
                )
                return

            required = [
                "Student_ID", "Name", "Roll_No",
                "Course", "Semester", "Section",
                "Parent_Mobile", "Parent_Email"
            ]

            try:
                students = pd.read_csv(STUDENT_FILE, dtype=str).fillna("")
            except Exception:
                students = pd.DataFrame(columns=required)

            if "Class" in students.columns and "Course" not in students.columns:
                students["Course"] = students["Class"]

            for col in required:
                if col not in students.columns:
                    students[col] = ""

            if (students["Student_ID"].astype(str) == student_id).any():
                messagebox.showerror(
                    "Duplicate Student ID",
                    "This Student ID already exists.",
                    parent=win
                )
                return

            if (students["Roll_No"].astype(str) == roll_no).any():
                messagebox.showerror(
                    "Duplicate Roll No",
                    "This Roll No already exists.",
                    parent=win
                )
                return

            new_row = {
                "Student_ID": student_id,
                "Name": name,
                "Roll_No": roll_no,
                "Course": course,
                "Semester": semester,
                "Section": section,
                "Parent_Mobile": parent_mobile,
                "Parent_Email": parent_email
            }

            students = pd.concat(
                [students[required], pd.DataFrame([new_row])],
                ignore_index=True
            )

            students.to_csv(STUDENT_FILE, index=False)
            sync_students_sqlite()

            messagebox.showinfo(
                "Registration Saved",
                "Student registered successfully. Face capture will start next.",
                parent=win
            )

            win.destroy()
            self.root.update_idletasks()

            if "capture_face_samples" in globals():
                try:
                    capture_face_samples(student_id, name)
                except TypeError:
                    try:
                        capture_face_samples(student_id)
                    except Exception as exc:
                        messagebox.showwarning(
                            "Face Capture",
                            "Student saved, but face capture could not start: " + str(exc)
                        )
                except Exception as exc:
                    messagebox.showwarning(
                        "Face Capture",
                        "Student saved, but face capture could not start: " + str(exc)
                    )

            self.refresh_dashboard()

        button_row = tk.Frame(form, bg="#FFFFFF")
        button_row.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(22, 5))

        tk.Button(
            button_row,
            text="Cancel",
            command=win.destroy,
            font=("Segoe UI", 10, "bold"),
            fg="#475569",
            bg="#E2E8F0",
            activebackground="#CBD5E1",
            relief="flat",
            bd=0,
            padx=22,
            pady=10,
            cursor="hand2"
        ).pack(side="right", padx=(8, 0))

        tk.Button(
            button_row,
            text="Save & Capture Face",
            command=submit_registration,
            font=("Segoe UI", 10, "bold"),
            fg="#FFFFFF",
            bg="#2563EB",
            activebackground="#1D4ED8",
            relief="flat",
            bd=0,
            padx=22,
            pady=10,
            cursor="hand2"
        ).pack(side="right")

        tk.Label(
            card,
            text="Student ID, Roll No and Name are required.",
            font=("Segoe UI", 9),
            fg="#64748B",
            bg="#FFFFFF"
        ).pack(anchor="w", padx=30, pady=(0, 18))

    def train_model_gui(self):
        self.root.withdraw()

        try:
            train_model()
        finally:
            self.root.deiconify()
            self.refresh_dashboard()

    def _sqlite_dates(self):
        with db_connect() as conn:
            rows=conn.execute("SELECT DISTINCT Attendance_Date FROM attendance WHERE Attendance_Date<>'' ORDER BY Attendance_Date DESC").fetchall()
        dates=[r[0] for r in rows if r[0]]
        if today_string() not in dates:
            dates.insert(0,today_string())
        return dates

    def _load_students_sqlite(self):
        with db_connect() as conn:
            return pd.read_sql_query(
                "SELECT Student_ID,Name,Roll_No,Course,Semester,Section FROM students ORDER BY Roll_No,Name",
                conn
            ).fillna("")

    def _load_attendance_sqlite(self, date_value=None):
        query="SELECT Student_ID,Attendance_Date,Lecture_No,Name,Course,Lecture_Start,Lecture_End,In_Time,Out_Time,Status FROM attendance"
        params=[]
        if date_value:
            query += " WHERE Attendance_Date=?"
            params.append(date_value)
        query += " ORDER BY Attendance_Date DESC, Lecture_No, Roll_No" if False else " ORDER BY Attendance_Date DESC, Lecture_No, Student_ID"
        with db_connect() as conn:
            return pd.read_sql_query(query,conn,params=params).fillna("")

    def _build_daily_summary(self,date_value):
        students=self._load_students_sqlite()
        attendance=self._load_attendance_sqlite(date_value)
        total_students=len(students)
        total_expected=total_students*MAX_LECTURES
        present=int((attendance["Status"]=="Present").sum()) if not attendance.empty else 0
        late=int((attendance["Status"]=="Late").sum()) if not attendance.empty else 0
        attended=present+late
        absent=max(0,total_expected-attended)
        temp_out=len(get_movement_df()[get_movement_df()["Date"]==date_value]) if os.path.exists(MOVEMENT_FILE) else 0
        final_out=int((attendance["Out_Time"].astype(str).str.strip()!="").sum()) if not attendance.empty else 0
        pct=(attended/total_expected*100) if total_expected else 0
        return {"students":total_students,"expected":total_expected,"present":present,"late":late,"attended":attended,"absent":absent,"temp":temp_out,"final":final_out,"pct":pct}

    def _student_summary_df(self,date_value,search=""):
        students=self._load_students_sqlite()
        attendance=self._load_attendance_sqlite(date_value)
        if search:
            s=search.lower()
            mask=(students["Student_ID"].str.lower().str.contains(s,na=False) |
                  students["Name"].str.lower().str.contains(s,na=False) |
                  students["Roll_No"].str.lower().str.contains(s,na=False))
            students=students[mask]
        rows=[]
        for _,st in students.iterrows():
            sid=str(st["Student_ID"])
            a=attendance[attendance["Student_ID"]==sid] if not attendance.empty else pd.DataFrame()
            attended=int((a["In_Time"].astype(str).str.strip()!="").sum()) if not a.empty else 0
            present=int((a["Status"]=="Present").sum()) if not a.empty else 0
            late=int((a["Status"]=="Late").sum()) if not a.empty else 0
            temp=0
            if os.path.exists(MOVEMENT_FILE):
                m=get_movement_df()
                temp=int(((m["Date"]==date_value)&(m["Student_ID"]==sid)&(m["Movement_Type"]=="OUT")).sum())
            pct=(attended/MAX_LECTURES*100)
            rows.append({"Student ID":sid,"Name":st["Name"],"Roll No":st["Roll_No"],"Course":st["Course"],"Sem":st["Semester"],"Sec":st["Section"],"Attended":f"{attended}/{MAX_LECTURES}","Present":present,"Late":late,"Temp OUT":temp,"Attendance %":f"{pct:.1f}%"})
        return pd.DataFrame(rows)

    def show_student_summary(self):
        win=tk.Toplevel(self.root)
        win.title("Student-wise 8 Lecture Summary")
        win.geometry("1250x650")
        win.configure(bg=self.bg)
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW",win.destroy)

        header=tk.Frame(win,bg="#06101D",height=90); header.pack(fill="x"); header.pack_propagate(False)
        tk.Label(header,text="👤  STUDENT-WISE 8 LECTURE SUMMARY",font=("Segoe UI",18,"bold"),fg=self.white,bg="#06101D").pack(side="left",padx=24,pady=(18,2))
        tk.Label(header,text="Attendance percentage + Present/Late/Temp OUT",font=("Segoe UI",9,"bold"),fg=self.cyan,bg="#06101D").pack(side="left",pady=(24,0))

        controls=tk.Frame(win,bg=self.panel); controls.pack(fill="x",padx=18,pady=14)
        tk.Label(controls,text="Date",font=("Segoe UI",10,"bold"),fg=self.white,bg=self.panel).pack(side="left",padx=(14,6),pady=10)
        date_var=tk.StringVar(value=today_string())
        date_box=ttk.Combobox(controls,textvariable=date_var,values=self._sqlite_dates(),state="normal",width=14,font=("Segoe UI",10)); date_box.pack(side="left",pady=10)
        tk.Label(controls,text="Search ID / Name / Roll",font=("Segoe UI",10,"bold"),fg=self.white,bg=self.panel).pack(side="left",padx=(20,6))
        search_var=tk.StringVar()
        search=tk.Entry(controls,textvariable=search_var,font=("Segoe UI",10),bg="#F8FAFC",fg="#172033",relief="flat",width=28); search.pack(side="left",ipady=6,pady=10)

        table_frame=tk.Frame(win,bg=self.bg); table_frame.pack(fill="both",expand=True,padx=18)
        cols=["Student ID","Name","Roll No","Course","Sem","Sec","Attended","Present","Late","Temp OUT","Attendance %"]
        tree=ttk.Treeview(table_frame,columns=cols,show="headings")
        for c in cols:
            tree.heading(c,text=c); tree.column(c,width=105 if c not in ("Name","Course") else 145,anchor="center")
        tree.pack(side="left",fill="both",expand=True)
        sb=ttk.Scrollbar(table_frame,orient="vertical",command=tree.yview); sb.pack(side="right",fill="y"); tree.configure(yscrollcommand=sb.set)

        summary=tk.Label(win,text="",font=("Segoe UI",11,"bold"),fg=self.white,bg=self.blue,pady=9); summary.pack(fill="x",padx=18,pady=(8,8))

        def refresh():
            try:
                d=datetime.strptime(date_var.get().strip(),"%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                messagebox.showerror("Invalid Date","Use YYYY-MM-DD.",parent=win); return
            df=self._student_summary_df(d,search_var.get().strip())
            for item in tree.get_children(): tree.delete(item)
            for _,r in df.iterrows(): tree.insert("","end",values=[r[c] for c in cols])
            avg=float(df["Attendance %"].str.rstrip("%").astype(float).mean()) if not df.empty else 0
            summary.configure(text=f"{len(df)} students shown  •  Average attendance: {avg:.1f}%  •  Date: {d}")

        tk.Button(controls,text="🔎  SEARCH / REFRESH",command=refresh,font=("Segoe UI",9,"bold"),fg=self.white,bg=self.blue,activebackground=self.blue,relief="flat",bd=0,padx=15,pady=8,cursor="hand2").pack(side="left",padx=8)
        tk.Button(controls,text="✕ CLOSE WINDOW",command=win.destroy,font=("Segoe UI",9,"bold"),fg=self.white,bg=self.red,activebackground=self.red,relief="flat",bd=0,padx=15,pady=8,cursor="hand2").pack(side="right",padx=8)
        search.bind("<Return>",lambda e:refresh())
        refresh()

    def show_history_export(self):
        win=tk.Toplevel(self.root)
        win.title("Date-wise History / Export")
        win.geometry("1250x680")
        win.configure(bg=self.bg)
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW",win.destroy)

        header=tk.Frame(win,bg="#06101D",height=90); header.pack(fill="x"); header.pack_propagate(False)
        tk.Label(header,text="📅  DATE-WISE HISTORY / EXPORT",font=("Segoe UI",19,"bold"),fg=self.white,bg="#06101D").pack(side="left",padx=24,pady=20)

        controls=tk.Frame(win,bg=self.panel); controls.pack(fill="x",padx=18,pady=14)
        tk.Label(controls,text="Date",font=("Segoe UI",10,"bold"),fg=self.white,bg=self.panel).pack(side="left",padx=(14,6),pady=10)
        date_var=tk.StringVar(value=today_string())
        date_box=ttk.Combobox(controls,textvariable=date_var,values=self._sqlite_dates(),state="normal",width=14,font=("Segoe UI",10)); date_box.pack(side="left",pady=10)
        summary_label=tk.Label(controls,text="",font=("Segoe UI",9,"bold"),fg=self.cyan,bg=self.panel); summary_label.pack(side="left",padx=18)

        frame=tk.Frame(win,bg=self.bg); frame.pack(fill="both",expand=True,padx=18)
        cols=["Student_ID","Attendance_Date","Lecture_No","Name","Course","Lecture_Start","Lecture_End","In_Time","Out_Time","Status"]
        tree=ttk.Treeview(frame,columns=cols,show="headings")
        for c in cols:
            tree.heading(c,text=c.replace("_"," ")); tree.column(c,width=110,anchor="center")
        tree.pack(side="left",fill="both",expand=True)
        sb=ttk.Scrollbar(frame,orient="vertical",command=tree.yview); sb.pack(side="right",fill="y"); tree.configure(yscrollcommand=sb.set)

        current_df=pd.DataFrame()
        def refresh():
            nonlocal current_df
            try:
                d=datetime.strptime(date_var.get().strip(),"%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                messagebox.showerror("Invalid Date","Use YYYY-MM-DD.",parent=win); return
            current_df=self._load_attendance_sqlite(d)
            for item in tree.get_children(): tree.delete(item)
            for _,r in current_df.iterrows(): tree.insert("","end",values=[r[c] for c in cols])
            s=self._build_daily_summary(d)
            summary_label.configure(text=f"Present {s['present']}  •  Late {s['late']}  •  Absent {s['absent']}  •  Temp OUT {s['temp']}  •  Avg {s['pct']:.1f}%")

        def export_csv():
            if current_df.empty:
                messagebox.showinfo("Export","No attendance rows for this date.",parent=win); return
            path=filedialog.asksaveasfilename(parent=win,title="Export CSV",defaultextension=".csv",filetypes=[("CSV files","*.csv")],initialfile=f"attendance_{date_var.get().strip()}.csv")
            if path:
                current_df.to_csv(path,index=False)
                messagebox.showinfo("Export Complete",f"CSV saved to:\n{path}",parent=win)

        def export_excel():
            if current_df.empty:
                messagebox.showinfo("Export","No attendance rows for this date.",parent=win); return
            path=filedialog.asksaveasfilename(parent=win,title="Export Excel",defaultextension=".xlsx",filetypes=[("Excel files","*.xlsx")],initialfile=f"attendance_{date_var.get().strip()}.xlsx")
            if not path: return
            try:
                summary_df=pd.DataFrame([self._build_daily_summary(date_var.get().strip())])
                with pd.ExcelWriter(path,engine="openpyxl") as writer:
                    current_df.to_excel(writer,index=False,sheet_name="Attendance")
                    summary_df.to_excel(writer,index=False,sheet_name="Summary")
                messagebox.showinfo("Export Complete",f"Excel saved to:\n{path}",parent=win)
            except Exception as exc:
                messagebox.showerror("Excel Export Failed",f"Could not create Excel file.\n\n{exc}",parent=win)

        tk.Button(controls,text="🔄 REFRESH",command=refresh,font=("Segoe UI",9,"bold"),fg=self.white,bg=self.blue,activebackground=self.blue,relief="flat",bd=0,padx=15,pady=8,cursor="hand2").pack(side="left",padx=5)
        tk.Button(controls,text="⬇ CSV",command=export_csv,font=("Segoe UI",9,"bold"),fg=self.white,bg=self.green,activebackground=self.green,relief="flat",bd=0,padx=15,pady=8,cursor="hand2").pack(side="left",padx=5)
        tk.Button(controls,text="⬇ EXCEL",command=export_excel,font=("Segoe UI",9,"bold"),fg=self.white,bg=self.orange,activebackground=self.orange,relief="flat",bd=0,padx=15,pady=8,cursor="hand2").pack(side="left",padx=5)
        tk.Button(controls,text="✕ CLOSE WINDOW",command=win.destroy,font=("Segoe UI",9,"bold"),fg=self.white,bg=self.red,activebackground=self.red,relief="flat",bd=0,padx=15,pady=8,cursor="hand2").pack(side="right",padx=5)
        refresh()

    def show_attendance_report(self):
        """Open the upgraded date-wise attendance history/report window."""
        self.show_history_export()

    def show_movement_report(self):
        df = get_movement_df()

        if df.empty:
            messagebox.showinfo(
                "Movement Report",
                "No movement records found."
            )
            return

        win = tk.Toplevel(
            self.root
        )
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.title(
            "Temporary OUT / IN Report"
        )
        win.geometry(
            "1050x550"
        )
        win.configure(
            bg=self.bg
        )

        frame = tk.Frame(
            win,
            bg=self.bg
        )
        frame.pack(
            fill="both",
            expand=True,
            padx=15,
            pady=15
        )

        tree = ttk.Treeview(
            frame,
            columns=list(df.columns),
            show="headings"
        )

        for col in df.columns:
            tree.heading(
                col,
                text=col
            )
            tree.column(
                col,
                width=120,
                anchor="center"
            )

        for _, row in df.iterrows():
            tree.insert(
                "",
                "end",
                values=list(row)
            )

        tree.pack(
            fill="both",
            expand=True
        )

        tk.Button(
            win, text="✕  CLOSE WINDOW", command=win.destroy,
            font=("Segoe UI", 10, "bold"), fg="#FFFFFF", bg="#DC2626",
            activebackground="#B91C1C", relief="flat", bd=0,
            padx=24, pady=10, cursor="hand2"
        ).pack(pady=(0, 15))



def show_animated_welcome(root):
    """Full-screen letter-flow welcome animation; closes directly into the dashboard."""
    import math
    import random
    import tkinter.font as tkfont

    splash = tk.Toplevel(root)
    splash.configure(bg="#020617")
    splash.attributes("-fullscreen", True)
    splash.attributes("-topmost", True)
    splash.protocol("WM_DELETE_WINDOW", lambda: None)
    splash.update_idletasks()
    splash.deiconify()
    splash.lift()
    splash.focus_force()

    sw = splash.winfo_screenwidth()
    sh = splash.winfo_screenheight()

    canvas = tk.Canvas(
        splash,
        bg="#020617",
        highlightthickness=0,
        bd=0
    )
    canvas.pack(fill="both", expand=True)

    cx, cy = sw // 2, sh // 2

    # Decorative neon background.
    canvas.create_arc(
        -sw * 0.18, -sh * 0.55,
        sw * 0.48, sh * 0.48,
        start=205, extent=72,
        style="arc", outline="#ff1493", width=5
    )
    canvas.create_arc(
        sw * 0.52, sh * 0.52,
        sw * 1.18, sh * 1.55,
        start=25, extent=72,
        style="arc", outline="#00bfff", width=5
    )

    # Floating particles.
    rng = random.Random(17)
    particles = []
    for _ in range(85):
        x = rng.randint(0, sw)
        y = rng.randint(0, sh)
        r = rng.choice((1, 1, 2, 2, 3))
        item = canvas.create_oval(
            x-r, y-r, x+r, y+r,
            fill=rng.choice(("#ff1493", "#00bfff", "#ffffff")),
            outline=""
        )
        particles.append([item, x, y, rng.uniform(0.25, 1.0), rng.uniform(0, math.tau)])

    title_font_size = max(52, min(104, int(sw * 0.078)))
    sub_font_size = max(25, min(52, int(sw * 0.034)))
    title_font = tkfont.Font(family="Segoe UI", size=title_font_size, weight="bold")
    sub_font = tkfont.Font(family="Segoe UI", size=sub_font_size, weight="bold")

    def letter_targets(text, font, spacing=0):
        widths = [font.measure(ch) for ch in text]
        total = sum(widths) + spacing * max(0, len(text)-1)
        x = cx - total / 2
        targets = []
        for w in widths:
            targets.append((x + w / 2, cy))
            x += w + spacing
        return targets

    title_targets = letter_targets("WELCOME", title_font, 2)
    sub_targets = letter_targets("SMART ATTENDENCE SYSTEM", sub_font, 1)

    title_y = cy - int(sh * 0.10)
    sub_y = cy + int(sh * 0.12)

    # Individual letters start around the screen and flow into place.
    letters = []
    for i, ch in enumerate("WELCOME"):
        angle = rng.uniform(0, math.tau)
        radius = rng.uniform(sw * 0.30, sw * 0.58)
        sx = cx + math.cos(angle) * radius
        sy = cy + math.sin(angle) * radius
        tx, _ = title_targets[i]
        item = canvas.create_text(
            sx, sy,
            text=ch,
            font=title_font,
            fill="#ff1493",
            anchor="center"
        )
        letters.append([item, sx, sy, tx, title_y, rng.uniform(0.9, 1.25)])

    subtitle_letters = []
    for i, ch in enumerate("SMART ATTENDENCE SYSTEM"):
        # Alternate entry sides to create a flowing/assembling effect.
        side = -1 if i % 2 == 0 else 1
        sx = -120 if side < 0 else sw + 120
        sy = sub_y + rng.uniform(-sh * 0.18, sh * 0.18)
        tx, _ = sub_targets[i]
        item = canvas.create_text(
            sx, sy,
            text=ch,
            font=sub_font,
            fill="#00bfff",
            anchor="center"
        )
        subtitle_letters.append([item, sx, sy, tx, sub_y, rng.uniform(0.85, 1.15)])

    underline = canvas.create_line(
        cx, sub_y + int(sh * 0.085),
        cx, sub_y + int(sh * 0.085),
        fill="#ff1493", width=4
    )
    star = canvas.create_text(
        cx, sub_y + int(sh * 0.085),
        text="★",
        font=("Segoe UI", max(20, int(sw * 0.025)), "bold"),
        fill="#ff1493"
    )

    frame = {"n": 0}
    total_frames = 55

    def ease(t):
        # Smooth ease-out-back: slight overshoot before settling.
        c1 = 1.70158
        c3 = c1 + 1
        return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2

    def move_item(item, sx, sy, tx, ty, progress, strength=1.0):
        # A gentle overshoot makes the letters feel like they are assembling.
        e = ease(min(1.0, progress))
        x = sx + (tx - sx) * e
        y = sy + (ty - sy) * e
        canvas.coords(item, x, y)
        return x, y

    def animate():
        n = frame["n"]
        p = min(n / total_frames, 1.0)

        # Title arrives first, subtitle follows a little later.
        title_p = min(1.0, p / 0.72)
        sub_p = min(1.0, max(0.0, (p - 0.22) / 0.68))

        for item, sx, sy, tx, ty, _ in letters:
            move_item(item, sx, sy, tx, ty, title_p)

        for item, sx, sy, tx, ty, _ in subtitle_letters:
            if sub_p > 0:
                move_item(item, sx, sy, tx, ty, sub_p)

        # Animated underline grows after the subtitle starts assembling.
        line_p = min(1.0, max(0.0, (p - 0.48) / 0.45))
        half = int(sw * 0.22 * line_p)
        ly = sub_y + int(sh * 0.085)
        canvas.coords(underline, cx-half, ly, cx+half, ly)
        canvas.itemconfig(star, state="normal" if line_p > 0.82 else "hidden")

        # Subtle particle motion.
        for part in particles:
            item, x, y, speed, phase = part
            y -= speed * 0.7
            x += math.sin(n * 0.035 + phase) * 0.25
            if y < -5:
                y = sh + 5
            canvas.coords(item, x-1, y-1, x+1, y+1)
            part[1], part[2] = x, y

        frame["n"] += 1
        if n < total_frames:
            splash.after(16, animate)  # ~60 FPS
        else:
            # No extra pause: immediately reveal the already-created dashboard.
            close_splash()

    def close_splash():
        try:
            splash.grab_release()
        except tk.TclError:
            pass
        try:
            splash.destroy()
        except tk.TclError:
            pass

    splash.grab_set()
    animate()
    root.wait_window(splash)

def main():
    initialize_files()
    migrate_student_schema()
    initialize_sqlite_layer()

    if not ensure_cascade():
        return

    # Timetable is now managed entirely from the ATM-style GUI.
    # No lecture-time input is requested in the terminal.
    lectures, timetable_configured = load_timetable()
    if timetable_configured:
        sync_timetable_sqlite(lectures)

    root = tk.Tk()
    root.withdraw()

    # Show the welcome animation immediately when the application starts.
    # If accounts already exist, prepare the dashboard first so it is ready
    # underneath the welcome screen and appears without a delay afterward.
    if _users_configured():
        app = SmartAttendanceDashboard(
            root,
            lectures,
            timetable_configured
        )
        root.deiconify()
        show_animated_welcome(root)
    else:
        show_animated_welcome(root)
        if not setup_user_accounts(root):
            root.destroy()
            return
        app = SmartAttendanceDashboard(
            root,
            lectures,
            timetable_configured
        )
        root.deiconify()

    root.mainloop()


if __name__ == "__main__":
    main()
