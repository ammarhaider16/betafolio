"""Transactional account mail and consented briefings; never operator-selected recipients."""
import asyncio
from email.message import EmailMessage
from email.utils import formataddr
import smtplib
import ssl
import httpx
from .production import settings


async def send_mail(database, recipient, subject, text, html=None, *, idempotency_key=None):
    cfg = settings(database).email
    if not cfg.sender:
        raise ValueError('Email sender is not configured')
    if cfg.transport == 'resend':
        key = database.credentials.get('email_api_key')
        if not key:
            raise ValueError('Email credential is not configured')
        headers = {'Authorization': 'Bearer '+key}
        if idempotency_key:
            headers['Idempotency-Key'] = idempotency_key
        payload = {'from': formataddr((cfg.sender_name, cfg.sender)), 'to': [recipient],
                   'subject': subject, 'text': text}
        if html:
            payload['html'] = html
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            response = await client.post('https://api.resend.com/emails', headers=headers, json=payload)
            response.raise_for_status()
        return
    def smtp():
        message = EmailMessage()
        message['From'] = formataddr((cfg.sender_name, cfg.sender))
        message['To'], message['Subject'] = recipient, subject
        message.set_content(text)
        if html:
            message.add_alternative(html, subtype='html')
        context = ssl.create_default_context()
        if cfg.smtp_tls == 'implicit':
            connection = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=20, context=context)
        else:
            connection = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20)
            connection.starttls(context=context)
        with connection:
            if cfg.smtp_username:
                connection.login(cfg.smtp_username, database.credentials.get('smtp_password') or '')
            connection.send_message(message)
    await asyncio.to_thread(smtp)
