"""API de Pesquisas V12 — versão de desenvolvimento, não implantar em produção.

Requer as tabelas pesq_* do SQL revisado. Não executa migrações.
"""
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

router_pesquisas = APIRouter(prefix="/api/pesquisas", tags=["Pesquisas"])
_deps = {}


def configurar_pesquisas(conectar, autenticar, registrar_auditoria):
    _deps.update(conectar=conectar, autenticar=autenticar, auditoria=registrar_auditoria)


def _usuario(authorization, admin=False):
    if not _deps:
        raise RuntimeError("Módulo de pesquisas não configurado")
    u = _deps["autenticar"](authorization)
    if u[5]:
        raise HTTPException(403, "Troca de senha obrigatória")
    if admin and u[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")
    return u


def _auditar(u, acao, pesquisa_id):
    # Nunca registra conteúdo de respostas ou identificação de respondentes.
    _deps["auditoria"](_deps["conectar"], u[0], u[6], "PESQUISAS", acao,
                       "PESQUISAS", "SUCESSO", recurso_id=str(pesquisa_id))


def _registro(cur, pesquisa_id, lock=False):
    cur.execute("SELECT id,titulo,descricao,anonima,status,imagem_capa,inicio_em,encerramento_em,divulgar_resultados FROM pesq_pesquisas WHERE id=%s" + (" FOR UPDATE" if lock else ""), (pesquisa_id,))
    r = cur.fetchone()
    if not r:
        raise HTTPException(404, "Pesquisa não encontrada")
    return dict(zip(("id", "titulo", "descricao", "anonima", "status", "imagem_capa", "inicio_em", "encerramento_em", "divulgar_resultados"), r))


def _criterio_valido(tipo, valor):
    if tipo not in {"TODOS", "DEPARTAMENTO", "CENTRO_CUSTO", "CARGO"}:
        raise HTTPException(422, "Tipo de público inválido")
    if (tipo == "TODOS" and valor is not None) or (tipo != "TODOS" and (not isinstance(valor, str) or not valor.strip())):
        raise HTTPException(422, "Valor do público inválido")


def _publico_permitido(cur, pesquisa_id, usuario_id):
    cur.execute("""
        SELECT EXISTS (
            SELECT 1
            FROM intranet_usuarios u
            WHERE u.id = %s
              AND u.ativo IS TRUE
              AND EXISTS (
                  SELECT 1
                  FROM pesq_publico pub
                  WHERE pub.pesquisa_id = %s
                    AND (
                        pub.tipo = 'TODOS'
                        OR (
                            pub.tipo = 'DEPARTAMENTO'
                            AND UPPER(TRIM(pub.valor)) = UPPER(TRIM(u.departamento))
                        )
                        OR (
                            pub.tipo = 'CENTRO_CUSTO'
                            AND UPPER(TRIM(pub.valor)) = UPPER(TRIM(u.centro_custo))
                        )
                        OR (
                            pub.tipo = 'CARGO'
                            AND UPPER(TRIM(pub.valor)) = UPPER(TRIM(u.cargo))
                        )
                    )
              )
        )
    """, (usuario_id, pesquisa_id))

    return bool(cur.fetchone()[0])

class PesquisaNova(BaseModel):
    titulo: str = Field(min_length=1, max_length=200)
    descricao: Optional[str] = None
    anonima: bool = False
    imagem_capa: Optional[str] = None
    inicio_em: Optional[datetime] = None
    encerramento_em: Optional[datetime] = None


class PerguntaNova(BaseModel):
    enunciado: str = Field(min_length=1)
    tipo: str
    ordem: int = Field(ge=1)
    alternativas: list[str] = Field(default_factory=list)



class PerguntaEdicao(BaseModel):
    enunciado: str = Field(min_length=1, max_length=1000)
    ordem: int = Field(ge=1)
class PublicoNovo(BaseModel):
    tipo: str
    valor: Optional[str] = None


class Resposta(BaseModel):
    pergunta_id: int
    valor: Any


class Envio(BaseModel):
    respostas: list[Resposta] = Field(min_length=1)


@router_pesquisas.post("", status_code=201)
def criar_pesquisa(dados: PesquisaNova, authorization: Optional[str] = Header(None)):
    u = _usuario(authorization, True)
    if dados.inicio_em and dados.encerramento_em and dados.encerramento_em <= dados.inicio_em:
        raise HTTPException(422, "Encerramento deve ser posterior ao início")
    with _deps["conectar"]() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pesq_pesquisas(titulo,descricao,anonima,imagem_capa,inicio_em,encerramento_em,criado_por)
                VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (dados.titulo.strip(), dados.descricao, dados.anonima, dados.imagem_capa,
                 dados.inicio_em, dados.encerramento_em, u[0]))
            pid = cur.fetchone()[0]
            cur.execute("INSERT INTO pesq_auditoria(pesquisa_id,usuario_id,acao,dados_novos) VALUES (%s,%s,'CRIAR',%s)",
                        (pid, u[0], Jsonb({"titulo": dados.titulo, "anonima": dados.anonima})))
    _auditar(u, "CRIAR", pid)
    return {"id": pid, "status": "RASCUNHO"}


