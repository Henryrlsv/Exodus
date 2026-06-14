"""
imap_service.py v5 — Filtrado por NIT/NRC ANTES de guardar
===========================================================
CAMBIO CLAVE v5:
  - IMAPService recibe expected_nit y expected_nrc al construirse
  - _process_email filtra adjuntos JSON leyendo su contenido y buscando NIT/NRC
  - Solo descarga JSON cuyo contenido corresponda a esa empresa
  - Solo descarga el PDF del correo SI ya se validó al menos un JSON de esa empresa
  - Correos sin ningún JSON de la empresa → IGNORADOS completamente, sin tocar disco
  - _validate_nit en fetch.py ya no rechaza nada nuevo (todo llega pre-validado)
"""

import imaplib
import email
import json
import os
import re
import threading
import logging
import base64
from datetime import datetime, date, timedelta
from email.header import decode_header

logger = logging.getLogger(__name__)


# ─── Proveedores conocidos ────────────────────────────────────────────────────
IMAP_PROVIDERS = {
    'gmail.com':       {'host':'imap.gmail.com',          'port':993,'name':'Gmail'},
    'googlemail.com':  {'host':'imap.gmail.com',          'port':993,'name':'Gmail'},
    'yahoo.com':       {'host':'imap.mail.yahoo.com',     'port':993,'name':'Yahoo'},
    'yahoo.es':        {'host':'imap.mail.yahoo.com',     'port':993,'name':'Yahoo'},
    'yahoo.com.mx':    {'host':'imap.mail.yahoo.com',     'port':993,'name':'Yahoo'},
    'yahoo.co.uk':     {'host':'imap.mail.yahoo.com',     'port':993,'name':'Yahoo'},
    'yahoo.com.ar':    {'host':'imap.mail.yahoo.com',     'port':993,'name':'Yahoo'},
    'ymail.com':       {'host':'imap.mail.yahoo.com',     'port':993,'name':'Yahoo'},
    'rocketmail.com':  {'host':'imap.mail.yahoo.com',     'port':993,'name':'Yahoo'},
    'hotmail.com':     {'host':'outlook.office365.com',   'port':993,'name':'Hotmail'},
    'hotmail.es':      {'host':'outlook.office365.com',   'port':993,'name':'Hotmail'},
    'hotmail.com.mx':  {'host':'outlook.office365.com',   'port':993,'name':'Hotmail'},
    'hotmail.com.ar':  {'host':'outlook.office365.com',   'port':993,'name':'Hotmail'},
    'hotmail.co.uk':   {'host':'outlook.office365.com',   'port':993,'name':'Hotmail'},
    'outlook.com':     {'host':'outlook.office365.com',   'port':993,'name':'Outlook'},
    'outlook.es':      {'host':'outlook.office365.com',   'port':993,'name':'Outlook'},
    'outlook.com.mx':  {'host':'outlook.office365.com',   'port':993,'name':'Outlook'},
    'live.com':        {'host':'outlook.office365.com',   'port':993,'name':'Outlook'},
    'live.es':         {'host':'outlook.office365.com',   'port':993,'name':'Outlook'},
    'live.com.mx':     {'host':'outlook.office365.com',   'port':993,'name':'Outlook'},
    'msn.com':         {'host':'outlook.office365.com',   'port':993,'name':'Outlook'},
}

def detect_provider(email_address: str) -> dict:
    domain = email_address.lower().split('@')[-1] if '@' in email_address else ''
    return IMAP_PROVIDERS.get(domain, {'host': None, 'port': 993, 'name': 'Custom'})

def is_known_provider(email_address: str) -> bool:
    domain = email_address.lower().split('@')[-1] if '@' in email_address else ''
    return domain in IMAP_PROVIDERS

KNOWN_DOMAINS = set(IMAP_PROVIDERS.keys())


# ─── Utilidades de NIT/NRC ────────────────────────────────────────────────────

def _normalize(value: str) -> str:
    """Quita guiones, espacios y puntos para comparación flexible."""
    return re.sub(r'[\-\s\.]', '', str(value or '')).strip()


