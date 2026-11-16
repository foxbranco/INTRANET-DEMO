from typing import Optional
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router_gestores_equipes = APIRouter(
    prefix="/api/admin/gestores-equipes",
    tags=["Admin - Gestores e Equipes"]
)

_conectar = None
_autenticar = None

def configurar_gestores_equipes(conectar, autenticar):
    global _conectar, _autenticar
    _conectar, _autenticar = conectar, autenticar

def exigir_admin(authorization):
    if _conectar is None or _autenticar is None:
        raise HTTPException(503, "Modulo nao configurado")

    u = _autenticar(authorization)

    if u[5]:
        raise HTTPException(403, "Troca de senha obrigatoria")

    nivel = int(u[3])

    if nivel == 3:
        return u

    if nivel != 2:
        raise HTTPException(403, "Acesso administrativo nao autorizado")

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1
                FROM intranet_admin_permissoes p
                JOIN intranet_admin_modulos m ON m.id = p.modulo_id
                WHERE p.usuario_id = %s
                  AND p.ativo = TRUE
                  AND m.ativo = TRUE
                  AND m.codigo = 'GESTORES_EQUIPES'
                LIMIT 1
            """, (u[0],))

            if not cur.fetchone():
                raise HTTPException(
                    403, "Sem permissao para Gestores e Equipes"
                )

    return u
class Vinculo(BaseModel):
    funcionario_id: int = Field(gt=0)
    gestor_id: int = Field(gt=0)

@router_gestores_equipes.get("/usuarios")
def usuarios(authorization: Optional[str] = Header(None)):
    exigir_admin(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, nome, tipo, nivel_acesso, departamento, cargo, centro_custo
                FROM intranet_usuarios
                WHERE ativo = TRUE
                ORDER BY nome
            """)
            return [
                dict(id=r[0], nome=r[1], tipo=r[2],
                     nivel_acesso=r[3], departamento=r[4], cargo=r[5], centro_custo=r[6])
                for r in cur.fetchall()
            ]

@router_gestores_equipes.get("/vinculos")
def vinculos(authorization: Optional[str] = Header(None)):
    exigir_admin(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT v.id, v.funcionario_id, v.gestor_id,
                       v.inicio_em, u.nome, g.nome
                FROM intranet_gestores_funcionarios v
                JOIN intranet_usuarios u ON u.id = v.funcionario_id
                JOIN intranet_usuarios g ON g.id = v.gestor_id
                WHERE v.ativo = TRUE
                ORDER BY g.nome, u.nome
            """)
            return [
                dict(id=r[0], funcionario_id=r[1], gestor_id=r[2],
                     inicio_em=r[3].isoformat(),
                     funcionario_nome=r[4], gestor_nome=r[5])
                for r in cur.fetchall()
            ]

@router_gestores_equipes.get("/historico/{funcionario_id}")
def historico(
    funcionario_id: int,
    authorization: Optional[str] = Header(None)
):
    exigir_admin(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT v.id, g.nome, v.inicio_em, v.fim_em, v.ativo
                FROM intranet_gestores_funcionarios v
                JOIN intranet_usuarios g ON g.id = v.gestor_id
                WHERE v.funcionario_id = %s
                ORDER BY v.inicio_em DESC, v.id DESC
            """, (funcionario_id,))
            return [
                dict(id=r[0], gestor=r[1],
                     inicio_em=r[2].isoformat(),
                     fim_em=r[3].isoformat() if r[3] else None,
                     ativo=r[4])
                for r in cur.fetchall()
            ]

@router_gestores_equipes.post("/vincular")
def vincular(
    dados: Vinculo,
    authorization: Optional[str] = Header(None)
):
    admin = exigir_admin(authorization)

    if dados.funcionario_id == dados.gestor_id:
        raise HTTPException(400, "Usuario nao pode gerir a si mesmo")

    with _conectar() as conn:
        with conn.cursor() as cur:
            # Serializa alteracoes de hierarquia para evitar ciclos concorrentes.
            cur.execute("SELECT pg_advisory_xact_lock(7841201)")

            cur.execute("""
                SELECT id, tipo, ativo
                FROM intranet_usuarios
                WHERE id IN (%s, %s)
                FOR UPDATE
            """, (dados.funcionario_id, dados.gestor_id))
            pessoas = {r[0]: r for r in cur.fetchall()}

            funcionario = pessoas.get(dados.funcionario_id)
            gestor = pessoas.get(dados.gestor_id)

            if not funcionario or not funcionario[2]:
                raise HTTPException(400, "Funcionario inexistente ou inativo")

            if not gestor or not gestor[2] or gestor[1] != 2:
                raise HTTPException(400, "Gestor deve ser usuario ativo tipo 2")

            # Impede ciclos na cadeia de gestores.
            cur.execute("""
                WITH RECURSIVE superiores(id, caminho) AS (
                    SELECT gestor_id, ARRAY[funcionario_id, gestor_id]
                    FROM intranet_gestores_funcionarios
                    WHERE funcionario_id = %s AND ativo = TRUE

                    UNION ALL

                    SELECT v.gestor_id, s.caminho || v.gestor_id
                    FROM superiores s
                    JOIN intranet_gestores_funcionarios v
                      ON v.funcionario_id = s.id AND v.ativo = TRUE
                    WHERE NOT v.gestor_id = ANY(s.caminho)
                )
                SELECT 1 FROM superiores
                WHERE id = %s LIMIT 1
            """, (dados.gestor_id, dados.funcionario_id))

            if cur.fetchone():
                raise HTTPException(400, "Vinculo criaria ciclo na hierarquia")

            cur.execute("""
                SELECT id, gestor_id
                FROM intranet_gestores_funcionarios
                WHERE funcionario_id = %s AND ativo = TRUE
                FOR UPDATE
            """, (dados.funcionario_id,))
            atual = cur.fetchone()

            if atual and atual[1] == dados.gestor_id:
                return {"ok": True, "mensagem": "Vinculo ja existente"}

            if atual:
                cur.execute("""
                    UPDATE intranet_gestores_funcionarios
                    SET ativo = FALSE, fim_em = NOW()
                    WHERE id = %s
                """, (atual[0],))

            cur.execute("""
                INSERT INTO intranet_gestores_funcionarios
                    (funcionario_id, gestor_id, alterado_por)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (dados.funcionario_id, dados.gestor_id, admin[0]))
            novo_id = cur.fetchone()[0]

    return {"ok": True, "vinculo_id": novo_id}

@router_gestores_equipes.post("/desvincular/{funcionario_id}")
def desvincular(
    funcionario_id: int,
    authorization: Optional[str] = Header(None)
):
    exigir_admin(authorization)

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(7841201)")
            cur.execute("""
                UPDATE intranet_gestores_funcionarios
                SET ativo = FALSE, fim_em = NOW()
                WHERE funcionario_id = %s AND ativo = TRUE
                RETURNING id
            """, (funcionario_id,))
            resultado = cur.fetchone()

    if not resultado:
        raise HTTPException(404, "Vinculo ativo nao encontrado")

    return {"ok": True, "vinculo_id": resultado[0]}