class PesquisaIdentificacao(BaseModel):
    anonima: bool


@router_pesquisas.put("/{pesquisa_id}/identificacao")
def atualizar_identificacao_pesquisa(
    pesquisa_id: int,
    dados: PesquisaIdentificacao,
    authorization: Optional[str] = Header(None)
):
    u = _usuario(authorization, True)

    with _deps["conectar"]() as conn:
        with conn.cursor() as cur:
            p = _registro(cur, pesquisa_id, lock=True)

            if p["status"] != "RASCUNHO":
                raise HTTPException(
                    409, "A identificação só pode ser alterada em rascunho"
                )

            anterior = p["anonima"]

            cur.execute(
                "UPDATE pesq_pesquisas SET anonima=%s WHERE id=%s",
                (dados.anonima, pesquisa_id)
            )

            cur.execute(
                """INSERT INTO pesq_auditoria
                   (pesquisa_id, usuario_id, acao,
                    dados_anteriores, dados_novos)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    pesquisa_id,
                    u[0],
                    "ALTERAR_IDENTIFICACAO",
                    Jsonb({"anonima": anterior}),
                    Jsonb({"anonima": dados.anonima})
                )
            )

    _auditar(u, "ALTERAR_IDENTIFICACAO", pesquisa_id)

    return {
        "id": pesquisa_id,
        "anonima": dados.anonima,
        "status": "RASCUNHO"
    }

@router_pesquisas.get("/admin")
def listar_admin(authorization: Optional[str] = Header(None)):
    _usuario(authorization, True)
    with _deps["conectar"]() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,titulo,anonima,status,inicio_em,encerramento_em FROM pesq_pesquisas ORDER BY id DESC")
            return [dict(zip(("id", "titulo", "anonima", "status", "inicio_em", "encerramento_em"), r)) for r in cur.fetchall()]


@router_pesquisas.post("/{pesquisa_id}/perguntas", status_code=201)
def adicionar_pergunta(pesquisa_id: int, dados: PerguntaNova, authorization: Optional[str] = Header(None)):
    u = _usuario(authorization, True)
    tipos = {"NOTA_1_5", "NOTA_0_10", "ESCOLHA_UNICA", "MULTIPLA_ESCOLHA", "TEXTO", "SIM_NAO"}
    if dados.tipo not in tipos:
        raise HTTPException(422, "Tipo de pergunta inválido")
    if dados.tipo in {"ESCOLHA_UNICA", "MULTIPLA_ESCOLHA"} and not dados.alternativas:
        raise HTTPException(422, "Pergunta de escolha exige alternativas")
    if dados.tipo not in {"ESCOLHA_UNICA", "MULTIPLA_ESCOLHA"} and dados.alternativas:
        raise HTTPException(422, "Este tipo não admite alternativas")
    if any(not a.strip() or len(a) > 500 for a in dados.alternativas):
        raise HTTPException(422, "Alternativa vazia ou muito longa")
    with _deps["conectar"]() as conn:
        with conn.cursor() as cur:
            p = _registro(cur, pesquisa_id, True)
            if p["status"] != "RASCUNHO":
                raise HTTPException(409, "Perguntas bloqueadas após sair do rascunho")
            cur.execute("INSERT INTO pesq_perguntas(pesquisa_id,enunciado,tipo,ordem) VALUES (%s,%s,%s,%s) RETURNING id",
                        (pesquisa_id, dados.enunciado.strip(), dados.tipo, dados.ordem))
            qid = cur.fetchone()[0]
            for i, texto in enumerate(dados.alternativas, 1):
                cur.execute("INSERT INTO pesq_alternativas(pergunta_id,texto,ordem) VALUES (%s,%s,%s)", (qid, texto.strip(), i))
            cur.execute("INSERT INTO pesq_auditoria(pesquisa_id,usuario_id,acao,dados_novos) VALUES (%s,%s,'EDITAR',%s)",
                        (pesquisa_id, u[0], Jsonb({"pergunta_id": qid, "tipo": dados.tipo})))
    _auditar(u, "EDITAR", pesquisa_id)
    return {"id": qid}



@router_pesquisas.put("/{pesquisa_id}/perguntas/{pergunta_id}")
def editar_pergunta(
    pesquisa_id: int,
    pergunta_id: int,
    dados: PerguntaEdicao,
    authorization: Optional[str] = Header(None)
):
    u = _usuario(authorization, True)

    enunciado = dados.enunciado.strip()
    if not enunciado:
        raise HTTPException(422, "O enunciado nao pode ficar vazio")

    with _deps["conectar"]() as conn:
        with conn.cursor() as cur:
            p = _registro(cur, pesquisa_id, True)

            if p["status"] != "RASCUNHO":
                raise HTTPException(409, "Somente perguntas de rascunhos podem ser editadas")

            cur.execute("""
                SELECT id, enunciado, ordem, tipo
                FROM pesq_perguntas
                WHERE id = %s AND pesquisa_id = %s
                FOR UPDATE
            """, (pergunta_id, pesquisa_id))

            pergunta = cur.fetchone()

            if not pergunta:
                raise HTTPException(404, "Pergunta nao encontrada")

            cur.execute("""
                SELECT 1 FROM pesq_perguntas
                WHERE pesquisa_id = %s
                  AND ordem = %s
                  AND id <> %s
            """, (pesquisa_id, dados.ordem, pergunta_id))

            if cur.fetchone():
                raise HTTPException(409, "Ja existe outra pergunta nessa ordem")

            cur.execute("""
                UPDATE pesq_perguntas
                SET enunciado = %s, ordem = %s
                WHERE id = %s AND pesquisa_id = %s
            """, (enunciado, dados.ordem, pergunta_id, pesquisa_id))

            cur.execute("""
                INSERT INTO pesq_auditoria
                    (pesquisa_id, usuario_id, acao, dados_anteriores, dados_novos)
                VALUES (%s, %s, 'EDITAR', %s, %s)
            """, (
                pesquisa_id,
                u[0],
                Jsonb({"pergunta_id": pergunta_id, "enunciado": pergunta[1], "ordem": pergunta[2]}),
                Jsonb({"pergunta_id": pergunta_id, "enunciado": enunciado, "ordem": dados.ordem})
            ))

    _auditar(u, "EDITAR", pesquisa_id)

    return {"id": pergunta_id, "mensagem": "Pergunta atualizada com sucesso"}


@router_pesquisas.delete("/{pesquisa_id}/perguntas/{pergunta_id}")
def excluir_pergunta(
    pesquisa_id: int,
    pergunta_id: int,
    authorization: Optional[str] = Header(None)
):
    u = _usuario(authorization, True)

    with _deps["conectar"]() as conn:
        with conn.cursor() as cur:
            p = _registro(cur, pesquisa_id, True)

            if p["status"] != "RASCUNHO":
                raise HTTPException(409, "Somente perguntas de rascunhos podem ser excluidas")

            cur.execute("""
                SELECT id, enunciado, tipo, ordem
                FROM pesq_perguntas
                WHERE id = %s AND pesquisa_id = %s
                FOR UPDATE
            """, (pergunta_id, pesquisa_id))

            pergunta = cur.fetchone()

            if not pergunta:
                raise HTTPException(404, "Pergunta nao encontrada")

            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM pesq_respostas
                    WHERE pergunta_id = %s
                )
            """, (pergunta_id,))

            if cur.fetchone()[0]:
                raise HTTPException(409, "Pergunta possui respostas vinculadas e nao pode ser excluida")

            cur.execute("""
                DELETE FROM pesq_alternativas
                WHERE pergunta_id = %s
            """, (pergunta_id,))

            cur.execute("""
                DELETE FROM pesq_perguntas
                WHERE id = %s AND pesquisa_id = %s
            """, (pergunta_id, pesquisa_id))

            cur.execute("""
                INSERT INTO pesq_auditoria
                    (pesquisa_id, usuario_id, acao, dados_anteriores)
                VALUES (%s, %s, 'EXCLUIR_PERGUNTA', %s)
            """, (
                pesquisa_id,
                u[0],
                Jsonb({
                    "pergunta_id": pergunta_id,
                    "enunciado": pergunta[1],
                    "tipo": pergunta[2],
                    "ordem": pergunta[3]
                })
            ))

    _auditar(u, "EXCLUIR_PERGUNTA", pesquisa_id)

    return {
        "id": pergunta_id,
        "mensagem": "Pergunta e alternativas excluidas com sucesso"
    }

