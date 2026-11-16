from datetime import datetime, date
from typing import Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field


class ComunicadoRequest(BaseModel):
    titulo: str = Field(min_length=1, max_length=200)
    categoria: str = Field(min_length=1, max_length=100)
    resumo: Optional[str] = None
    conteudo: str = Field(min_length=1)
    imagem_url: Optional[str] = None
    anexo_url: Optional[str] = None
    destinatarios: list[str] = Field(default_factory=lambda: ["TODOS"])
    destaque: bool = False
    permitir_curtidas: bool = True
    permitir_comentarios: bool = True
    data_inicio: Optional[datetime] = None
    data_fim: Optional[datetime] = None
    publicado: bool = False
    ativo: bool = True
    comunicado_do_mes: Optional[bool] = None
    mes_referencia: Optional[date] = None

def listar_comunicados_publicados(conectar):
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id, titulo, categoria, resumo, conteudo,
                    imagem_url, destaque, comunicado_do_mes, mes_referencia, permitir_curtidas,
                    permitir_comentarios, visualizacoes,
                    data_inicio, data_fim, criado_em
                FROM intranet_comunicados
                WHERE ativo = TRUE
                  AND publicado = TRUE
                  AND (data_inicio IS NULL OR data_inicio <= NOW())
                  AND (data_fim IS NULL OR data_fim >= NOW())
                ORDER BY
                    COALESCE(data_inicio, criado_em) DESC,
                    id DESC
            """)

            colunas = [c.name for c in cur.description]
            return [
                dict(zip(colunas, registro))
                for registro in cur.fetchall()
            ]

def listar_comunicados_admin(conectar):
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id, titulo, categoria, resumo, conteudo,
                    imagem_url, anexo_url, destaque, comunicado_do_mes, mes_referencia,
                    permitir_curtidas, permitir_comentarios,
                    visualizacoes, data_inicio, data_fim,
                    publicado, ativo, criado_por, atualizado_por,
                    criado_em, atualizado_em,
                    CASE
                        WHEN ativo = FALSE THEN 'INATIVO'
                        WHEN publicado = FALSE THEN 'RASCUNHO'
                        WHEN data_inicio > NOW() THEN 'AGENDADO'
                        WHEN data_fim < NOW() THEN 'ENCERRADO'
                        ELSE 'PUBLICADO'
                    END AS status
                FROM intranet_comunicados
                ORDER BY COALESCE(data_inicio, criado_em) DESC, id DESC
            """)

            colunas = [c.name for c in cur.description]
            return [
                dict(zip(colunas, registro))
                for registro in cur.fetchall()
            ]

def _validar_mes(dados):
    if dados.data_inicio and dados.data_fim and dados.data_fim < dados.data_inicio:
        raise HTTPException(400, "Data final anterior à data inicial")
    if dados.mes_referencia and dados.mes_referencia.day != 1:
        raise HTTPException(400, "O mês de referência deve ser o primeiro dia do mês")
    if dados.comunicado_do_mes is True:
        if not dados.mes_referencia:
            raise HTTPException(400, "Informe o mês de referência do Comunicado do Mês")
        if not dados.publicado or not dados.ativo:
            raise HTTPException(400, "O Comunicado do Mês precisa estar publicado e ativo")


def _reservar_mes(cur, mes, comunicado_id):
    # Serializa seleções simultâneas para o mesmo mês.
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("comunicado_mes:" + mes.isoformat(),))
    cur.execute("""
        UPDATE intranet_comunicados
        SET comunicado_do_mes = FALSE, mes_referencia = NULL
        WHERE comunicado_do_mes = TRUE AND mes_referencia = %s
          AND id <> %s
    """, (mes, comunicado_id))


def criar_comunicado(conectar, dados: ComunicadoRequest, usuario_id: int):
    _validar_mes(dados)
    with conectar() as conn:
        with conn.cursor() as cur:
            if dados.comunicado_do_mes:
                _reservar_mes(cur, dados.mes_referencia, -1)
            cur.execute("""
                INSERT INTO intranet_comunicados (
                    titulo, categoria, resumo, conteudo,
                    imagem_url, anexo_url, destinatarios,
                    destaque, permitir_curtidas, permitir_comentarios,
                    data_inicio, data_fim, publicado, ativo,
                    criado_por, atualizado_por, comunicado_do_mes, mes_referencia
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s::jsonb,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                ) RETURNING id
            """, (
                dados.titulo, dados.categoria, dados.resumo, dados.conteudo,
                dados.imagem_url, dados.anexo_url, '["TODOS"]',
                dados.destaque, dados.permitir_curtidas, dados.permitir_comentarios,
                dados.data_inicio, dados.data_fim, dados.publicado, dados.ativo,
                usuario_id, usuario_id, dados.comunicado_do_mes is True,
                dados.mes_referencia if dados.comunicado_do_mes else None
            ))
            novo_id = cur.fetchone()[0]
    return {"id": novo_id, "mensagem": "Comunicado cadastrado com sucesso"}


def atualizar_comunicado(conectar, comunicado_id: int, dados: ComunicadoRequest, usuario_id: int):
    _validar_mes(dados)
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT comunicado_do_mes, mes_referencia
                FROM intranet_comunicados WHERE id = %s FOR UPDATE
            """, (comunicado_id,))
            anterior = cur.fetchone()
            if anterior is None:
                raise HTTPException(404, "Comunicado não encontrado")

            # Requisições antigas sem os novos campos não apagam a seleção mensal.
            selecionado = anterior[0] if dados.comunicado_do_mes is None else dados.comunicado_do_mes
            mes = dados.mes_referencia if dados.mes_referencia is not None else anterior[1]
            if not dados.publicado or not dados.ativo:
                selecionado = False
            if selecionado and not mes:
                raise HTTPException(400, "Informe o mês de referência do Comunicado do Mês")
            if selecionado and mes.day != 1:
                raise HTTPException(400, "O mês de referência deve ser o primeiro dia do mês")
            if selecionado:
                _reservar_mes(cur, mes, comunicado_id)

            cur.execute("""
                UPDATE intranet_comunicados
                SET titulo = %s,
                    categoria = %s,
                    resumo = %s,
                    conteudo = %s,
                    imagem_url = COALESCE(NULLIF(%s, ''), imagem_url),
                    anexo_url = %s,
                    destinatarios = '["TODOS"]'::jsonb,
                    destaque = %s,
                    permitir_curtidas = %s,
                    permitir_comentarios = %s,
                    data_inicio = %s,
                    data_fim = %s,
                    publicado = %s,
                    ativo = %s,
                    comunicado_do_mes = %s,
                    mes_referencia = %s,
                    atualizado_por = %s,
                    atualizado_em = NOW()
                WHERE id = %s RETURNING id
            """, (
                dados.titulo, dados.categoria, dados.resumo, dados.conteudo,
                dados.imagem_url, dados.anexo_url, dados.destaque,
                dados.permitir_curtidas, dados.permitir_comentarios,
                dados.data_inicio, dados.data_fim, dados.publicado, dados.ativo,
                selecionado, mes if selecionado else None, usuario_id, comunicado_id
            ))
            if cur.fetchone() is None:
                raise HTTPException(404, "Comunicado não encontrado")
    return {"id": comunicado_id, "mensagem": "Comunicado atualizado com sucesso"}
