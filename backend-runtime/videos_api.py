import re
from urllib.parse import urlparse
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel

router_videos = APIRouter(prefix="/api/videos", tags=["Videos"])


def configurar_videos(conectar, autenticar, registrar_auditoria):
    router_videos.state_conectar = conectar
    router_videos.state_autenticar = autenticar
    router_videos.state_auditoria = registrar_auditoria


def exigir_administrador(authorization):
    usuario = router_videos.state_autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatoria")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")

    return usuario


def extrair_url_instagram(conteudo):
    if not conteudo or len(conteudo) > 20000:
        raise HTTPException(400, "Informe um link ou codigo Embed valido")

    padrao = r'https?://(?:www\.)?instagram\.com/(?:reel|p)/[A-Za-z0-9_-]+/?'
    encontrados = re.findall(padrao, conteudo, re.IGNORECASE)

    if not encontrados:
        raise HTTPException(400, "Codigo Embed sem link valido do Instagram")

    url = encontrados[0]
    partes = urlparse(url)

    if partes.hostname not in ("instagram.com", "www.instagram.com"):
        raise HTTPException(400, "Endereco Instagram invalido")

    return f"https://www.instagram.com{partes.path.rstrip('/')}/"


def registrar_evento(usuario, acao, video_id, detalhes=None):
    try:
        router_videos.state_auditoria(
            router_videos.state_conectar,
            usuario[0],
            usuario[6],
            "ADMINISTRACAO",
            acao,
            "VIDEOS",
            "SUCESSO",
            recurso="VIDEO",
            recurso_id=str(video_id),
            detalhes=detalhes or {}
        )
    except Exception:
        pass


class CriarVideoRequest(BaseModel):
    titulo: str
    embed_html: str


class AtualizarVideoRequest(BaseModel):
    titulo: str
    embed_html: str
    publicado: bool


class OrdenarVideoRequest(BaseModel):
    posicao: int


@router_videos.get("")
def listar_videos(authorization: Optional[str] = Header(None)):
    usuario = router_videos.state_autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatoria")

    with router_videos.state_conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, titulo, instagram_url, ordem
                FROM intranet_videos
                WHERE publicado = TRUE
                ORDER BY ordem, id
            """)
            registros = cur.fetchall()

    return [
        {
            "id": r[0],
            "titulo": r[1],
            "instagram_url": r[2],
            "ordem": r[3]
        }
        for r in registros
    ]


@router_videos.get("/admin")
def listar_videos_admin(authorization: Optional[str] = Header(None)):
    exigir_administrador(authorization)

    with router_videos.state_conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, titulo, instagram_url, ordem,
                       publicado, criado_em, atualizado_em
                FROM intranet_videos
                ORDER BY ordem, id
            """)
            registros = cur.fetchall()

    return [
        {
            "id": r[0],
            "titulo": r[1],
            "instagram_url": r[2],
            "ordem": r[3],
            "publicado": r[4],
            "criado_em": r[5].isoformat(),
            "atualizado_em": r[6].isoformat()
        }
        for r in registros
    ]


@router_videos.post("", status_code=201)
def criar_video(
    dados: CriarVideoRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = exigir_administrador(authorization)
    titulo = dados.titulo.strip()
    url = extrair_url_instagram(dados.embed_html)

    if not titulo or len(titulo) > 200:
        raise HTTPException(400, "Titulo invalido")

    with router_videos.state_conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO intranet_videos
                    (titulo, instagram_url, ordem, publicado, criado_por)
                VALUES (
                    %s, %s,
                    (SELECT COALESCE(MAX(ordem), 0) + 1
                     FROM intranet_videos),
                    TRUE, %s
                )
                RETURNING id
            """, (titulo, url, str(usuario[0])))
            video_id = cur.fetchone()[0]

    registrar_evento(usuario, "CRIAR_VIDEO", video_id)

    return {
        "id": video_id,
        "titulo": titulo,
        "instagram_url": url,
        "mensagem": "Video cadastrado com sucesso"
    }


@router_videos.put("/{video_id}")
def atualizar_video(
    video_id: int,
    dados: AtualizarVideoRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = exigir_administrador(authorization)
    titulo = dados.titulo.strip()
    url = extrair_url_instagram(dados.embed_html)

    if not titulo or len(titulo) > 200:
        raise HTTPException(400, "Titulo invalido")

    with router_videos.state_conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE intranet_videos
                SET titulo = %s,
                    instagram_url = %s,
                    publicado = %s,
                    atualizado_em = NOW()
                WHERE id = %s
                RETURNING id
            """, (titulo, url, dados.publicado, video_id))

            if not cur.fetchone():
                raise HTTPException(404, "Video nao encontrado")

    registrar_evento(
        usuario, "ATUALIZAR_VIDEO", video_id,
        {"publicado": dados.publicado}
    )

    return {
        "id": video_id,
        "titulo": titulo,
        "instagram_url": url,
        "publicado": dados.publicado,
        "mensagem": "Video atualizado com sucesso"
    }


@router_videos.put("/{video_id}/ordem")
def ordenar_video(
    video_id: int,
    dados: OrdenarVideoRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = exigir_administrador(authorization)

    with router_videos.state_conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM intranet_videos
                ORDER BY ordem, id FOR UPDATE
            """)
            ids = [r[0] for r in cur.fetchall()]

            if video_id not in ids:
                raise HTTPException(404, "Video nao encontrado")

            if dados.posicao < 1 or dados.posicao > len(ids):
                raise HTTPException(400, "Posicao invalida")

            ids.remove(video_id)
            ids.insert(dados.posicao - 1, video_id)

            for posicao, id_atual in enumerate(ids, start=1):
                cur.execute("""
                    UPDATE intranet_videos
                    SET ordem = %s, atualizado_em = NOW()
                    WHERE id = %s
                """, (posicao, id_atual))

    registrar_evento(
        usuario, "ORDENAR_VIDEO", video_id,
        {"posicao": dados.posicao}
    )

    return {
        "id": video_id,
        "posicao": dados.posicao,
        "mensagem": "Ordem atualizada com sucesso"
    }


@router_videos.delete("/{video_id}")
def excluir_video(
    video_id: int,
    authorization: Optional[str] = Header(None)
):
    usuario = exigir_administrador(authorization)

    with router_videos.state_conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM intranet_videos
                WHERE id = %s
                RETURNING id
            """, (video_id,))

            if not cur.fetchone():
                raise HTTPException(404, "Video nao encontrado")

            cur.execute("""
                SELECT id FROM intranet_videos
                ORDER BY ordem, id FOR UPDATE
            """)
            ids = [r[0] for r in cur.fetchall()]

            for posicao, id_atual in enumerate(ids, start=1):
                cur.execute("""
                    UPDATE intranet_videos
                    SET ordem = %s, atualizado_em = NOW()
                    WHERE id = %s
                """, (posicao, id_atual))

    registrar_evento(usuario, "EXCLUIR_VIDEO", video_id)

    return {
        "id": video_id,
        "mensagem": "Video excluido com sucesso"
    }