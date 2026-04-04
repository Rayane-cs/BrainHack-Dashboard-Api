import os
import sys
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import Flask, request, jsonify
from flask_cors import CORS
import mysql.connector
import mysql.connector.pooling
from dotenv import load_dotenv

# Ensure the repo root (parent of api/) is on sys.path so that
# email_templates.py can always be found, regardless of CWD.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from email_templates import get_registration_confirmation_html, get_registration_confirmation_text

load_dotenv()

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# SMTP config
# ---------------------------------------------------------------------------
SMTP_HOST  = os.getenv("SMTP_HOST")
SMTP_PORT  = int(os.getenv("SMTP_PORT", 587))
SMTP_USER  = os.getenv("SMTP_USER")
SMTP_PASS  = os.getenv("SMTP_PASS")

# ---------------------------------------------------------------------------
# DB pool (lazy-init, one pool per process)
# ---------------------------------------------------------------------------
_db_pool = None

def get_db_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = mysql.connector.pooling.MySQLConnectionPool(
            pool_name="mypool",
            pool_size=5,
            host=os.getenv("DB_HOST"),
            port=int(os.getenv("DB_PORT", 3306)),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME"),
        )
    return _db_pool

def get_db_connection():
    return get_db_pool().get_connection()

# ---------------------------------------------------------------------------
# Run the status-column migration ONCE at startup, not on every request.
# ---------------------------------------------------------------------------
def _ensure_status_column():
    """Add the status column to the registrations table if it is missing."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME   = 'participants'
              AND COLUMN_NAME  = 'status'
        """)
        (count,) = cursor.fetchone()
        if count == 0:
            cursor.execute("""
                ALTER TABLE participants
                ADD COLUMN status ENUM('pending','accepted','rejected')
                    NOT NULL DEFAULT 'pending'
            """)
            conn.commit()
            print("status column added to participants table.")
        cursor.close()
        conn.close()
    except Exception as exc:
        print(f"[startup] Could not ensure status column: {exc}")

with app.app_context():
    try:
        _ensure_status_column()
    except Exception as e:
        print(f"[startup] DB not available yet: {e}")

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "message": "Dashboard API is running"})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def send_email(to_email: str, subject: str, html_content: str, text_content: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["From"]    = SMTP_USER
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(text_content, "plain"))
        msg.attach(MIMEText(html_content, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        return True
    except Exception as exc:
        print(f"Email sending failed: {exc}")
        return False

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}

    required = ["full_name", "email", "phone", "registration_number", "level", "speciality"]
    missing  = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    conn   = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO participants
                (full_name, email, phone, registration_number, level, speciality, portfolio_link, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
        """, (
            data["full_name"],
            data["email"],
            data["phone"],
            data["registration_number"],
            data["level"],
            data["speciality"],
            data.get("portfolio_link") or "",
        ))
        conn.commit()

        email_data = {
            "full_name":           data["full_name"],
            "email":               data["email"],
            "phone":               data["phone"],
            "registration_number": data["registration_number"],
            "level":               data["level"],
            "speciality":          data["speciality"],
            "portfolio_link":      data.get("portfolio_link") or "",
        }
        html_body = get_registration_confirmation_html(data["full_name"], "registration", email_data)
        text_body = get_registration_confirmation_text(data["full_name"], "registration")
        send_email(data["email"], "BrainHack 2026 – Registration Received", html_body, text_body)

        return jsonify({"message": "Registration successful"}), 201

    except mysql.connector.IntegrityError:
        return jsonify({"error": "A registration with this email or student ID already exists."}), 409
    except Exception as exc:
        print(f"[register] {exc}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        cursor.close()
        conn.close()


@app.route("/api/registrations", methods=["GET"])
def get_registrations():
    conn   = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT
                id, full_name, email, phone, registration_number,
                level, speciality, portfolio_link, status, created_at
            FROM participants
            ORDER BY created_at DESC
        """)
        rows = cursor.fetchall()
        for row in rows:
            if row.get("created_at"):
                row["created_at"] = row["created_at"].isoformat()
        return jsonify(rows)
    except Exception as exc:
        print(f"[get_registrations] {exc}")
        return jsonify({"error": "Failed to fetch registrations"}), 500
    finally:
        cursor.close()
        conn.close()


@app.route("/api/accept", methods=["POST"])
def accept_registration():
    data   = request.get_json(silent=True) or {}
    reg_id = data.get("id")
    if not reg_id:
        return jsonify({"error": "ID required"}), 400

    conn   = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM participants WHERE id = %s", (reg_id,))
        reg = cursor.fetchone()
        if not reg:
            return jsonify({"error": "Registration not found"}), 404

        cursor.execute("UPDATE participants SET status = 'accepted' WHERE id = %s", (reg_id,))
        conn.commit()

        html_body = get_registration_confirmation_html(reg["full_name"], "acceptance", reg)
        text_body = get_registration_confirmation_text(reg["full_name"], "acceptance")
        ok = send_email(reg["email"], "Congratulations! You've been accepted to BrainHack", html_body, text_body)

        msg = "Registration accepted and email sent" if ok else "Registration accepted but email failed"
        return jsonify({"message": msg})
    except Exception as exc:
        print(f"[accept] {exc}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        cursor.close()
        conn.close()


@app.route("/api/reject", methods=["POST"])
def reject_registration():
    data   = request.get_json(silent=True) or {}
    reg_id = data.get("id")
    if not reg_id:
        return jsonify({"error": "ID required"}), 400

    conn   = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM participants WHERE id = %s", (reg_id,))
        reg = cursor.fetchone()
        if not reg:
            return jsonify({"error": "Registration not found"}), 404

        cursor.execute("UPDATE participants SET status = 'rejected' WHERE id = %s", (reg_id,))
        conn.commit()

        html_body = get_registration_confirmation_html(reg["full_name"], "rejection", reg)
        text_body = get_registration_confirmation_text(reg["full_name"], "rejection")
        ok = send_email(reg["email"], "BrainHack Registration Update", html_body, text_body)

        msg = "Registration rejected and email sent" if ok else "Registration rejected but email failed"
        return jsonify({"message": msg})
    except Exception as exc:
        print(f"[reject] {exc}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        cursor.close()
        conn.close()