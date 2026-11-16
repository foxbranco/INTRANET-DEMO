from typing import Optional
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router_admin_permissoes = APIRouter(prefix="/api/admin/permissoes")
_conectar = None
_autenticar = None

def configurar_admin_permissoes(conectar, autenticar):
    global _conectar, _autenticar
    _conectar, _autenticar = conectar, autenticar

def usuario_atual(authorization):
    if _autenticar is None or _conectar is None:
        raise HTTPException(503, "Modulo nao configurado")
    u = _autenticar(authorization)
    if u[5]:
        raise HTTPException(403, "Troca de senha obrigatoria")
    return u

def exigir_nivel3(authorization):
    u = usuario_atual(authorization)
    if u[3] != 3:
        raise HTTPException(403, "Exclusivo nivel 3")
    return u

class Permissao(BaseModel):
    usuario_id: int = Field(gt=0)
    modulo_id: int = Field(gt=0)

@router_admin_permissoes.get("/minhas")
def minhas(authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    if u[3] == 1:
        return []
    with _conectar() as conn:
        with conn.cursor() as cur:
            if u[3] == 3:
                cur.execute("""
                    SELECT codigo FROM intranet_admin_modulos
                    WHERE ativo = TRUE ORDER BY codigo
                """)
            else:
                cur.execute("""
                    SELECT m.codigo
                    FROM intranet_admin_permissoes p
                    JOIN intranet_admin_modulos m ON m.id = p.modulo_id
                    WHERE p.usuario_id = %s
                      AND p.ativo = TRUE AND m.ativo = TRUE
                    ORDER BY m.codigo
                """, (u[0],))
            return [r[0] for r in cur.fetchall()]

@router_admin_permissoes.get("/modulos")
def modulos(authorization: Optional[str] = Header(None)):
    exigir_nivel3(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, codigo, nome FROM intranet_admin_modulos
                WHERE ativo = TRUE ORDER BY nome
            """)
            return [
                {"id": r[0], "codigo": r[1], "nome": r[2]}
                for r in cur.fetchall()
            ]

@router_admin_permissoes.get("/usuarios")
def usuarios(authorization: Optional[str] = Header(None)):
    exigir_nivel3(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, nome, departamento, cargo
                FROM intranet_usuarios
                WHERE ativo = TRUE AND nivel_acesso = 2
                ORDER BY nome
            """)
            return [
                {"id": r[0], "nome": r[1],
                 "departamento": r[2], "cargo": r[3]}
                for r in cur.fetchall()
            ]

@router_admin_permissoes.get("/lista")
def lista(authorization: Optional[str] = Header(None)):
    exigir_nivel3(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.usuario_id, p.modulo_id, p.ativo
                FROM intranet_admin_permissoes p
                JOIN intranet_usuarios u ON u.id = p.usuario_id
                WHERE u.nivel_acesso = 2
                ORDER BY p.usuario_id, p.modulo_id
            """)
            return [
                {"usuario_id": r[0], "modulo_id": r[1], "ativo": r[2]}
                for r in cur.fetchall()
            ]

@router_admin_permissoes.post("/conceder")
def conceder(d: Permissao, authorization: Optional[str] = Header(None)):
    admin = exigir_nivel3(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT nivel_acesso FROM intranet_usuarios
                WHERE id = %s AND ativo = TRUE
                FOR UPDATE
            """, (d.usuario_id,))
            r = cur.fetchone()
            if not r or r[0] != 2:
                raise HTTPException(
                    403, "Somente nivel 2 pode receber permissoes"
                )
            cur.execute("""
                SELECT id FROM intranet_admin_modulos
                WHERE id = %s AND ativo = TRUE
            """, (d.modulo_id,))
            if not cur.fetchone():
                raise HTTPException(404, "Modulo inexistente ou inativo")
            cur.execute("""
                INSERT INTO intranet_admin_permissoes
                    (usuario_id, modulo_id, ativo, concedido_por)
                VALUES (%s, %s, TRUE, %s)
                ON CONFLICT (usuario_id, modulo_id)
                DO UPDATE SET ativo = TRUE,
                    concedido_por = EXCLUDED.concedido_por,
                    atualizado_em = NOW()
            """, (d.usuario_id, d.modulo_id, admin[0]))
    return {"ok": True, "acao": "concedida"}

@router_admin_permissoes.post("/revogar")
def revogar(d: Permissao, authorization: Optional[str] = Header(None)):
    exigir_nivel3(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE intranet_admin_permissoes
                SET ativo = FALSE, atualizado_em = NOW()
                WHERE usuario_id = %s AND modulo_id = %s
                RETURNING id
            """, (d.usuario_id, d.modulo_id))
            if not cur.fetchone():
                raise HTTPException(404, "Permissao nao encontrada")
    return {"ok": True, "acao": "revogada"}
