CREATE TABLE IF NOT EXISTS students (
  student_id TEXT PRIMARY KEY, name TEXT NOT NULL, roll_no TEXT, course TEXT, semester TEXT, section TEXT,
  parent_mobile TEXT, parent_email TEXT, face_descriptor JSONB, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS timetable (
  lecture_no INTEGER PRIMARY KEY, subject TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attendance (
  student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
  attendance_date DATE NOT NULL, lecture_no INTEGER NOT NULL, lecture_start TEXT, lecture_end TEXT,
  in_time TEXT, out_time TEXT, status TEXT, PRIMARY KEY(student_id,attendance_date,lecture_no)
);
CREATE TABLE IF NOT EXISTS movement (
  id BIGSERIAL PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
  attendance_date DATE NOT NULL, lecture_no INTEGER NOT NULL, movement_type TEXT NOT NULL, event_time TEXT NOT NULL, reason TEXT
);
CREATE TABLE IF NOT EXISTS user_accounts (
  username TEXT PRIMARY KEY, role TEXT NOT NULL, password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id BIGSERIAL PRIMARY KEY, username TEXT, role TEXT, action TEXT, details TEXT, event_time TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS holidays (
  holiday_date DATE PRIMARY KEY, name TEXT NOT NULL, holiday_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
  id BIGSERIAL PRIMARY KEY, student_id TEXT, student_name TEXT, channel TEXT, recipient TEXT, subject TEXT, message TEXT,
  status TEXT, details TEXT, sent_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS notification_settings (key TEXT PRIMARY KEY, value TEXT);
