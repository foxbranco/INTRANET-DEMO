import os
import re
import secrets
import hashlib
import smtplib
import bcrypt
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

MENSAGEM = "Se o CPF possuir um e-mail cadastrado, enviaremos as instruções de recuperação."


class SolicitarRecuperacao(BaseModel):
    cpf: str


class RedefinirSenha(BaseModel):
    token: str
    nova_senha: str


def enviar_email(destinatario, link):
    mensagem = EmailMessage()
    mensagem["Subject"] = "Intranet - Recuperação de senha"
    mensagem["From"] = os.environ["SMTP_FROM"]
    mensagem["To"] = destinatario
    mensagem.set_content(
        "Recebemos uma solicitação de recuperação de senha.\n\n"
        f"Acesse este link para criar uma nova senha:\n{link}\n\n"
        "O link expira em 15 minutos e pode ser usado uma única vez.\n\n"
        "Se você não solicitou a recuperação, ignore este e-mail."
    )

    with smtplib.SMTP(
        os.environ["SMTP_HOST"],
        int(os.getenv("SMTP_PORT", "587")),
        timeout=15
    ) as smtp:
        smtp.starttls()
        smtp.login(
            os.environ["SMTP_USER"],
            os.environ["SMTP_PASSWORD"]
        )
        smtp.send_message(mensagem)


def configurar(conectar, hash_token):
    @router.post("/api/esqueci-senha")
    def solicitar(dados: SolicitarRecuperacao, request: Request):
        import hmac

        cpf = re.sub(r"\D", "", dados.cpf)
        ip = request.client.host if request.client else "desconhecido"
        segredo = os.environ["RECUPERACAO_IP_SECRET"].encode()

        ip_hash = hmac.new(
            segredo,
            ip.encode(),
            hashlib.sha256
        ).hexdigest()

        with conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM intranet_recuperacao_tentativas
                    WHERE criado_em < NOW() - INTERVAL '24 hours'
                """)

                cur.execute("""
                    SELECT pg_advisory_xact_lock(
                        hashtextextended(%s, 0)
                    )
                """, (ip_hash,))

                cur.execute("""
                    SELECT COUNT(*)
                    FROM intranet_recuperacao_tentativas
                    WHERE ip_hash = %s
                      AND criado_em > NOW() - INTERVAL '15 minutes'
                """, (ip_hash,))

                quantidade = cur.fetchone()[0]

                if quantidade >= 5:
                    raise HTTPException(
                        429,
                        "Muitas solicitações. Tente novamente mais tarde."
                    )

                cur.execute("""
                    INSERT INTO intranet_recuperacao_tentativas (ip_hash)
                    VALUES (%s)
                """, (ip_hash,))

        if len(cpf) != 11:
            return {"mensagem": MENSAGEM}

        with conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, email
                    FROM intranet_usuarios
                    WHERE cpf = %s AND ativo = TRUE
                """, (cpf,))
                usuario = cur.fetchone()

                if not usuario or not usuario[1]:
                    return {"mensagem": MENSAGEM}

                uid, email = usuario

                cur.execute("""
                    SELECT COUNT(*)
                    FROM intranet_recuperacao_senha
                    WHERE usuario_id = %s
                      AND criado_em > NOW() - INTERVAL '5 minutes'
                """, (uid,))

                if cur.fetchone()[0] > 0:
                    return {"mensagem": MENSAGEM}

                token = secrets.token_urlsafe(48)

                cur.execute("""
                    INSERT INTO intranet_recuperacao_senha
                        (usuario_id, token_hash, expira_em)
                    VALUES (%s, %s, %s)
                """, (
                    uid,
                    hash_token(token),
                    datetime.now(timezone.utc) + timedelta(minutes=15)
                ))

        base_url = os.environ["INTRANET_PUBLIC_URL"].rstrip("/")
        link = f"{base_url}/redefinir-senha.html?token={token}"

        try:
            enviar_email(email, link)
        except Exception:
            import logging
            logging.exception("Falha no envio de recuperação de senha")

        return {"mensagem": MENSAGEM}

    @router.post("/api/redefinir-senha")
    def redefinir(dados: RedefinirSenha):
        if not dados.token or len(dados.token) > 256:
            raise HTTPException(400, "Link inválido ou expirado")

        if len(dados.nova_senha) < 8:
            raise HTTPException(400, "A senha deve ter pelo menos 8 caracteres")

        with conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT r.id, r.usuario_id, u.cpf
                    FROM intranet_recuperacao_senha r
                    JOIN intranet_usuarios u ON u.id = r.usuario_id
                    WHERE r.token_hash = %s
                      AND r.utilizado_em IS NULL
                      AND r.expira_em > NOW()
                      AND u.ativo = TRUE
                    FOR UPDATE OF r
                """, (hash_token(dados.token),))

                registro = cur.fetchone()

                if not registro:
                    raise HTTPException(400, "Link inválido ou expirado")

                recuperacao_id, uid, cpf = registro

                if dados.nova_senha == cpf:
                    raise HTTPException(400, "A senha não pode ser o CPF")

                senha_hash = bcrypt.hashpw(
                    dados.nova_senha.encode(),
                    bcrypt.gensalt(rounds=12)
                ).decode()

                cur.execute("""
                    UPDATE intranet_usuarios
                    SET senha_hash = %s, trocar_senha = FALSE
                    WHERE id = %s
                """, (senha_hash, uid))

                cur.execute("""
                    UPDATE intranet_recuperacao_senha
                    SET utilizado_em = NOW()
                    WHERE usuario_id = %s AND utilizado_em IS NULL
                """, (uid,))

                cur.execute("""
                    UPDATE intranet_sessoes
                    SET revogado_em = NOW()
                    WHERE usuario_id = %s AND revogado_em IS NULL
                """, (uid,))

        return {"mensagem": "Senha redefinida com sucesso"}
