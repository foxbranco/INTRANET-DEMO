from dashboards_acesso_api import exigir_acesso_dashboard
from fastapi import APIRouter, Header, HTTPException
from typing import Optional
from collections import Counter
import json

router_pesquisas_dashboard = APIRouter(
    prefix="/api/dashboards/pesquisas"
)

_conectar = None
_autenticar = None
MINIMO_ANONIMO = 5


def configurar_pesquisas_dashboard(conectar, autenticar):
    global _conectar, _autenticar
    _conectar = conectar
    _autenticar = autenticar


def validar_administrador(authorization, rota_dashboard):
    if not rota_dashboard:
        raise HTTPException(403, "Identificacao do dashboard ausente")
    exigir_acesso_dashboard(authorization, rota_dashboard)

def interpretar(valor):
    if isinstance(valor, str):
        try:
            return json.loads(valor)
        except (ValueError, TypeError):
            return valor
    return valor


@router_pesquisas_dashboard.get("/lista")
def listar_pesquisas(
    authorization: Optional[str] = Header(None),
    rota_dashboard: Optional[str] = Header(None, alias="X-Intranet-Dashboard-Route")
):
    validar_administrador(authorization, rota_dashboard)

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    p.id,
                    p.titulo,
                    p.status,
                    p.anonima,
                    COUNT(DISTINCT e.id)
                FROM pesq_pesquisas p
                LEFT JOIN pesq_envios e
                    ON e.pesquisa_id = p.id
                GROUP BY p.id
                ORDER BY p.id
            """)

            registros = cur.fetchall()

    return [
        {
            "id": r[0],
            "titulo": r[1],
            "status": r[2],
            "anonima": r[3],
            "participantes": r[4]
        }
        for r in registros
    ]


@router_pesquisas_dashboard.get("/{pesquisa_id}")
def resultados_pesquisa(
    pesquisa_id: int,
    authorization: Optional[str] = Header(None),
    rota_dashboard: Optional[str] = Header(None, alias="X-Intranet-Dashboard-Route")
):
    validar_administrador(authorization, rota_dashboard)

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, titulo, status, anonima
                FROM pesq_pesquisas
                WHERE id = %s
            """, (pesquisa_id,))

            pesquisa = cur.fetchone()

            if not pesquisa:
                raise HTTPException(404, "Pesquisa nao encontrada")

            cur.execute("""
                SELECT COUNT(*)
                FROM pesq_envios
                WHERE pesquisa_id = %s
            """, (pesquisa_id,))

            participantes = cur.fetchone()[0]

            cur.execute("""
                SELECT COUNT(*)
                FROM pesq_respostas r
                JOIN pesq_envios e ON e.id = r.envio_id
                WHERE e.pesquisa_id = %s
            """, (pesquisa_id,))

            total_respostas = cur.fetchone()[0]

            resultado = {
                "id": pesquisa[0],
                "titulo": pesquisa[1],
                "status": pesquisa[2],
                "anonima": pesquisa[3],
                "participantes": participantes,
                "total_respostas": total_respostas,
                "perguntas": []
            }

            if pesquisa[3] and participantes < MINIMO_ANONIMO:
                resultado["protegido"] = True
                resultado["mensagem"] = (
                    "Resultados detalhados indisponiveis: "
                    "participacao insuficiente para preservar "
                    "o anonimato."
                )
                return resultado

            resultado["protegido"] = False

            cur.execute("""
                SELECT id, enunciado, tipo
                FROM pesq_perguntas
                WHERE pesquisa_id = %s
                ORDER BY ordem, id
            """, (pesquisa_id,))

            perguntas = cur.fetchall()

            for pergunta_id, enunciado, tipo in perguntas:
                cur.execute("""
                    SELECT valor
                    FROM pesq_respostas
                    WHERE pergunta_id = %s
                """, (pergunta_id,))

                valores = [
                    interpretar(r[0])
                    for r in cur.fetchall()
                ]

                dados = {
                    "id": pergunta_id,
                    "enunciado": enunciado,
                    "tipo": tipo,
                    "total": len(valores)
                }

                if tipo == "TEXTO":
                    dados["comentarios"] = len([
                        v for v in valores
                        if isinstance(v, str) and v.strip()
                    ])

                elif tipo in ("NOTA_1_5", "NOTA_0_10"):
                    numeros = [
                        float(v) for v in valores
                        if isinstance(v, (int, float))
                        and not isinstance(v, bool)
                    ]

                    dados["media"] = (
                        round(sum(numeros) / len(numeros), 2)
                        if numeros else None
                    )
                    dados["distribuicao"] = dict(
                        sorted(Counter(str(int(v)) for v in numeros).items())
                    )

                elif tipo == "SIM_NAO":
                    dados["distribuicao"] = {
                        "SIM": sum(v is True for v in valores),
                        "NAO": sum(v is False for v in valores)
                    }

                elif tipo in ("ESCOLHA_UNICA", "MULTIPLA_ESCOLHA"):
                    cur.execute("""
                        SELECT id, texto
                        FROM pesq_alternativas
                        WHERE pergunta_id = %s
                        ORDER BY ordem, id
                    """, (pergunta_id,))

                    alternativas = cur.fetchall()
                    contagem = Counter()

                    for valor in valores:
                        itens = valor if isinstance(valor, list) else [valor]
                        for item in set(
                            x for x in itens if isinstance(x, int)
                        ):
                            contagem[item] += 1

                    dados["distribuicao"] = {
                        texto: contagem.get(alt_id, 0)
                        for alt_id, texto in alternativas
                    }

                resultado["perguntas"].append(dados)

    return resultado

