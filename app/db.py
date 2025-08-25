import sqlite3, os, json
from .config import DB_PATH, DATA_DIR
DDL_JOBS = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL,
  type TEXT NOT NULL,
  priority TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  ack TEXT,
  job_url TEXT,
  dedup INTEGER DEFAULT 0,
  status TEXT DEFAULT 'queued',
  retries INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
"""
DDL_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER,
  stage TEXT NOT NULL,
  detail TEXT,
  meta_json TEXT,
  ts TEXT DEFAULT (datetime('now')),
  FOREIGN KEY(job_id) REFERENCES jobs(id)
);
"""
def connect():
  os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
  con = sqlite3.connect(DB_PATH, check_same_thread=False)
  con.execute("PRAGMA journal_mode=WAL;")
  con.execute("PRAGMA synchronous=NORMAL;")
  return con
def migrate(con: sqlite3.Connection):
  con.execute(DDL_JOBS)
  con.execute(DDL_EVENTS)
  con.commit()
def insert_job(con, rec):
  cur = con.cursor()
  try:
    cur.execute(
      "INSERT INTO jobs (idempotency_key, source, type, priority, timestamp, payload_json, dedup, status) VALUES (?,?,?,?,?,?,?,?)",
      (rec["idempotency_key"], rec["source"], rec["type"], rec["priority"], rec["timestamp"], json.dumps(rec["payload_json"], ensure_ascii=False), int(rec.get("dedup", 0)), rec.get("status","queued"))
    )
    con.commit()
    return cur.lastrowid, False
  except sqlite3.IntegrityError:
    cur.execute("SELECT id, ack, job_url FROM jobs WHERE idempotency_key = ?", (rec["idempotency_key"],))
    row = cur.fetchone()
    return (row[0] if row else None), True
def update_job_push(con, job_id: int, ack: str, job_url: str, status: str, retries: int):
  con.execute("UPDATE jobs SET ack=?, job_url=?, status=?, retries=?, updated_at=datetime('now') WHERE id=?",
              (ack, job_url, status, retries, job_id))
  con.commit()
def add_event(con, job_id: int, stage: str, detail: str = "", meta: dict | None = None):
  con.execute("INSERT INTO events (job_id, stage, detail, meta_json) VALUES (?,?,?,?)",
              (job_id, stage, detail, json.dumps(meta or {}, ensure_ascii=False)))
  con.commit()
def recent_jobs(con, hours: int = 24, limit: int = 50):
  cur = con.cursor()
  cur.execute(
    "SELECT id, idempotency_key, source, type, priority, timestamp, ack, job_url, dedup, status, retries, created_at, updated_at FROM jobs WHERE created_at >= datetime('now', ?) ORDER BY id DESC LIMIT ?",
    (f"-{hours} hours", limit)
  )
  cols = [d[0] for d in cur.description]
  return [dict(zip(cols, row)) for row in cur.fetchall()]
def errors_count(con, hours: int = 24):
  cur = con.cursor()
  cur.execute("SELECT COUNT(*) FROM events WHERE stage='error' AND ts >= datetime('now', ?)", (f"-{hours} hours",))
  return cur.fetchone()[0]
