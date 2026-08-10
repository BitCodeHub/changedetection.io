"""
SQLite persistence for the control plane: accounts, tenants, subscriptions.

Deliberately dependency-free (stdlib sqlite3) so the control plane runs anywhere.
One account has one tenant (its isolated changedetection.io instance) and one
subscription row tracking the current plan + Stripe linkage.
"""
import os
import sqlite3
import secrets
import hashlib
import hmac
import time

DB_PATH = os.getenv("SAAS_DB_PATH", os.path.join(os.path.dirname(__file__), "saas.db"))


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT UNIQUE NOT NULL,
                pw_hash       TEXT NOT NULL,
                pw_salt       TEXT NOT NULL,
                stripe_customer_id TEXT,
                created_at    INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tenants (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id    INTEGER NOT NULL UNIQUE,
                slug          TEXT UNIQUE NOT NULL,
                container_id  TEXT,
                internal_url  TEXT,
                public_url    TEXT,
                status        TEXT NOT NULL DEFAULT 'pending',
                created_at    INTEGER NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                account_id            INTEGER PRIMARY KEY,
                plan                  TEXT NOT NULL DEFAULT 'free',
                status                TEXT NOT NULL DEFAULT 'active',
                stripe_subscription_id TEXT,
                current_period_end    INTEGER,
                updated_at            INTEGER NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );
            """
        )


# ── password hashing (stdlib scrypt) ──────────────────────────────────────────
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.scrypt(password.encode(), salt=salt.encode(), n=2**14, r=8, p=1, dklen=32)
    return dk.hex(), salt


def verify_password(password, pw_hash, salt):
    calc, _ = hash_password(password, salt)
    return hmac.compare_digest(calc, pw_hash)


# ── accounts ──────────────────────────────────────────────────────────────────
def create_account(email, password):
    pw_hash, salt = hash_password(password)
    now = int(time.time())
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO accounts (email, pw_hash, pw_salt, created_at) VALUES (?,?,?,?)",
            (email.lower().strip(), pw_hash, salt, now),
        )
        account_id = cur.lastrowid
        c.execute(
            "INSERT INTO subscriptions (account_id, plan, status, updated_at) VALUES (?, 'free', 'active', ?)",
            (account_id, now),
        )
    return account_id


def get_account_by_email(email):
    with _conn() as c:
        r = c.execute("SELECT * FROM accounts WHERE email=?", (email.lower().strip(),)).fetchone()
        return dict(r) if r else None


def get_account(account_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return dict(r) if r else None


def set_stripe_customer(account_id, customer_id):
    with _conn() as c:
        c.execute("UPDATE accounts SET stripe_customer_id=? WHERE id=?", (customer_id, account_id))


def account_by_stripe_customer(customer_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM accounts WHERE stripe_customer_id=?", (customer_id,)).fetchone()
        return dict(r) if r else None


# ── subscriptions ─────────────────────────────────────────────────────────────
def get_subscription(account_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM subscriptions WHERE account_id=?", (account_id,)).fetchone()
        return dict(r) if r else None


def set_subscription(account_id, plan=None, status=None, stripe_subscription_id=None, current_period_end=None):
    cur = get_subscription(account_id) or {}
    with _conn() as c:
        c.execute(
            """UPDATE subscriptions
               SET plan=?, status=?, stripe_subscription_id=?, current_period_end=?, updated_at=?
               WHERE account_id=?""",
            (
                plan if plan is not None else cur.get("plan", "free"),
                status if status is not None else cur.get("status", "active"),
                stripe_subscription_id if stripe_subscription_id is not None else cur.get("stripe_subscription_id"),
                current_period_end if current_period_end is not None else cur.get("current_period_end"),
                int(time.time()),
                account_id,
            ),
        )


# ── tenants ───────────────────────────────────────────────────────────────────
def create_tenant(account_id, slug):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO tenants (account_id, slug, status, created_at) VALUES (?,?,?,?)",
            (account_id, slug, "pending", int(time.time())),
        )
        return cur.lastrowid


def get_tenant(account_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM tenants WHERE account_id=?", (account_id,)).fetchone()
        return dict(r) if r else None


def update_tenant(account_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE tenants SET {cols} WHERE account_id=?", (*fields.values(), account_id))