class PublicoMultiplo(BaseModel):
    tipo: str
    valores: list[str] = Field(default_factory=list)


@router_pesquisas.put("/{pesquisa_id}/publico")
def definir_publico(
    pesquisa_id: int,
    dados: PublicoMultiplo,
    authorization: Optional[str] = Header(None)
):
    u = _usuario(authorization, True)

    tipo = dados.tipo.upper().strip()

    if tipo not in {"TODOS", "DEPARTAMENTO", "CENTRO_CUSTO", "CARGO"}:
        raise HTTPException(422, "Tipo de publico invalido")

    valores = list(dict.fromkeys(
        v.strip() for v in dados.valores
        if isinstance(v, str) and v.strip()
    ))

    if tipo == "TODOS":
        if dados.valores:
            raise HTTPException(422, "TODOS nao aceita valores")
        valores = [None]
    elif not valores:
        raise HTTPException(422, "Selecione ao menos um valor")

    with _deps["conectar"]() as conn:
        with conn.cursor() as cur:
            p = _registro(cur, pesquisa_id, True)

            if p["status"] != "RASCUNHO":
                raise HTTPException(
                    409, "Publico so pode ser alterado em rascunho"
                )

            if tipo != "TODOS":
                coluna = {
                    "DEPARTAMENTO": "departamento",
                    "CENTRO_CUSTO": "centro_custo",
                    "CARGO": "cargo"
                }[tipo]

                cur.execute(
                    f"""
                    SELECT DISTINCT TRIM({coluna})
                    FROM intranet_usuarios
                    WHERE ativo IS TRUE
                      AND NULLIF(TRIM({coluna}), '') IS NOT NULL
                    """
                )
                disponiveis = {r[0] for r in cur.fetchall()}

                if any(v not in disponiveis for v in valores):
                    raise HTTPException(
                        422, "Um ou mais valores nao estao disponiveis"
                    )

            cur.execute(
                "SELECT tipo, valor FROM pesq_publico WHERE pesquisa_id=%s ORDER BY id",
                (pesquisa_id,)
            )
            anteriores = [
                {"tipo": t, "valor": v} for t, v in cur.fetchall()
            ]

            cur.execute(
                "DELETE FROM pesq_publico WHERE pesquisa_id=%s",
                (pesquisa_id,)
            )

            for valor in valores:
                cur.execute(
                    """
                    INSERT INTO pesq_publico(pesquisa_id,tipo,valor)
                    VALUES (%s,%s,%s)
                    """,
                    (pesquisa_id, tipo, valor)
                )

            cur.execute(
                """
                INSERT INTO pesq_auditoria
                    (pesquisa_id,usuario_id,acao,dados_anteriores,dados_novos)
                VALUES (%s,%s,'EDITAR',%s,%s)
                """,
                (
                    pesquisa_id,
                    u[0],
                    Jsonb({"publico": anteriores}),
                    Jsonb({"tipo": tipo, "valores": valores})
                )
            )

    _auditar(u, "EDITAR", pesquisa_id)

    return {
        "pesquisa_id": pesquisa_id,
        "tipo": tipo,
        "valores": valores
    }