def _json_contains_identifier(data, nit: str, nrc: str) -> tuple[bool, str]:
    """
    Recorre el JSON buscando el NIT o NRC de la empresa.
    Retorna (encontrado, valor_encontrado).
    Busca en: nivel raíz, emisor, receptor, contribuyente, identificacion.
    """
    nit_norm = _normalize(nit)
    nrc_norm = _normalize(nrc)

    # Convertir todo el JSON a texto plano para búsqueda rápida
    try:
        full_text = _normalize(json.dumps(data, ensure_ascii=False))
    except Exception:
        full_text = ''

    # Búsqueda en texto completo primero (rápida)
    if nit_norm and nit_norm in full_text:
        return True, nit
    if nrc_norm and nrc_norm in full_text:
        return True, nrc

    return False, ''


def _extract_identifier_from_json(data: dict) -> tuple[str, str]:
    """
    Extrae NIT y NRC del contenido JSON.
    Retorna (nit_encontrado, nrc_encontrado).
    """
    if not isinstance(data, dict):
        return '', ''

    nit_keys = ('nit', 'NIT', 'Nit', 'numeroNit', 'numero_nit',
                 'nitEmisor', 'emisor_nit', 'nit_emisor', 'contribuyente_nit')
    nrc_keys = ('nrc', 'NRC', 'Nrc', 'numeroNrc', 'numero_nrc',
                 'nrcEmisor', 'emisor_nrc', 'nrc_emisor')

    sections = [data] + [
        data.get(s) for s in
        ('emisor', 'receptor', 'contribuyente', 'empresa',
         'Emisor', 'Receptor', 'Empresa', 'identificacion', 'Identificacion')
        if isinstance(data.get(s), dict)
    ]

    found_nit, found_nrc = '', ''
    for section in sections:
        if not isinstance(section, dict):
            continue
        for k in nit_keys:
            if k in section and section[k]:
                found_nit = str(section[k]).strip()
                break
        for k in nrc_keys:
            if k in section and section[k]:
                found_nrc = str(section[k]).strip()
                break
        if found_nit or found_nrc:
            break

    return found_nit, found_nrc


