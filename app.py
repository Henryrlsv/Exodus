"""
app.py v5.1 — Compatible con Python 3.13 + Railway
====================================================
- Cambiado async_mode: eventlet → gevent
- Gevent es compatible con Python 3.13
"""

import os
import logging
from flask import Flask
from flask_socketio import SocketIO
from dotenv import load_dotenv

load_dotenv()

IS_PRODUCTION = os.getenv('RENDER', '') != ''

handlers = [logging.StreamHandler()]
if not IS_PRODUCTION:
    os.makedirs('logs', exist_ok=True)
    handlers.append(logging.FileHandler('logs/app.log', encoding='utf-8'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    handlers=handlers
)
logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)

    app.config.update(
        SECRET_KEY=os.getenv('SECRET_KEY', 'dev-secret-key-CHANGE-IN-PRODUCTION'),
        DOWNLOAD_FOLDER=os.getenv('DOWNLOAD_FOLDER', 'downloads'),
        DATABASE=os.getenv('DATABASE', 'data/app.db'),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=IS_PRODUCTION,
        PERMANENT_SESSION_LIFETIME=86400 * 7,
        SESSION_REFRESH_EACH_REQUEST=True,
    )

    for folder in [app.config['DOWNLOAD_FOLDER'], 'data']:
        os.makedirs(folder, exist_ok=True)
    if not IS_PRODUCTION:
        os.makedirs('logs', exist_ok=True)

    _init_fernet_key()

    from database import init_db
    with app.app_context():
        init_db(app)

    socketio = SocketIO(
        app,
        cors_allowed_origins='*',
        async_mode='gevent',        # Cambiado: eventlet → gevent (compatible Python 3.13)
        manage_session=False,
        logger=False,
        engineio_logger=False,
    )

    from routes.auth    import auth_bp
    from routes.company import company_bp
    from routes.email   import email_bp
    from routes.fetch   import fetch_bp
    from routes.files   import files_bp
    from routes.main    import main_bp

    app.register_blueprint(auth_bp,    url_prefix='/auth')
    app.register_blueprint(company_bp, url_prefix='/api/companies')
    app.register_blueprint(email_bp,   url_prefix='/api/emails')
    app.register_blueprint(fetch_bp,   url_prefix='/api/fetch')
    app.register_blueprint(files_bp,   url_prefix='/api/files')
    app.register_blueprint(main_bp)

    from scheduler import init_scheduler
    init_scheduler(app, socketio)

    env_label = 'PRODUCCIÓN (Railway)' if IS_PRODUCTION else 'DESARROLLO (local)'
    logger.info('InboxDTE v5.1 iniciado — %s', env_label)
    return app, socketio


def _init_fernet_key():
    from cryptography.fernet import Fernet
    import auth_helpers

    raw = os.getenv('FERNET_KEY', '').strip()
    if not raw:
        new_key = Fernet.generate_key().decode()
        auth_helpers._RAW_KEY = new_key
        logger.warning(
            'FERNET_KEY no encontrada. Clave temporal: %s — agrégala a las variables de entorno.',
            new_key
        )
    else:
        auth_helpers._RAW_KEY = raw
        logger.info('FERNET_KEY cargada correctamente')


if __name__ == '__main__':
    app, socketio = create_app()
    port  = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    logger.info('Servidor en http://localhost:%d', port)
    socketio.run(app, host='0.0.0.0', port=port, debug=debug, allow_unsafe_werkzeug=True)