@router_pesquisas.get("/opcoes-publico")
def listar_opcoes_publico(
    authorization: Optional[str] = Header(None)
):
    _usuario(authorization, True)

    resultado = {}

    with _deps["conectar"]() as conn:
        with conn.cursor() as cur:
            for tipo, coluna in {
                "DEPARTAMENTO": "departamento",
                "CENTRO_CUSTO": "centro_custo",
                "CARGO": "cargo"
            }.items():
                cur.execute(
                    f"""
                    SELECT DISTINCT TRIM({coluna})
                    FROM intranet_usuarios
                    WHERE ativo IS TRUE
                      AND NULLIF(TRIM({coluna}), '') IS NOT NULL
                    ORDER BY 1
                    """
                )
                resultado[tipo] = [r[0] for r in cur.fetchall()]

    return resultado

@router_pesquisas.delete("/{pesquisa_id}")
def excluir_pesquisa(pesquisa_id: int, authorization: Optional[str] = Header(None)):
    u = _usuario(authorization, True)

    with _deps["conectar"]() as conn:
        with conn.cursor() as cur:
            p = _registro(cur, pesquisa_id, True)

            if p["status"] != "RASCUNHO":
                raise HTTPException(
                    409,
                    "Somente pesquisas em rascunho podem ser excluidas"
                )

            # Respostas primeiro, respeitando as chaves estrangeiras.
            cur.execute("""
                DELETE FROM pesq_respostas
                WHERE envio_id IN (
                    SELECT id FROM pesq_envios WHERE pesquisa_id = %s
                )
                OR pergunta_id IN (
                    SELECT id FROM pesq_perguntas WHERE pesquisa_id = %s
                )
            """, (pesquisa_id, pesquisa_id))

            cur.execute(
                "DELETE FROM pesq_envios WHERE pesquisa_id = %s",
                (pesquisa_id,)
            )

            cur.execute(
                "DELETE FROM pesq_participacao WHERE pesquisa_id = %s",
                (pesquisa_id,)
            )

            cur.execute("""
                DELETE FROM pesq_alternativas
                WHERE pergunta_id IN (
                    SELECT id FROM pesq_perguntas WHERE pesquisa_id = %s
                )
            """, (pesquisa_id,))

            cur.execute(
                "DELETE FROM pesq_perguntas WHERE pesquisa_id = %s",
                (pesquisa_id,)
            )

            cur.execute(
                "DELETE FROM pesq_publico WHERE pesquisa_id = %s",
                (pesquisa_id,)
            )

            cur.execute(
                "DELETE FROM pesq_auditoria WHERE pesquisa_id = %s",
                (pesquisa_id,)
            )

            cur.execute(
                "DELETE FROM pesq_pesquisas WHERE id = %s",
                (pesquisa_id,)
            )

    _auditar(u, "EXCLUIR", pesquisa_id)

    return {
        "id": pesquisa_id,
        "mensagem": "Pesquisa e registros vinculados excluidos com sucesso"
    }