class IMAPService:
    """
    Servicio IMAP universal.
    Descarga solo JSON (y PDF relacionados) cuyo NIT o NRC
    corresponda a la empresa registrada.
    """

    def __init__(self, email_address: str, app_password: str,
                 download_folder: str = 'downloads',
                 cancel_event: threading.Event = None,
                 custom_host: str = None,
                 custom_port: int = 993,
                 oauth2_token: str = None,
                 expected_nit: str = '',
                 expected_nrc: str = ''):

        self.email_address   = email_address
        self.app_password    = app_password
        self.download_folder = download_folder
        self.cancel_event    = cancel_event or threading.Event()
        self.oauth2_token    = oauth2_token
        self.imap            = None
        self.is_connected    = False

        # NIT y NRC de la empresa — usados para filtrar ANTES de guardar
        self.expected_nit = expected_nit.strip()
        self.expected_nrc = expected_nrc.strip()

        detected = detect_provider(email_address)
        if custom_host and custom_host.strip():
            self.provider = {'host': custom_host.strip(), 'port': int(custom_port or 993), 'name': 'Custom'}
        elif detected['host']:
            self.provider = detected
        else:
            self.provider = {'host': None, 'port': 993, 'name': 'Custom'}

        logger.info(
            'IMAPService v5 init: %s → %s | NIT=%s | NRC=%s',
            email_address, self.provider['name'],
            self.expected_nit or '(sin filtro)', self.expected_nrc or '(sin filtro)'
        )

    # ─── Conexión ─────────────────────────────────────────────────────────────

    def connect(self) -> dict:
        if not self.provider.get('host'):
            return {
                'success': False,
                'message': 'Dominio de correo no reconocido. Ingresa el servidor IMAP manualmente.'
            }

        host = self.provider['host']
        port = self.provider['port']
        name = self.provider['name']

        try:
            logger.info('Conectando %s: %s:%s → %s', name, host, port, self.email_address)
            self.imap = imaplib.IMAP4_SSL(host, port)
            self.imap.socket().settimeout(45)

            if self.oauth2_token and name == 'Gmail':
                auth_string = f'user={self.email_address}\x01auth=Bearer {self.oauth2_token}\x01\x01'
                auth_bytes  = base64.b64encode(auth_string.encode()).decode()
                self.imap.authenticate('XOAUTH2', lambda x: auth_bytes)
            else:
                self.imap.login(self.email_address, self.app_password)

            self.is_connected = True
            logger.info('Conectado OK: %s / %s', name, self.email_address)
            return {'success': True, 'message': f'Conectado a {name} ({self.email_address})', 'provider': name}

        except imaplib.IMAP4.error as e:
            msg   = str(e)
            upper = msg.upper()
            logger.error('IMAP error [%s]: %s', name, msg)
            if any(k in upper for k in ('AUTHENTICATIONFAILED', 'INVALID CREDENTIALS',
                                         'LOGIN FAILED', '[AUTH]', 'AUTHENTICATE')):
                return {'success': False, 'message': f'Credenciales incorrectas. {self._auth_tips()}'}
            return {'success': False, 'message': f'Error IMAP: {msg}'}

        except (ConnectionRefusedError, OSError, TimeoutError) as e:
            logger.error('Conexión fallida %s: %s', host, e)
            return {'success': False,
                    'message': f'No se pudo conectar a {host}. Verifica tu conexión a internet.'}
        except Exception as e:
            logger.error('Error inesperado: %s', e, exc_info=True)
            return {'success': False, 'message': f'Error: {e}'}

    def disconnect(self):
        try:
            if self.imap and self.is_connected:
                self.imap.logout()
        except Exception:
            pass
        finally:
            self.is_connected = False

    def get_connection_info(self) -> dict:
        return {'connected': self.is_connected, 'email': self.email_address,
                'provider': self.provider.get('name', '?')}

    # ─── Descarga principal ───────────────────────────────────────────────────

    def fetch_emails_with_json(self, date_from=None, date_to=None,
                               include_read=True, progress_callback=None) -> list:
        """
        Busca correos y descarga solo JSON+PDF que pertenezcan a la empresa
        identificada por NIT/NRC. Todo lo demás es ignorado.
        """
        results = []
        if not self.is_connected:
            logger.error('fetch_emails llamado sin conexión')
            return results

        try:
            self.imap.select('INBOX', readonly=False)
            criteria = self._build_criteria(date_from, date_to, include_read)
            logger.info('SEARCH criteria: %r', criteria)

            status, msg_ids = self.imap.search(None, criteria)
            if status != 'OK':
                logger.error('SEARCH falló status=%s', status)
                return results

            raw_ids = msg_ids[0].split()
            total   = len(raw_ids)
            logger.info('%d correos encontrados', total)

            if not raw_ids:
                return results

            if progress_callback:
                progress_callback(0, total, 'found', {'total': total, 'email': self.email_address})

            for idx, eid in enumerate(raw_ids):
                if self.cancel_event.is_set():
                    logger.info('Cancelado por usuario')
                    break

                if progress_callback:
                    progress_callback(idx + 1, total, 'processing',
                                      {'email': self.email_address, 'current': idx+1, 'total': total})

                try:
                    msg_results = self._process_email(eid)
                    results.extend(msg_results)
                    if msg_results:
                        logger.info('Email %s: %d archivos guardados', eid, len(msg_results))
                except Exception as e:
                    logger.error('Error en correo %s: %s', eid, e, exc_info=True)

        except Exception as e:
            logger.error('Error general en fetch: %s', e, exc_info=True)
            if not self.cancel_event.is_set():
                raise

        return results

    # ─── Procesamiento de un correo ───────────────────────────────────────────

    def _process_email(self, eid: bytes) -> list:
        """
        FILTRO PRINCIPAL:
        1. Descarga el correo completo
        2. Recorre los adjuntos y carga en memoria los JSON
        3. Valida si algún JSON contiene el NIT o NRC de la empresa
        4. Si sí: guarda ese JSON + PDF del mismo correo
        5. Si no: ignora completamente — no toca el disco
        """
        results = []

        st, data = self.imap.fetch(eid, '(RFC822)')
        if st != 'OK' or not data or data[0] is None:
            return results

        raw = data[0][1]
        if not isinstance(raw, bytes):
            return results

        msg      = email.message_from_bytes(raw)
        subject  = self._decode_str(msg.get('Subject', ''))
        sender   = self._decode_str(msg.get('From', ''))
        date_hdr = msg.get('Date', '')

        # ── Recolectar adjuntos en memoria (sin tocar disco aún) ──────────────
        json_candidates = []   # lista de (filename, bytes, parsed_dict)
        pdf_candidates  = []   # lista de (filename, bytes)

        for part in msg.walk():
            if part.get_content_maintype() == 'multipart':
                continue

            fname = self._decode_str(part.get_filename() or '')
            ctype = part.get_content_type()

            is_json = (fname and fname.lower().endswith('.json')) or \
                      ctype in ('application/json', 'text/json')
            is_pdf  = (fname and fname.lower().endswith('.pdf')) or \
                      ctype == 'application/pdf'

            if not (is_json or is_pdf):
                continue

            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                if not fname:
                    fname = 'adjunto.json' if is_json else 'adjunto.pdf'

                if is_json:
                    # Intentar parsear para validar NIT/NRC
                    try:
                        text   = self._decode_bytes(payload)
                        parsed = json.loads(text)
                        json_candidates.append((fname, payload, parsed))
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        logger.warning('JSON inválido %s en correo: %s', fname, e)
                        # Guardar como error para que quede registro
                        json_candidates.append((fname, payload, None))

                elif is_pdf:
                    pdf_candidates.append((fname, payload))

            except Exception as e:
                logger.warning('Error leyendo parte %s: %s', fname, e)

        if not json_candidates:
            # Sin JSON → no nos interesa este correo
            return results

        # ── Validar NIT/NRC en los JSON ───────────────────────────────────────
        matched_jsons = []   # JSON que pertenecen a esta empresa
        rejected_jsons = []  # JSON con NIT/NRC de otra empresa
        invalid_jsons  = []  # JSON malformados

        for fname, payload, parsed in json_candidates:
            if parsed is None:
                invalid_jsons.append((fname, payload))
                continue

            belongs, identifier = _json_contains_identifier(
                parsed, self.expected_nit, self.expected_nrc
            )

            if belongs:
                logger.info(
                    '✔ JSON "%s" pertenece a la empresa (NIT/NRC encontrado: %s)',
                    fname, identifier
                )
                matched_jsons.append((fname, payload, parsed))
            else:
                # Extraer el NIT/NRC real del JSON para el log
                found_nit, found_nrc = _extract_identifier_from_json(parsed)
                logger.info(
                    '✘ JSON "%s" NO pertenece a esta empresa → ignorado | '
                    'Encontrado NIT=%s NRC=%s | Esperado NIT=%s NRC=%s',
                    fname, found_nit or '?', found_nrc or '?',
                    self.expected_nit, self.expected_nrc
                )
                rejected_jsons.append(fname)

        # Si ningún JSON corresponde a esta empresa → ignorar TODO el correo
        if not matched_jsons and not invalid_jsons:
            if rejected_jsons:
                logger.info(
                    'Correo "%s" ignorado: %d JSON(s) de otra empresa → %s',
                    subject, len(rejected_jsons), ', '.join(rejected_jsons)
                )
            return results

        # ── Guardar los JSON que sí corresponden ──────────────────────────────
        for fname, payload, parsed in matched_jsons:
            r = self._save_json(fname, payload, parsed, sender, subject, date_hdr)
            results.append(r)

        # ── Guardar JSON inválidos (con registro de error) ────────────────────
        for fname, payload in invalid_jsons:
            r = self._error_result(fname, 'JSON inválido o malformado',
                                   sender, subject, date_hdr,
                                   datetime.now().isoformat(), len(payload))
            results.append(r)

        # ── Guardar PDFs SOLO si hubo al menos un JSON válido de esta empresa ─
        if matched_jsons and pdf_candidates:
            for fname, payload in pdf_candidates:
                r = self._save_pdf(fname, payload, sender, subject, date_hdr)
                results.append(r)

        return results

    # ─── Guardar archivos ─────────────────────────────────────────────────────

    def _save_json(self, filename: str, data: bytes, parsed,
                   sender: str, subject: str, date_hdr: str) -> dict:
        ts       = datetime.now().isoformat()
        safename = self._safe_filename(filename, '.json')
        filepath = self._unique_path(safename)

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(parsed, f, ensure_ascii=False, indent=2)

            size = os.path.getsize(filepath)
            logger.info('JSON guardado: %s (%d bytes)', safename, size)

            found_nit, found_nrc = _extract_identifier_from_json(parsed)

            return {
                'filename': safename, 'original_filename': filename,
                'file_type': 'json',
                'status': 'success',
                'message': f'OK — {self._json_info(parsed)}',
                'sender': sender, 'subject': subject, 'date_received': date_hdr,
                'file_size': size, 'filepath': filepath, 'timestamp': ts,
                'nit_found': found_nit,
                'nit_match': 1,   # Ya validado antes de guardar
            }
        except Exception as e:
            logger.error('Error guardando JSON %s: %s', filename, e, exc_info=True)
            return self._error_result(filename, str(e), sender, subject, date_hdr, ts, 0)

    def _save_pdf(self, filename: str, data: bytes,
                  sender: str, subject: str, date_hdr: str) -> dict:
        ts       = datetime.now().isoformat()
        safename = self._safe_filename(filename, '.pdf')
        filepath = self._unique_path(safename)

        try:
            with open(filepath, 'wb') as f:
                f.write(data)
            size = os.path.getsize(filepath)
            logger.info('PDF guardado: %s (%d bytes)', safename, size)
            return {
                'filename': safename, 'original_filename': filename,
                'file_type': 'pdf',
                'status': 'success', 'message': f'PDF guardado ({size//1024} KB)',
                'sender': sender, 'subject': subject, 'date_received': date_hdr,
                'file_size': size, 'filepath': filepath, 'timestamp': ts,
                'nit_found': None, 'nit_match': 1,
            }
        except Exception as e:
            logger.error('Error guardando PDF %s: %s', filename, e, exc_info=True)
            return self._error_result(filename, str(e), sender, subject, date_hdr, ts, 0)

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _build_criteria(self, date_from, date_to, include_read: bool) -> str:
        parts = []
        if not include_read:
            parts.append('UNSEEN')
        if date_from:
            parts.append(f'SINCE {self._imap_date(date_from)}')
        if date_to:
            parts.append(f'BEFORE {self._imap_date(date_to + timedelta(days=1))}')
        return ' '.join(parts) if parts else 'ALL'

    @staticmethod
    def _imap_date(d: date) -> str:
        months = ['Jan','Feb','Mar','Apr','May','Jun',
                  'Jul','Aug','Sep','Oct','Nov','Dec']
        return f'{d.day:02d}-{months[d.month-1]}-{d.year}'

    def _decode_str(self, value: str) -> str:
        if not value:
            return ''
        try:
            parts = decode_header(value)
            out = ''
            for part, enc in parts:
                if isinstance(part, bytes):
                    out += part.decode(enc or 'utf-8', errors='replace')
                else:
                    out += str(part)
            return out.strip()
        except Exception:
            return str(value).strip()

    def _decode_bytes(self, data: bytes) -> str:
        for enc in ('utf-8', 'utf-8-sig', 'latin-1', 'cp1252', 'iso-8859-1'):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return data.decode('utf-8', errors='replace')

    def _safe_filename(self, name: str, default_ext: str = '') -> str:
        if not name:
            return f'adjunto{default_ext}'
        for ch in r'<>:"/\|?*':
            name = name.replace(ch, '_')
        name = name.strip('. ') or f'adjunto{default_ext}'
        return name

    def _unique_path(self, filename: str) -> str:
        filepath = os.path.join(self.download_folder, filename)
        if not os.path.exists(filepath):
            return filepath
        base, ext = os.path.splitext(filename)
        i = 1
        while os.path.exists(filepath):
            filepath = os.path.join(self.download_folder, f'{base}_{i}{ext}')
            i += 1
        return filepath

    def _json_info(self, data) -> str:
        if isinstance(data, dict):
            return f'objeto {len(data)} claves'
        if isinstance(data, list):
            return f'array {len(data)} items'
        return type(data).__name__

    def _error_result(self, filename, message, sender, subject, date_hdr, ts, size):
        return {
            'filename': filename, 'original_filename': filename,
            'file_type': 'error',
            'status': 'error', 'message': message,
            'sender': sender, 'subject': subject, 'date_received': date_hdr,
            'file_size': size, 'filepath': '', 'timestamp': ts,
            'nit_found': None, 'nit_match': 0,
        }

    def _auth_tips(self) -> str:
        tips = {
            'Gmail':   'Usa una App Password de Google: myaccount.google.com/apppasswords',
            'Yahoo':   'Usa una App Password de Yahoo: login.yahoo.com/account/security',
            'Hotmail': 'Con MFA activo genera App Password en: account.microsoft.com/security',
            'Outlook': 'Con MFA activo genera App Password en: account.microsoft.com/security',
        }
        return tips.get(self.provider.get('name', ''), '')


# Alias de compatibilidad
GmailService = IMAPService
