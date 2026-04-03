from flask import Flask, request, jsonify
from flask_cors import CORS
import mysql.connector
import mysql.connector.pooling
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
from dotenv import load_dotenv
import sys

# Make sure sibling modules (email_templates) are importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_templates import get_registration_confirmation_html, get_registration_confirmation_text

load_dotenv()

app = Flask(__name__)
CORS(app)

# SMTP settings
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")

# Lazy-initialized connection pool
_db_pool = None

def get_db_pool():
    global _db_pool
    if _db_pool is None:
        db_config = {
            "host": os.getenv("DB_HOST"),
            "port": int(os.getenv("DB_PORT", 3306)),
            "user": os.getenv("DB_USER"),
            "password": os.getenv("DB_PASSWORD"),
            "database": os.getenv("DB_NAME"),
            "pool_name": "mypool",
            "pool_size": 5,
        }
        _db_pool = mysql.connector.pooling.MySQLConnectionPool(**db_config)
    return _db_pool

def get_db_connection():
    return get_db_pool().get_connection()

def send_email(to_email, subject, html_content, text_content):
    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = SMTP_USER
        msg['To'] = to_email
        msg['Subject'] = subject

        msg.attach(MIMEText(text_content, 'plain'))
        msg.attach(MIMEText(html_content, 'html'))

        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"Email sending failed: {e}")
        return False

def ensure_status_column():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'registrations'
              AND COLUMN_NAME = 'status'
        """)
        (count,) = cursor.fetchone()
        if count == 0:
            cursor.execute("""
                ALTER TABLE registrations
                ADD COLUMN status ENUM('pending', 'accepted', 'rejected') DEFAULT 'pending'
            """)
            conn.commit()
    except Exception as e:
        print(f"Error ensuring status column: {e}")
    finally:
        cursor.close()
        conn.close()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/api/register', methods=['POST'])
def register():
    ensure_status_column()
    data = request.get_json()

    required = ['full_name', 'email', 'phone', 'registration_number', 'level', 'speciality']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO registrations
                (full_name, email, phone, registration_number, level, speciality, portfolio_link, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
        """, (
            data['full_name'],
            data['email'],
            data['phone'],
            data['registration_number'],
            data['level'],
            data['speciality'],
            data.get('portfolio_link', ''),
        ))
        conn.commit()

        email_data = {
            'full_name':           data['full_name'],
            'email':               data['email'],
            'phone':               data['phone'],
            'registration_number': data['registration_number'],
            'level':               data['level'],
            'speciality':          data['speciality'],
            'portfolio_link':      data.get('portfolio_link', ''),
        }

        html_content = get_registration_confirmation_html(data['full_name'], "registration", email_data)
        text_content = get_registration_confirmation_text(data['full_name'], "registration")
        send_email(data['email'], "BrainHack 2026 – Registration Received", html_content, text_content)

        return jsonify({"message": "Registration successful"}), 201

    except mysql.connector.IntegrityError:
        return jsonify({"error": "A registration with this email or student ID already exists."}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/registrations', methods=['GET'])
def get_registrations():
    ensure_status_column()
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT
                id,
                full_name,
                email,
                phone,
                registration_number,
                level,
                speciality,
                portfolio_link,
                status,
                created_at
            FROM registrations
            ORDER BY created_at DESC
        """)
        rows = cursor.fetchall()
        for row in rows:
            if row.get('created_at'):
                row['created_at'] = row['created_at'].isoformat()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/accept', methods=['POST'])
def accept_registration():
    data = request.get_json()
    reg_id = data.get('id')
    if not reg_id:
        return jsonify({"error": "ID required"}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT id, full_name, email, phone, registration_number,
                   level, speciality, portfolio_link, status, created_at
            FROM registrations WHERE id = %s
        """, (reg_id,))
        reg = cursor.fetchone()
        if not reg:
            return jsonify({"error": "Registration not found"}), 404

        cursor.execute("UPDATE registrations SET status = 'accepted' WHERE id = %s", (reg_id,))
        conn.commit()

        subject = "Congratulations! You've been accepted to BrainHack"
        html_content = get_registration_confirmation_html(reg['full_name'], "acceptance", reg)
        text_content = get_registration_confirmation_text(reg['full_name'], "acceptance")

        if send_email(reg['email'], subject, html_content, text_content):
            return jsonify({"message": "Registration accepted and email sent"})
        return jsonify({"message": "Registration accepted but email failed"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/reject', methods=['POST'])
def reject_registration():
    data = request.get_json()
    reg_id = data.get('id')
    if not reg_id:
        return jsonify({"error": "ID required"}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT id, full_name, email, phone, registration_number,
                   level, speciality, portfolio_link, status, created_at
            FROM registrations WHERE id = %s
        """, (reg_id,))
        reg = cursor.fetchone()
        if not reg:
            return jsonify({"error": "Registration not found"}), 404

        cursor.execute("UPDATE registrations SET status = 'rejected' WHERE id = %s", (reg_id,))
        conn.commit()

        subject = "BrainHack Registration Update"
        html_content = get_registration_confirmation_html(reg['full_name'], "rejection", reg)
        text_content = get_registration_confirmation_text(reg['full_name'], "rejection")

        if send_email(reg['email'], subject, html_content, text_content):
            return jsonify({"message": "Registration rejected and email sent"})
        return jsonify({"message": "Registration rejected but email failed"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()