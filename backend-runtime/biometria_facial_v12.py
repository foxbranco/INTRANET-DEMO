"""Nucleo reutilizavel para biometria facial da Intranet V12.

Este modulo NAO realiza reconhecimento por conta propria: requer adaptador de
motor facial confiavel, executado no servidor. Nunca aceite resultado de
similaridade/prova de vida enviado diretamente pelo navegador.

Politica: falha fechada; dados biometricos sensiveis; usar HTTPS, consentimento
ou outra base legal adequada, controle de retencao e criptografia em repouso.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import Protocol


class BiometriaIndisponivel(RuntimeError):
    pass


class BiometriaRejeitada(ValueError):
    pass


@dataclass(frozen=True)
class ResultadoFacial:
    correspondencia: float
    vivacidade: float
    quantidade_rostos: int


class MotorFacial(Protocol):
    def cadastrar(self, captura: bytes) -> tuple[bytes, float, int]:
        """Retorna template biometrico, score de vivacidade, numero de rostos."""
        ...

    def comparar(self, captura: bytes, template: bytes) -> ResultadoFacial:
        """Processa imagem NO SERVIDOR, inclusive a prova de vida."""
        ...


class MotorNaoConfigurado:
    def cadastrar(self, captura: bytes) -> tuple[bytes, float, int]:
        raise BiometriaIndisponivel('Motor facial ainda nao integrado')

    def comparar(self, captura: bytes, template: bytes) -> ResultadoFacial:
        raise BiometriaIndisponivel('Motor facial ainda nao integrado')


class ServicoBiometria:
    """Validacao centralizada, independente de avisos, pesquisas e documentos."""

    def __init__(self, motor: MotorFacial | None = None,
                 limiar_correspondencia: float = 0.75,
                 limiar_vivacidade: float = 0.90,
                 tamanho_maximo: int = 2_000_000):
        self.motor = motor if motor is not None else MotorNaoConfigurado()
        self.limiar_correspondencia = limiar_correspondencia
        self.limiar_vivacidade = limiar_vivacidade
        self.tamanho_maximo = tamanho_maximo

    def _validar_captura(self, captura: bytes) -> None:
        if not isinstance(captura, bytes) or not 1024 <= len(captura) <= self.tamanho_maximo:
            raise BiometriaRejeitada('Captura facial invalida ou muito grande')

    def cadastrar(self, captura: bytes) -> bytes:
        self._validar_captura(captura)
        template, vivacidade, rostos = self.motor.cadastrar(captura)
        if rostos != 1 or vivacidade < self.limiar_vivacidade or not template:
            raise BiometriaRejeitada('Cadastro facial nao aprovado')
        return template

    def verificar(self, captura: bytes, template: bytes) -> bool:
        self._validar_captura(captura)
        if not template:
            raise BiometriaRejeitada('Cadastro facial inexistente')
        resultado = self.motor.comparar(captura, template)
        return (resultado.quantidade_rostos == 1
                and resultado.vivacidade >= self.limiar_vivacidade
                and resultado.correspondencia >= self.limiar_correspondencia)


def criar_desafio() -> tuple[str, str, datetime]:
    """Token aleatorio; armazenar apenas hash no banco, com validade curta."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest(), datetime.now(timezone.utc) + timedelta(minutes=3)


def conferir_desafio(token: str, hash_armazenado: str, validade: datetime) -> bool:
    if not token or not hash_armazenado or validade.tzinfo is None:
        return False
    return (datetime.now(timezone.utc) < validade
            and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), hash_armazenado))


SQL_ESTRUTURA = """
CREATE TABLE IF NOT EXISTS intranet_biometria_faciais (
    usuario_id BIGINT PRIMARY KEY REFERENCES intranet_usuarios(id),
    template_cifrado BYTEA NOT NULL,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revogado_em TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS intranet_biometria_desafios (
    id BIGSERIAL PRIMARY KEY,
    usuario_id BIGINT NOT NULL REFERENCES intranet_usuarios(id),
    token_hash CHAR(64) NOT NULL UNIQUE,
    expira_em TIMESTAMPTZ NOT NULL,
    utilizado_em TIMESTAMPTZ,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
