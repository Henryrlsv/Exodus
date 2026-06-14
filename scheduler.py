"""
scheduler.py — Revisión automática por empresa
================================================
MODIFICADO en v2:
  - El scheduler puede ejecutarse por empresa individual
  - Referencia al socketio para emitir eventos
"""

import logging
import os
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_scheduler = None
_config    = {'enabled': False, 'interval_minutes': 30, 'last_run': None, 'next_run': None, 'runs_count': 0}
_app_ref   = None
_sio_ref   = None


def init_scheduler(app, socketio):
    global _scheduler, _app_ref, _sio_ref

    _app_ref = app
    _sio_ref = socketio

    # Inyectar socketio en fetch.py
    from routes.fetch import set_socketio
    set_socketio(socketio)

    _scheduler = BackgroundScheduler(daemon=True, timezone='UTC')
    _scheduler.start()

    auto_interval = int(os.getenv('AUTO_CHECK_INTERVAL', 0))
    if auto_interval > 0:
        update_scheduler_config(auto_interval, True)

    logger.info('Scheduler v2 iniciado')


def _task():
    """Tarea periódica: descarga JSON de todas las empresas de todos los usuarios activos."""
    from database import get_db, rows_to_list
    from routes.fetch_internal import fetch_for_company_internal

    logger.info(f'[Scheduler] Tarea automática — {datetime.now():%Y-%m-%d %H:%M}')

    with get_db(_app_ref) as conn:
        companies = rows_to_list(conn.execute(
            'SELECT c.* FROM companies c JOIN users u ON u.id = c.user_id WHERE u.is_active = 1'
        ).fetchall())

    for company in companies:
        try:
            fetch_for_company_internal(company, _app_ref, _sio_ref)
        except Exception as e:
            logger.error(f'[Scheduler] Error en empresa {company["id"]}: {e}')

    _config['last_run']   = datetime.now().isoformat()
    _config['runs_count'] += 1

    job = _scheduler.get_job('auto_fetch')
    if job and job.next_run_time:
        _config['next_run'] = job.next_run_time.isoformat()


def update_scheduler_config(interval_minutes: int, enabled: bool):
    if not _scheduler:
        return

    if _scheduler.get_job('auto_fetch'):
        _scheduler.remove_job('auto_fetch')

    _config['enabled']          = enabled
    _config['interval_minutes'] = interval_minutes

    if enabled and interval_minutes > 0:
        _scheduler.add_job(
            func=_task,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id='auto_fetch',
            replace_existing=True,
            misfire_grace_time=60,
        )
        job = _scheduler.get_job('auto_fetch')
        if job and job.next_run_time:
            _config['next_run'] = job.next_run_time.isoformat()
        logger.info(f'Scheduler activado: cada {interval_minutes} min')
    else:
        _config['next_run'] = None
        logger.info('Scheduler desactivado')


def get_scheduler_status() -> dict:
    return dict(_config)
