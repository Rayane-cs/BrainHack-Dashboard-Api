from flask import Flask, request, jsonify
from flask_cors import CORS
import mysql.connector.pooling
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
from email_templates import get_registration_confirmation_html, get_registration_confirmation_text

app = Flask(__name__)
CORS(app)

# Database connection pool
db_config = {
    "host": os.getenv("DB_HOST"),
    "port": int(os.getenv("DB_PORT")),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME"),
    "pool_name": "mypool",
    "pool_size": 5
}

db_pool = mysql.connector.pooling.MySQLConnectionPool(**db_config)

# SMTP settings
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")

def get_db_connection():
    return db_pool.get_connection()

def send_email(to_email, subject, html_content, text_content):
    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = SMTP_USER
        msg['To'] = to_email
        msg['Subject'] = subject

        part1 = MIMEText(text_content, 'plain')
        part2 = MIMEText(html_content, 'html')

        msg.attach(part1)
        msg.attach(part2)

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
            ALTER TABLE registrations
            ADD COLUMN IF NOT EXISTS status ENUM('pending', 'accepted', 'rejected') DEFAULT 'pending'
        """)
        conn.commit()
    except Exception as e:
        print(f"Error ensuring status column: {e}")
    finally:
        cursor.close()
        conn.close()

@app.route('/api/registrations', methods=['GET'])
def get_registrations():
    ensure_status_column()
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, full_name, email, phone, registration_number, level, speciality, portfolio_link, status FROM registrations ORDER BY created_at DESC")
        registrations = cursor.fetchall()
        return jsonify(registrations)
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
        # Get registration details
        cursor.execute("SELECT * FROM registrations WHERE id = %s", (reg_id,))
        reg = cursor.fetchone()
        if not reg:
            return jsonify({"error": "Registration not found"}), 404

        # Update status
        cursor.execute("UPDATE registrations SET status = 'accepted' WHERE id = %s", (reg_id,))
        conn.commit()

        # Send acceptance email
        subject = "Congratulations! You've been accepted to BrainHack"
        html_content = get_registration_confirmation_html(reg['full_name'], "acceptance")
        text_content = get_registration_confirmation_text(reg['full_name'], "acceptance")

        if send_email(reg['email'], subject, html_content, text_content):
            return jsonify({"message": "Registration accepted and email sent"})
        else:
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
        # Get registration details
        cursor.execute("SELECT * FROM registrations WHERE id = %s", (reg_id,))
        reg = cursor.fetchone()
        if not reg:
            return jsonify({"error": "Registration not found"}), 404

        # Update status
        cursor.execute("UPDATE registrations SET status = 'rejected' WHERE id = %s", (reg_id,))
        conn.commit()

        # Send rejection email
        subject = "BrainHack Registration Update"
        html_content = get_registration_confirmation_html(reg['full_name'], "rejection")
        text_content = get_registration_confirmation_text(reg['full_name'], "rejection")

        if send_email(reg['email'], subject, html_content, text_content):
            return jsonify({"message": "Registration rejected and email sent"})
        else:
            return jsonify({"message": "Registration rejected but email failed"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    ensure_status_column()
    app.run(debug=True, host='0.0.0.0', port=5000)