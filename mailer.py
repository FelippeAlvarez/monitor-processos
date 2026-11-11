"""Envio de e-mail via SMTP (Gmail: use uma 'senha de app', nunca a senha da conta)."""
import smtplib
import ssl
from email.message import EmailMessage


def enviar(cfg, assunto, corpo):
    """cfg é o dicionário 'email' da configuração. Levanta exceção em caso de falha."""
    for campo in ("smtp_host", "usuario", "senha", "destinatario"):
        if not cfg.get(campo):
            raise ValueError(f"Configuração de e-mail incompleta: falta '{campo}'.")
    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = cfg.get("remetente") or cfg["usuario"]
    msg["To"] = cfg["destinatario"]
    msg.set_content(corpo)
    contexto = ssl.create_default_context()
    porta = int(cfg.get("smtp_porta") or 465)
    if porta == 465:
        with smtplib.SMTP_SSL(cfg["smtp_host"], porta, context=contexto, timeout=30) as s:
            s.login(cfg["usuario"], cfg["senha"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg["smtp_host"], porta, timeout=30) as s:
            s.starttls(context=contexto)
            s.login(cfg["usuario"], cfg["senha"])
            s.send_message(msg)
