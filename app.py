"""
Smart Waste Reporting & Resolution System — Flask Backend
=========================================================
Provides session-based authentication, SQLite storage for tickets,
and image upload handling.  All frontend design is preserved as-is.
"""

import hashlib
import os
import secrets
import sqlite3
import uuid
from functools import wraps
from datetime import datetime

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify, flash, send_from_directory, abort
)
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

# ── App Configuration ────────────────────────────────────────────────────────
_base_dir = os.path.dirname(os.path.abspath(__file__))
# Vercel sets VERCEL=1; VERCEL_ENV is preview | development | production
_vercel = bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))

# Vercel CDN serves public/**; keep legacy ./static as fallback for local/Docker
_static_root = os.path.join(_base_dir, "public", "static")
if not os.path.isdir(_static_root):
    _static_root = os.path.join(_base_dir, "static")

_is_production = os.environ.get("FLASK_ENV", "").lower() == "production"
_debug = os.environ.get("FLASK_DEBUG", "0").strip().lower() in ("1", "true", "yes")
if _is_production:
    _debug = False

app = Flask(
    __name__,
    static_folder=_static_root,
    static_url_path="/static",
    template_folder=os.path.join(_base_dir, "templates"),
)
_secret = os.environ.get("SECRET_KEY")
if _is_production and not _secret:
    # Vercel Preview: random key per cold start (sessions fragile).
    _vercel_preview = _vercel and os.environ.get("VERCEL_ENV", "") != "production"
    if _vercel_preview:
        _secret = secrets.token_urlsafe(48)
    elif _vercel:
        # Production on Vercel without SECRET_KEY: deterministic per deployment so all
        # instances share one key (sessions work). Set SECRET_KEY in the dashboard for stronger security.
        _dep = (
            os.environ.get("VERCEL_DEPLOYMENT_ID")
            or os.environ.get("VERCEL_GIT_COMMIT_SHA")
            or "vercel"
        )
        _secret = hashlib.sha256(f"waste-app:{_dep}".encode()).hexdigest()
    else:
        raise RuntimeError(
            "SECRET_KEY must be set when FLASK_ENV=production. "
            "Vercel: Project → Settings → Environment Variables → add SECRET_KEY."
        )
app.secret_key = _secret or "dev-only-insecure-not-for-production"
app.config["DEBUG"] = _debug

_proxy_flag = os.environ.get("BEHIND_PROXY", "").strip().lower()
if _proxy_flag in ("0", "false", "no"):
    _use_proxy = False
elif _proxy_flag in ("1", "true", "yes"):
    _use_proxy = True
else:
    _use_proxy = _vercel

if _use_proxy:
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
    )

if os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes"):
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
elif os.environ.get("VERCEL_ENV") == "production":
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

if _vercel:
    UPLOAD_FOLDER = os.path.join("/tmp", "waste_uploads")
else:
    UPLOAD_FOLDER = os.path.join(app.static_folder, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

if os.environ.get("DATABASE_PATH"):
    DATABASE = os.environ["DATABASE_PATH"]
elif _vercel:
    DATABASE = os.path.join("/tmp", "waste.db")
else:
    DATABASE = os.path.join(_base_dir, "waste.db")

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}


# ── Database Helpers ─────────────────────────────────────────────────────────