@router_pesquisas.post("/{pesquisa_id}/publicar")
def publicar(pesquisa_id: int, authorization: Optional[str] = Header(None)):
    u = _usuario(authorization, True)
    with _deps["conectar"]() as conn:
        with conn.cursor() as cur:
            p = _registro(cur, pesquisa_id, True)
            if p["status"] != "RASCUNHO":
                raise HTTPException(409, "Apenas rascunhos podem ser publicados")
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM pesq_perguntas WHERE pesquisa_id=%s)",
                (pesquisa_id,)
            )
            if not cur.fetchone()[0]:
                raise HTTPException(422, "Cadastre pelo menos uma pergunta antes de publicar")

            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM pesq_publico WHERE pesquisa_id=%s)",
                (pesquisa_id,)
            )
            if not cur.fetchone()[0]:
                raise HTTPException(422, "Defina o publico antes de publicar")

            agora = datetime.now(timezone.utc)
            if p["encerramento_em"] and p["encerramento_em"] <= agora:
                raise HTTPException(422, "Prazo de encerramento já passou")
            status = "AGENDADA" if p["inicio_em"] and p["inicio_em"] > agora else "PUBLICADA"
            cur.execute("UPDATE pesq_pesquisas SET status=%s,atualizado_em=NOW() WHERE id=%s", (status, pesquisa_id))
            acao = "AGENDAR" if status == "AGENDADA" else "PUBLICAR"
            cur.execute("INSERT INTO pesq_auditoria(pesquisa_id,usuario_id,acao) VALUES (%s,%s,%s)", (pesquisa_id, u[0], acao))
    _auditar(u, acao, pesquisa_id)
    return {"id": pesquisa_id, "status": status}



