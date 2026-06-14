"""
auth_helpers.py — Utilidades de autenticación
==============================================
NUEVO en v2:
  - Hash de contraseñas con bcrypt (werkzeug)
  - Cifrado de App Passwords con Fernet (cryptography)
  - Decorador login_required para proteger rutas
  - Funciones de sesión: login_user / logout_user / current_user
"""

import os
import base64
import logging
from functools import wraps
from flask import session, redirect, url_for, jsonify, request
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

# ─── Clave Fernet para cifrar App Passwords ──────────────────────────────────
# En producción esta clave debe estar en .env y nunca en el código.
_RAW_KEY = os.getenv('FERNET_KEY', '')

def _get_fernet():
    """Devuelve una instancia de Fernet, generando la clave si no existe."""
    from cryptography.fernet import Fernet
    global _RAW_KEY
    if not _RAW_KEY:
        # Primera ejecución: generar y advertir
        _RAW_KEY = Fernet.generate_key().decode()
        logger.warning(
            'FERNET_KEY no encontrada en .env — se generó una temporal. '
            'Las contraseñas cifradas NO sobrevivirán un reinicio. '
            'Agrega FERNET_KEY=%s al archivo .env', _RAW_KEY
        )
    key_bytes = _RAW_KEY.encode() if isinstance(_RAW_KEY, str) else _RAW_KEY
    # Fernet necesita exactamente 32 bytes en base64url
    if len(base64.urlsafe_b64decode(key_bytes + b'==')) != 32:
        key_bytes = base64.urlsafe_b64encode(key_bytes[:32].ljust(32, b'0'))
    return Fernet(key_bytes)


# ─── Contraseñas de usuario (bcrypt via werkzeug) ────────────────────────────

def hash_password(plain: str) -> str:
    return generate_password_hash(plain, method='pbkdf2:sha256', salt_length=16)


def verify_password(plain: str, hashed: str) -> bool:
    return check_password_hash(hashed, plain)


# ─── App Passwords de Gmail (cifrado simétrico Fernet) ───────────────────────

def encrypt_app_password(plain: str) -> str:
    """Cifra la contraseña de aplicación antes de guardarla en SQLite."""
    try:
        f = _get_fernet()
        return f.encrypt(plain.encode()).decode()
    except Exception as e:
        logger.error(f'Error cifrando app password: {e}')
        raise


def decrypt_app_password(token: str) -> str:
    """Descifra la contraseña de aplicación al momento de usarla."""
    try:
        f = _get_fernet()
        return f.decrypt(token.encode()).decode()
    except Exception as e:
        logger.error(f'Error descifrando app password: {e}')
        raise ValueError('No se pudo descifrar la contraseña de aplicación. '
                         'Puede que la FERNET_KEY haya cambiado.')


# ─── Gestión de sesión Flask ─────────────────────────────────────────────────

def login_user(user: dict):
    """Guarda datos mínimos del usuario en la sesión Flask."""
    session.permanent = True
    session['user_id']   = user['id']
    session['user_name'] = user['name']
    session['user_email']= user['email']


def logout_user():
    session.clear()


def current_user():
    """Devuelve dict con datos del usuario autenticado o None."""
    if 'user_id' not in session:
        return None
    return {
        'id':    session['user_id'],
        'name':  session['user_name'],
        'email': session['user_email'],
    }


# ─── Decoradores de protección de rutas ──────────────────────────────────────

def login_required(f):
    """
    Decorador para vistas HTML:
    redirige a /auth/login si no hay sesión activa.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user():
            return redirect(url_for('auth.login_page'))
        return f(*args, **kwargs)
    return decorated


def api_login_required(f):
    """
    Decorador para endpoints JSON:
    devuelve 401 si no hay sesión activa.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user():
            return jsonify({'success': False, 'message': 'No autenticado'}), 401
        return f(*args, **kwargs)
    return decorated
