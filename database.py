"""
database.py v5 — PostgreSQL (producción) + SQLite (desarrollo local)
======================================================================
- En producción (Render): usa DATABASE_URL con psycopg2
- En local: usa SQLite como antes
- API compatible: get_db(), row_to_dict(), rows_to_list()
"""

import os
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv('DATABASE_URL', '')
USE_POSTGRES  = bool(DATABASE_URL)

# ─── Esquema compartido (adaptado para ambas BD) ──────────────────────────────

SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS users (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL UNIQUE,
    password   TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    is_active  INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS companies (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    nit        TEXT NOT NULL,
    nrc        TEXT,
    address    TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS email_accounts (
    id           SERIAL PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    email        TEXT NOT NULL,
    app_password TEXT NOT NULL,
    label        TEXT DEFAULT '',
    is_active    INTEGER DEFAULT 1,
    last_check   TIMESTAMP,
    custom_host  TEXT,
    custom_port  INTEGER DEFAULT 993,
    created_at   TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS downloads (
    id                SERIAL PRIMARY KEY,
    company_id        INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    email_account_id  INTEGER REFERENCES email_accounts(id),
    filename          TEXT NOT NULL,
    original_filename TEXT,
    status            TEXT NOT NULL,
    message           TEXT,
    sender            TEXT,
    subject           TEXT,
    date_received     TEXT,
    file_size         INTEGER DEFAULT 0,
    filepath          TEXT,
    nit_found         TEXT,
    nit_match         INTEGER DEFAULT 0,
    timestamp         TEXT NOT NULL,
    created_at        TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_companies_user    ON companies(user_id);
CREATE INDEX IF NOT EXISTS idx_emails_company    ON email_accounts(company_id);
CREATE INDEX IF NOT EXISTS idx_downloads_company ON downloads(company_id);
CREATE INDEX IF NOT EXISTS idx_downloads_status  ON downloads(status);
"""

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL UNIQUE,
    password   TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    is_active  INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS companies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    nit        TEXT NOT NULL,
    nrc        TEXT,
    address    TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS email_accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    email        TEXT NOT NULL,
    app_password TEXT NOT NULL,
    label        TEXT DEFAULT '',
    is_active    INTEGER DEFAULT 1,
    last_check   TEXT,
    custom_host  TEXT,
    custom_port  INTEGER DEFAULT 993,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS downloads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id       INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    email_account_id INTEGER REFERENCES email_accounts(id),
    filename         TEXT NOT NULL,
    original_filename TEXT,
    status           TEXT NOT NULL,
    message          TEXT,
    sender           TEXT,
    subject          TEXT,
    date_received    TEXT,
    file_size        INTEGER DEFAULT 0,
    filepath         TEXT,
    nit_found        TEXT,
    nit_match        INTEGER DEFAULT 0,
    timestamp        TEXT NOT NULL,
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_companies_user    ON companies(user_id);
CREATE INDEX IF NOT EXISTS idx_emails_company    ON email_accounts(company_id);
CREATE INDEX IF NOT EXISTS idx_downloads_company ON downloads(company_id);
CREATE INDEX IF NOT EXISTS idx_downloads_status  ON downloads(status);
"""


# ─── Inicialización ───────────────────────────────────────────────────────────

def init_db(app=None):
    if USE_POSTGRES:
        _init_postgres()
    else:
        _init_sqlite(app)


def _init_postgres():
    import psycopg2
    url = _fix_postgres_url(DATABASE_URL)
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()
    # Ejecutar sentencias una por una
    for stmt in [s.strip() for s in SCHEMA_POSTGRES.split(';') if s.strip()]:
        try:
            cur.execute(stmt)
        except Exception as e:
            logger.warning('Schema stmt warning: %s', e)
    cur.close()
    conn.close()
    logger.info('PostgreSQL: tablas listas')


def _init_sqlite(app=None):
    import sqlite3
    path = _sqlite_path(app)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQLITE)
    conn.commit()
    conn.close()
    logger.info('SQLite: base de datos lista en %s', path)


# ─── Context manager get_db() ─────────────────────────────────────────────────

@contextmanager
def get_db(app=None):
    if USE_POSTGRES:
        yield from _pg_conn_adapted()
    else:
        yield from _sqlite_conn(app)


@contextmanager
def _pg_conn():
    import psycopg2
    import psycopg2.extras
    url  = _fix_postgres_url(DATABASE_URL)
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def _sqlite_conn(app=None):
    import sqlite3
    path = _sqlite_path(app)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─── Helpers de fila ──────────────────────────────────────────────────────────

def row_to_dict(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return dict(row)   # sqlite3.Row también soporta dict()


def rows_to_list(rows):
    return [dict(r) for r in rows] if rows else []


# ─── Utilidades internas ──────────────────────────────────────────────────────

def _sqlite_path(app=None):
    if app:
        return app.config.get('DATABASE', 'data/app.db')
    return os.getenv('DATABASE', 'data/app.db')


def _fix_postgres_url(url: str) -> str:
    """
    Render entrega 'postgres://...' pero psycopg2 necesita 'postgresql://'.
    """
    if url.startswith('postgres://'):
        return url.replace('postgres://', 'postgresql://', 1)
    return url


def placeholder(n: int = 1) -> str:
    """
    Devuelve el placeholder correcto según la BD:
      PostgreSQL → %s
      SQLite     → ?
    Útil para queries con un parámetro.
    Para múltiples: use placeholders(n).
    """
    return '%s' if USE_POSTGRES else '?'


def placeholders(values: list) -> str:
    """
    Genera la cadena de placeholders para una lista de valores.
    PostgreSQL → '%s, %s, %s'
    SQLite     → '?, ?, ?'
    """
    p = '%s' if USE_POSTGRES else '?'
    return ', '.join([p] * len(values))


def now_sql() -> str:
    """Expresión SQL para la fecha/hora actual según BD."""
    return 'NOW()' if USE_POSTGRES else "datetime('now')"


# ─── Adaptador de cursor universal ───────────────────────────────────────────
#
# Problema: SQLite usa ? como placeholder, PostgreSQL usa %s.
# Solución: un wrapper que convierte ? → %s automáticamente en PostgreSQL,
# así las rutas pueden seguir usando ? sin cambios.
#

class _AdaptedConn:
    """
    Envuelve una conexión psycopg2 y convierte ? → %s en todas las queries,
    exponiendo la misma API que sqlite3.Connection.
    """
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        cur = self._conn.cursor()
        cur.execute(sql.replace('?', '%s'), params or ())
        return _AdaptedCursor(cur)

    def executescript(self, script):
        cur = self._conn.cursor()
        for stmt in script.split(';'):
            s = stmt.strip()
            if s:
                cur.execute(s)

    def commit(self):   self._conn.commit()
    def rollback(self): self._conn.rollback()
    def close(self):    self._conn.close()

    def __enter__(self): return self
    def __exit__(self, *a): self._conn.__exit__(*a)


class _AdaptedCursor:
    """Cursor psycopg2 que devuelve filas como dict (como sqlite3.Row)."""
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._cur.description]
        return dict(zip(cols, row))

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._cur.description]
        return [dict(zip(cols, r)) for r in rows]

    @property
    def lastrowid(self):
        # psycopg2: usar RETURNING o lastval()
        try:
            self._cur.execute('SELECT lastval()')
            return self._cur.fetchone()[0]
        except Exception:
            return None

    def __iter__(self):
        return iter(self.fetchall())


# Reemplazar _pg_conn para usar el adaptador
import contextlib as _cl

@_cl.contextmanager
def _pg_conn_adapted():
    import psycopg2
    url  = _fix_postgres_url(DATABASE_URL)
    conn = psycopg2.connect(url)
    adapted = _AdaptedConn(conn)
    try:
        yield adapted
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute_insert(conn, sql: str, params: tuple = ()):
    """
    Ejecuta un INSERT y devuelve el id del registro creado.
    En PostgreSQL agrega RETURNING id automáticamente.
    En SQLite usa lastrowid del cursor.
    """
    if USE_POSTGRES:
        # Agregar RETURNING id al final del INSERT
        sql_pg = sql.rstrip().rstrip(')') 
        # En realidad: agregar RETURNING id después del INSERT completo
        if 'RETURNING' not in sql.upper():
            sql = sql.rstrip() + ' RETURNING id'
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        if isinstance(row, dict):
            return row.get('id')
        return row[0] if row else None
    else:
        cur = conn.execute(sql, params)
        return cur.lastrowid
