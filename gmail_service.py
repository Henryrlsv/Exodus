"""gmail_service.py — Alias de compatibilidad → usa imap_service"""
from imap_service import IMAPService as GmailService, IMAPService, detect_provider
__all__ = ['GmailService', 'IMAPService', 'detect_provider']