def get_db():
    """Open a new database connection."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row          # dict-like rows
    # WAL creates -wal/-shm files; some serverless /tmp setups handle this poorly
    if not _vercel:
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create tables and seed demo users if they don't exist."""
    conn = get_db()
    cur = conn.cursor()

    # Users table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT    UNIQUE NOT NULL,
            password TEXT    NOT NULL,
            role     TEXT    NOT NULL CHECK(role IN ('citizen','admin'))
        )
    ''')

    # Tickets table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            area           TEXT    NOT NULL,
            days           INTEGER NOT NULL,
            description    TEXT    NOT NULL,
            image          TEXT,
            status         TEXT    NOT NULL DEFAULT 'Pending',
            estimated_time TEXT    NOT NULL,
            created_at     TEXT    NOT NULL,
            user_id        INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    # Seed demo accounts (password stored plain — demo only)
    demo_users = [
        ('citizen@demo.com', '123456', 'citizen'),
        ('admin@demo.com',   '123456', 'admin'),
    ]
    for uname, pwd, role in demo_users:
        cur.execute(
            'INSERT OR IGNORE INTO users (username, password, role) VALUES (?, ?, ?)',
            (uname, pwd, role)
        )

    conn.commit()
    conn.close()


# ── Auth Decorator ───────────────────────────────────────────────────────────

def login_required(f):
    """Redirect to login page if user is not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Restrict route to admin role only."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login_page'))
        if session.get('role') != 'admin':
            return redirect(url_for('citizen_page'))
        return f(*args, **kwargs)
    return decorated


# ── ETA Calculation ──────────────────────────────────────────────────────────

def calculate_eta(days):
    """Return (text, urgent_bool) based on days-unchecked value."""
    if days <= 1:
        return ('Within 24 hours', False)
    elif days == 2:
        return ('Within 12 hours', False)
    else:
        return ('Immediate action required', True)


def allowed_file(filename):
    """Check if the uploaded file has an allowed extension."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/uploads/<filename>")
def serve_upload(filename):
    """Serve user-uploaded images (Vercel: files live in /tmp, not in public/)."""
    if ".." in filename or "/" in filename or "\\" in filename:
        abort(404)
    return send_from_directory(UPLOAD_FOLDER, filename)


# ── Routes ───────────────────────────────────────────────────────────────────

# ── Login ─────────────────────────────────────

@app.route('/')
def login_page():
    """Render login page."""
    # If already logged in, redirect to their dashboard
    if 'user_id' in session:
        if session['role'] == 'admin':
            return redirect(url_for('admin_page'))
        return redirect(url_for('citizen_page'))
    return render_template('login.html', error=None)


@app.route('/login', methods=['POST'])
def login():
    """Authenticate user and create session."""
    email    = request.form.get('email', '').strip()
    password = request.form.get('password', '').strip()
    role     = request.form.get('role', '').strip()

    if not email or not password or not role:
        return render_template('login.html', error='Please fill in all fields and select a role.')

    conn = get_db()
    user = conn.execute(
        'SELECT * FROM users WHERE username = ? AND password = ? AND role = ?',
        (email, password, role)
    ).fetchone()
    conn.close()

    if not user:
        return render_template('login.html', error='Invalid credentials. Please try again.')

    # Create session
    session['user_id']  = user['id']
    session['username'] = user['username']
    session['role']     = user['role']

    if role == 'admin':
        return redirect(url_for('admin_page'))
    return redirect(url_for('citizen_page'))


@app.route('/logout')
def logout():
    """Clear session and redirect to login."""
    session.clear()
    return redirect(url_for('login_page'))


# ── Citizen ───────────────────────────────────

@app.route('/citizen')
@login_required
def citizen_page():
    """Render citizen dashboard with complaint form and history."""
    conn = get_db()
    tickets = conn.execute(
        'SELECT * FROM tickets WHERE user_id = ? ORDER BY id DESC',
        (session['user_id'],)
    ).fetchall()
    conn.close()

    # Convert to list of dicts for Jinja
    ticket_list = []
    for t in tickets:
        ticket_list.append({
            'id':             t['id'],
            'area':           t['area'],
            'days':           t['days'],
            'description':    t['description'],
            'image':          t['image'],
            'status':         t['status'],
            'estimated_time': t['estimated_time'],
            'created_at':     t['created_at'],
            'urgent':         t['days'] >= 3,
        })

    success = request.args.get('success')
    return render_template(
        'citizen.html',
        tickets=ticket_list,
        ticket_count=len(ticket_list),
        username=session.get('username', 'Citizen'),
        success=success
    )


@app.route('/submit', methods=['POST'])
@login_required
def submit_ticket():
    """Handle complaint form submission from citizen."""
    area = request.form.get('area', '').strip()
    days = request.form.get('days', '0').strip()
    desc = request.form.get('desc', '').strip()

    # Validate
    errors = []
    if not area:
        errors.append('Please select an area.')
    try:
        days = int(days)
        if days < 1:
            raise ValueError
    except ValueError:
        errors.append('Please enter a valid number of days.')
        days = 0
    if not desc:
        errors.append('Please describe the issue.')

    if errors:
        flash(' '.join(errors), 'error')
        return redirect(url_for('citizen_page'))

    # Handle image upload
    image_filename = None
    file = request.files.get('image')
    if file and file.filename and allowed_file(file.filename):
        ext = file.filename.rsplit('.', 1)[1].lower()
        image_filename = f"{uuid.uuid4().hex}.{ext}"
        file.save(os.path.join(UPLOAD_FOLDER, image_filename))

    # Calculate ETA
    eta_text, _ = calculate_eta(days)

    # Save to database
    conn = get_db()
    conn.execute(
        '''INSERT INTO tickets (area, days, description, image, status, estimated_time, created_at, user_id)
           VALUES (?, ?, ?, ?, 'Pending', ?, ?, ?)''',
        (area, days, desc, image_filename, eta_text,
         datetime.now().strftime('%Y-%m-%d %H:%M:%S'), session['user_id'])
    )
    conn.commit()

    # Get the ID just inserted
    ticket_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.close()

    return redirect(url_for('citizen_page', success=f'Ticket #{ticket_id} for {area} — ETA: {eta_text}'))


# ── Admin ─────────────────────────────────────

@app.route('/admin')
@admin_required
def admin_page():
    """Render admin dashboard with all tickets."""
    conn = get_db()
    tickets = conn.execute('SELECT * FROM tickets ORDER BY id DESC').fetchall()
    conn.close()

    ticket_list = []
    for t in tickets:
        ticket_list.append({
            'id':             t['id'],
            'area':           t['area'],
            'days':           t['days'],
            'description':    t['description'],
            'image':          t['image'],
            'status':         t['status'],
            'estimated_time': t['estimated_time'],
            'created_at':     t['created_at'],
            'urgent':         t['days'] >= 3,
        })

    stats = {
        'total':    len(ticket_list),
        'pending':  sum(1 for t in ticket_list if t['status'] == 'Pending'),
        'resolved': sum(1 for t in ticket_list if t['status'] == 'Resolved'),
        'urgent':   sum(1 for t in ticket_list if t['urgent'] and t['status'] == 'Pending'),
    }

    return render_template(
        'admin.html',
        tickets=ticket_list,
        stats=stats,
        username=session.get('username', 'Admin')
    )


@app.route('/resolve/<int:ticket_id>', methods=['POST'])
@admin_required
def resolve_ticket(ticket_id):
    """Mark a ticket as resolved."""
    conn = get_db()
    conn.execute('UPDATE tickets SET status = ? WHERE id = ?', ('Resolved', ticket_id))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_page'))


@app.route('/api/tickets')
@admin_required
def api_tickets():
    """JSON endpoint for admin AJAX polling."""
    conn = get_db()
    tickets = conn.execute('SELECT * FROM tickets ORDER BY id DESC').fetchall()
    conn.close()

    result = []
    for t in tickets:
        result.append({
            'id':             t['id'],
            'area':           t['area'],
            'days':           t['days'],
            'description':    t['description'],
            'image':          t['image'],
            'status':         t['status'],
            'estimated_time': t['estimated_time'],
            'created_at':     t['created_at'],
            'urgent':         t['days'] >= 3,
        })
    return jsonify(result)


# ── Run ──────────────────────────────────────────────────────────────────────

# Schema and seed run on import so Gunicorn/uWSGI and `flask run` get a ready DB.
init_db()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", "5000"))
    print(f" * Smart Waste Reporting System running at http://127.0.0.1:{port}")
    app.run(debug=_debug, host="127.0.0.1", port=port)
