from dashboards_acesso_api import exigir_acesso_dashboard
from fastapi import APIRouter, Header, HTTPException
from typing import Optional

router_dashboards = APIRouter(prefix="/api/dashboards")

_conectar = None
_autenticar = None


def configurar_dashboards(conectar, autenticar):
    global _conectar, _autenticar
    _conectar = conectar
    _autenticar = autenticar


@router_dashboards.get("/auditoria/resumo")
def resumo_auditoria(authorization: Optional[str] = Header(None),
    rota_dashboard: Optional[str] = Header(None, alias="X-Intranet-Dashboard-Route")):
    usuario = _autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatoria")

    exigir_acesso_dashboard(
        authorization, rota_dashboard
    )

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total_eventos,
                    COUNT(*) FILTER (
                        WHERE tipo_evento = 'AUTENTICACAO'
                          AND acao = 'LOGIN'
                    ) AS logins,
                    COUNT(*) FILTER (
                        WHERE tipo_evento = 'AUTENTICACAO'
                          AND acao = 'LOGOUT'
                    ) AS logouts,
                    COUNT(DISTINCT usuario_id) FILTER (
                        WHERE tipo_evento = 'AUTENTICACAO'
                          AND acao = 'LOGIN'
                          AND resultado = 'SUCESSO'
                    ) AS usuarios_distintos,
                    COUNT(*) FILTER (
                        WHERE tipo_evento = 'DOCUMENTO'
                          AND acao = 'DOWNLOAD'
                    ) AS downloads
                FROM intranet_auditoria
            """)
            dados = cur.fetchone()

    return {
        "total_eventos": dados[0],
        "logins": dados[1],
        "logouts": dados[2],
        "usuarios_distintos": dados[3],
        "downloads": dados[4]
    }


@router_dashboards.get("/auditoria/dados")
def dados_auditoria(
    inicio: Optional[str] = None,
    fim: Optional[str] = None,
    modulo: Optional[str] = None,
    departamento: Optional[str] = None,
    usuario_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
    rota_dashboard: Optional[str] = Header(None, alias="X-Intranet-Dashboard-Route")
):
    from datetime import date

    usuario = _autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatoria")

    exigir_acesso_dashboard(
        authorization, rota_dashboard
    )

    filtros = [
        "NOT (a.tipo_evento = 'PESQUISAS' AND "
        "a.acao IN ('RESPONDER', 'RESPOSTA', 'ENVIAR_RESPOSTA', 'PARTICIPAR'))"
    ]
    parametros = []

    if inicio:
        try:
            date.fromisoformat(inicio)
        except ValueError:
            raise HTTPException(400, "Data inicial invalida")
        filtros.append("(a.criado_em AT TIME ZONE 'America/Sao_Paulo')::date >= %s")
        parametros.append(inicio)

    if fim:
        try:
            date.fromisoformat(fim)
        except ValueError:
            raise HTTPException(400, "Data final invalida")
        filtros.append("(a.criado_em AT TIME ZONE 'America/Sao_Paulo')::date <= %s")
        parametros.append(fim)

    if modulo:
        filtros.append("a.modulo = %s")
        parametros.append(modulo)

    if departamento:
        filtros.append("u.departamento = %s")
        parametros.append(departamento)

    if usuario_id is not None:
        filtros.append("a.usuario_id = %s")
        parametros.append(usuario_id)

    where = " AND ".join(filtros)

    base = """
        FROM intranet_auditoria a
        LEFT JOIN intranet_usuarios u ON u.id = a.usuario_id
        WHERE
    """ + where

    with _conectar() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    COUNT(*),
                    COUNT(*) FILTER (WHERE a.acao = 'LOGIN'
                        AND a.resultado = 'SUCESSO'),
                    COUNT(DISTINCT a.usuario_id) FILTER (
                        WHERE a.acao = 'LOGIN'
                        AND a.resultado = 'SUCESSO'),
                    COUNT(*) FILTER (WHERE a.acao = 'LOGOUT'),
                    COUNT(*) FILTER (WHERE a.acao = 'DOWNLOAD')
            """ + base, parametros)

            totais = cur.fetchone()

            cur.execute("""
                SELECT
                    (a.criado_em AT TIME ZONE 'America/Sao_Paulo')::date,
                    COUNT(*)
            """ + base + """
                GROUP BY 1 ORDER BY 1
            """, parametros)

            por_dia = [
                {"data": str(r[0]), "quantidade": r[1]}
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT a.modulo, COUNT(*)
            """ + base + """
                AND a.acao = 'ACESSO_MODULO'
                GROUP BY a.modulo
                ORDER BY COUNT(*) DESC
            """, parametros)

            por_modulo = [
                {"modulo": r[0], "quantidade": r[1]}
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT
                    to_char(
                        a.criado_em AT TIME ZONE 'America/Sao_Paulo',
                        'DD/MM/YYYY HH24:MI:SS'
                    ),
                    u.nome,
                    u.departamento,
                    a.modulo,
                    a.acao,
                    a.resultado
            """ + base + """
                ORDER BY a.criado_em DESC
                LIMIT 100
            """, parametros)

            historico = [
                {
                    "data": r[0],
                    "usuario": r[1],
                    "departamento": r[2],
                    "modulo": r[3],
                    "acao": r[4],
                    "resultado": r[5]
                }
                for r in cur.fetchall()
            ]

    return {
        "resumo": {
            "total_eventos": totais[0],
            "logins": totais[1],
            "usuarios_distintos": totais[2],
            "logouts": totais[3],
            "downloads": totais[4]
        },
        "por_dia": por_dia,
        "por_modulo": por_modulo,
        "historico": historico
    }