@router_pesquisas.get("/minhas")
def listar_minhas_pesquisas(authorization: Optional[str] = Header(None)):
    u = _usuario(authorization)

    with _deps["conectar"]() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    p.id,
                    p.titulo,
                    p.descricao,
                    p.anonima,
                    p.status,
                    p.imagem_capa,
                    p.inicio_em,
                    p.encerramento_em,
                    EXISTS (
                        SELECT 1
                        FROM pesq_participacao e
                        WHERE e.pesquisa_id = p.id
                          AND e.usuario_id = %s
                    ) AS respondida
                FROM pesq_pesquisas p
                WHERE p.status IN ('PUBLICADA', 'ENCERRADA')
                  AND EXISTS (
                      SELECT 1
                      FROM intranet_usuarios u
                      JOIN pesq_publico pub
                        ON pub.pesquisa_id = p.id
                      WHERE u.id = %s
                        AND u.ativo IS TRUE
                        AND (
                            pub.tipo = 'TODOS'
                            OR (
                                pub.tipo = 'DEPARTAMENTO'
                                AND UPPER(TRIM(pub.valor)) = UPPER(TRIM(u.departamento))
                            )
                            OR (
                                pub.tipo = 'CENTRO_CUSTO'
                                AND UPPER(TRIM(pub.valor)) = UPPER(TRIM(u.centro_custo))
                            )
                            OR (
                                pub.tipo = 'CARGO'
                                AND UPPER(TRIM(pub.valor)) = UPPER(TRIM(u.cargo))
                            )
                        )
                  )
                  AND (
                      p.inicio_em IS NULL
                      OR p.inicio_em <= NOW()
                  )
                ORDER BY p.id DESC
            """, (u[0], u[0]))

            resultado = []

            for r in cur.fetchall():
                encerrada = (
                    r[4] == "ENCERRADA"
                    or (
                        r[7] is not None
                        and r[7] <= datetime.now(timezone.utc)
                    )
                )

                situacao = (
                    "answered" if r[8]
                    else "expired" if encerrada
                    else "pending"
                )

                resultado.append({
                    "id": r[0],
                    "titulo": r[1],
                    "descricao": r[2],
                    "anonima": r[3],
                    "status": r[4],
                    "imagem_capa": r[5],
                    "inicio_em": r[6],
                    "encerramento_em": r[7],
                    "respondida": r[8],
                    "situacao": situacao
                })

            return resultado
@router_pesquisas.get("/{pesquisa_id}")
def obter_pesquisa(pesquisa_id: int, authorization: Optional[str] = Header(None)):
    u = _usuario(authorization)
    with _deps["conectar"]() as conn:
        with conn.cursor() as cur:
            p = _registro(cur, pesquisa_id)
            if u[3] != 3 and p["status"] not in ("PUBLICADA", "ENCERRADA"):
                raise HTTPException(404, "Pesquisa não disponível")
            # Colaborador: somente pesquisas publicadas e destinadas a TODOS.
            if u[3] != 3:
                permitido = _publico_permitido(
                    cur, pesquisa_id, u[0]
                )

                if not permitido:
                    raise HTTPException(404, "Pesquisa não disponível")

                if p["status"] != "PUBLICADA":
                    raise HTTPException(404, "Pesquisa não disponível")

                cur.execute("""
                    SELECT inicio_em, encerramento_em
                    FROM pesq_pesquisas
                    WHERE id = %s
                """, (pesquisa_id,))
                inicio, fim = cur.fetchone()

                agora = datetime.now(timezone.utc)

                if (inicio and inicio > agora) or (fim and fim <= agora):
                    raise HTTPException(404, "Pesquisa fora do período disponível")
            cur.execute("SELECT id,enunciado,tipo,ordem,obrigatoria FROM pesq_perguntas WHERE pesquisa_id=%s ORDER BY ordem", (pesquisa_id,))
            perguntas = []
            for qid, enunciado, tipo, ordem, obrigatoria in cur.fetchall():
                cur.execute("SELECT id,texto,ordem FROM pesq_alternativas WHERE pergunta_id=%s ORDER BY ordem", (qid,))
                perguntas.append({"id": qid, "enunciado": enunciado, "tipo": tipo, "ordem": ordem, "obrigatoria": obrigatoria,
                                  "alternativas": [{"id": a, "texto": t, "ordem": o} for a, t, o in cur.fetchall()]})
            p["perguntas"] = perguntas

            if u[3] == 3:
                cur.execute(
                    """
                    SELECT tipo, valor
                    FROM pesq_publico
                    WHERE pesquisa_id = %s
                    ORDER BY id
                    """,
                    (pesquisa_id,)
                )
                p["publico"] = [
                    {"tipo": tipo, "valor": valor}
                    for tipo, valor in cur.fetchall()
                ]

            return p


@router_pesquisas.get("/{pesquisa_id}/minhas-respostas")
def consultar_minhas_respostas(
    pesquisa_id: int,
    authorization: Optional[str] = Header(None)
):
    u = _usuario(authorization)

    with _deps["conectar"]() as conn:
        with conn.cursor() as cur:
            p = _registro(cur, pesquisa_id)

            if p["anonima"]:
                raise HTTPException(
                    403, "Pesquisas anônimas não permitem consulta individual"
                )

            cur.execute("""
                SELECT e.id
                FROM pesq_envios e
                WHERE e.pesquisa_id = %s
                  AND e.usuario_id = %s
            """, (pesquisa_id, u[0]))

            envio = cur.fetchone()

            if not envio:
                raise HTTPException(404, "Respostas não encontradas")

            cur.execute("""
                SELECT
                    q.id,
                    q.enunciado,
                    q.tipo,
                    r.valor
                FROM pesq_respostas r
                JOIN pesq_perguntas q ON q.id = r.pergunta_id
                WHERE r.envio_id = %s
                  AND q.pesquisa_id = %s
                ORDER BY q.ordem, q.id
            """, (envio[0], pesquisa_id))

            respostas = []

            for qid, enunciado, tipo, valor in cur.fetchall():
                if tipo in ("ESCOLHA_UNICA", "MULTIPLA_ESCOLHA"):
                    cur.execute("""
                        SELECT id, texto
                        FROM pesq_alternativas
                        WHERE pergunta_id = %s
                    """, (qid,))
                    alternativas = dict(cur.fetchall())

                    if tipo == "ESCOLHA_UNICA":
                        valor_exibicao = alternativas.get(valor, valor)
                    else:
                        valor_exibicao = [
                            alternativas.get(v, v)
                            for v in valor
                        ]
                else:
                    valor_exibicao = valor

                respostas.append({
                    "pergunta_id": qid,
                    "pergunta": enunciado,
                    "tipo": tipo,
                    "resposta": valor_exibicao
                })

            return {
                "pesquisa_id": pesquisa_id,
                "titulo": p["titulo"],
                "anonima": False,
                "somente_leitura": True,
                "respostas": respostas
            }

@router_pesquisas.post("/{pesquisa_id}/enviar")
def enviar_respostas(pesquisa_id: int, dados: Envio, authorization: Optional[str] = Header(None)):
    u = _usuario(authorization)
    usuario_id = u[0]

    with _deps["conectar"]() as conn:
        with conn.cursor() as cur:
            p = _registro(cur, pesquisa_id, True)

            agora = datetime.now(timezone.utc)

            if p["status"] != "PUBLICADA":
                raise HTTPException(409, "Pesquisa não está publicada")

            if (p["inicio_em"] and p["inicio_em"] > agora) or (
                p["encerramento_em"] and p["encerramento_em"] <= agora
            ):
                raise HTTPException(409, "Pesquisa fora do período")

            if not _publico_permitido(cur, pesquisa_id, usuario_id):
                raise HTTPException(403, "Pesquisa não disponível para este colaborador")

            cur.execute("""
                SELECT 1 FROM pesq_participacao
                WHERE pesquisa_id = %s AND usuario_id = %s
            """, (pesquisa_id, usuario_id))

            if cur.fetchone():
                raise HTTPException(409, "Pesquisa já respondida")

            cur.execute("""
                SELECT id, tipo, obrigatoria
                FROM pesq_perguntas
                WHERE pesquisa_id = %s
            """, (pesquisa_id,))

            perguntas = {
                r[0]: {"tipo": r[1], "obrigatoria": r[2]}
                for r in cur.fetchall()
            }

            if not perguntas:
                raise HTTPException(422, "Pesquisa sem perguntas")

            respostas = {}

            for resposta in dados.respostas:
                qid = resposta.pergunta_id
                valor = resposta.valor

                if qid not in perguntas:
                    raise HTTPException(422, "Pergunta inválida")

                if qid in respostas:
                    raise HTTPException(422, "Pergunta duplicada")

                tipo = perguntas[qid]["tipo"]

                if tipo in ("NOTA_1_5", "NOTA_0_10"):
                    minimo, maximo = (1, 5) if tipo == "NOTA_1_5" else (0, 10)
                    if type(valor) is not int or not minimo <= valor <= maximo:
                        raise HTTPException(422, "Nota inválida")

                elif tipo == "TEXTO":
                    if not isinstance(valor, str) or len(valor) > 5000:
                        raise HTTPException(422, "Texto inválido")

                elif tipo == "SIM_NAO":
                    if type(valor) is not bool:
                        raise HTTPException(422, "Resposta SIM/NÃO inválida")

                elif tipo in ("ESCOLHA_UNICA", "MULTIPLA_ESCOLHA"):
                    cur.execute(
                        "SELECT id FROM pesq_alternativas WHERE pergunta_id = %s",
                        (qid,)
                    )
                    alternativas = {r[0] for r in cur.fetchall()}

                    valores = [valor] if tipo == "ESCOLHA_UNICA" else valor

                    if (
                        not isinstance(valores, list)
                        or not valores
                        or any(type(v) is not int or v not in alternativas for v in valores)
                        or len(set(valores)) != len(valores)
                        or (tipo == "ESCOLHA_UNICA" and len(valores) != 1)
                    ):
                        raise HTTPException(422, "Alternativa inválida")

                else:
                    raise HTTPException(422, "Tipo de pergunta não suportado")

                respostas[qid] = valor

            for qid, pergunta in perguntas.items():
                if pergunta["obrigatoria"]:
                    if qid not in respostas or respostas[qid] is None or respostas[qid] == "":
                        raise HTTPException(422, "Existem perguntas obrigatórias sem resposta")

            cur.execute("""
                INSERT INTO pesq_participacao (pesquisa_id, usuario_id)
                VALUES (%s, %s)
                ON CONFLICT (pesquisa_id, usuario_id) DO NOTHING
                RETURNING id
            """, (pesquisa_id, usuario_id))

            if not cur.fetchone():
                raise HTTPException(409, "Pesquisa já respondida")

            usuario_envio = None if p["anonima"] else usuario_id

            cur.execute("""
                INSERT INTO pesq_envios (pesquisa_id, usuario_id)
                VALUES (%s, %s)
                RETURNING id
            """, (pesquisa_id, usuario_envio))

            envio_id = cur.fetchone()[0]

            for qid, valor in respostas.items():
                cur.execute("""
                    INSERT INTO pesq_respostas
                    (envio_id, pergunta_id, valor)
                    VALUES (%s, %s, %s)
                """, (envio_id, qid, Jsonb(valor)))

    return {"ok": True, "mensagem": "Pesquisa respondida com sucesso"}




