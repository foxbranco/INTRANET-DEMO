from pathlib import Path

from fastapi import APIRouter, HTTPException, Header, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional
from PIL import Image

router_galeria = APIRouter(prefix="/api/galeria", tags=["Galeria"])

PASTA_GALERIA = Path("/intranet/documentos_upload/galeria")
LIMITE_IMAGEM = 10 * 1024 * 1024

EXTENSOES_PERMITIDAS = {".jpg", ".jpeg", ".png", ".webp"}
FORMATOS_PERMITIDOS = {"JPEG", "PNG", "WEBP"}


def configurar_galeria(conectar, autenticar, registrar_auditoria):
    """Recebe as funcoes existentes da API principal."""
    router_galeria.state_conectar = conectar
    router_galeria.state_autenticar = autenticar
    router_galeria.state_auditoria = registrar_auditoria


def exigir_administrador(authorization):
    autenticar = router_galeria.state_autenticar
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatoria")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")

    return usuario


@router_galeria.get("")
def listar_galeria(authorization: Optional[str] = Header(None)):
    usuario = router_galeria.state_autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatoria")

    with router_galeria.state_conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, titulo, arquivo, ordem
                FROM intranet_galeria
                WHERE publicado = TRUE
                ORDER BY ordem, id
            """)

            registros = cur.fetchall()

    return [
        {
            "id": r[0],
            "titulo": r[1],
            "arquivo": r[2],
            "ordem": r[3]
        }
        for r in registros
    ]


@router_galeria.get("/admin")
def listar_galeria_admin(
    authorization: Optional[str] = Header(None)
):
    exigir_administrador(authorization)

    with router_galeria.state_conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, titulo, arquivo, ordem,
                       publicado, criado_em, atualizado_em
                FROM intranet_galeria
                ORDER BY ordem, id
            """)

            registros = cur.fetchall()

    return [
        {
            "id": r[0],
            "titulo": r[1],
            "arquivo": r[2],
            "ordem": r[3],
            "publicado": r[4],
            "criado_em": r[5].isoformat(),
            "atualizado_em": r[6].isoformat()
        }
        for r in registros
    ]


@router_galeria.post("/upload", status_code=201)
async def upload_imagem(
    titulo: str = Form(...),
    arquivo: UploadFile = File(...),
    authorization: Optional[str] = Header(None)
):
    import io
    import uuid

    usuario = exigir_administrador(authorization)
    titulo = titulo.strip()

    if not titulo or len(titulo) > 200:
        raise HTTPException(400, "Titulo invalido")

    conteudo = await arquivo.read(LIMITE_IMAGEM + 1)
    await arquivo.close()

    if len(conteudo) > LIMITE_IMAGEM:
        raise HTTPException(413, "Imagem excede 10 MB")

    try:
        with Image.open(io.BytesIO(conteudo)) as imagem:
            formato = imagem.format
            imagem.verify()
    except Exception:
        raise HTTPException(400, "Arquivo nao e uma imagem valida")

    if formato not in FORMATOS_PERMITIDOS:
        raise HTTPException(400, "Formato nao permitido")

    extensao = {
        "JPEG": ".jpg",
        "PNG": ".png",
        "WEBP": ".webp"
    }[formato]

    PASTA_GALERIA.mkdir(parents=True, exist_ok=True)

    nome = f"{uuid.uuid4().hex}{extensao}"
    destino = PASTA_GALERIA / nome

    try:
        destino.write_bytes(conteudo)

        with router_galeria.state_conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intranet_galeria
                        (titulo, arquivo, ordem, publicado, criado_por)
                    VALUES (
                        %s, %s,
                        (SELECT COALESCE(MAX(ordem), 0) + 1
                         FROM intranet_galeria),
                        TRUE, %s
                    )
                    RETURNING id
                """, (titulo, nome, str(usuario[0])))

                imagem_id = cur.fetchone()[0]

    except Exception:
        destino.unlink(missing_ok=True)
        raise

    router_galeria.state_auditoria(
        router_galeria.state_conectar,
        usuario[0],
        usuario[6],
        "ADMINISTRACAO",
        "CRIAR_IMAGEM",
        "GALERIA",
        "SUCESSO",
        recurso="IMAGEM",
        recurso_id=str(imagem_id)
    )

    return {
        "id": imagem_id,
        "titulo": titulo,
        "arquivo": nome,
        "mensagem": "Imagem publicada com sucesso"
    }


class AtualizarImagemRequest(BaseModel):
    titulo: str
    publicado: bool


@router_galeria.put("/{imagem_id}")
def atualizar_imagem(
    imagem_id: int,
    dados: AtualizarImagemRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = exigir_administrador(authorization)
    titulo = dados.titulo.strip()

    if not titulo or len(titulo) > 200:
        raise HTTPException(400, "Titulo invalido")

    with router_galeria.state_conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE intranet_galeria
                SET titulo = %s,
                    publicado = %s,
                    atualizado_em = NOW()
                WHERE id = %s
                RETURNING id
            """, (titulo, dados.publicado, imagem_id))

            if not cur.fetchone():
                raise HTTPException(404, "Imagem nao encontrada")

    router_galeria.state_auditoria(
        router_galeria.state_conectar,
        usuario[0],
        usuario[6],
        "ADMINISTRACAO",
        "ATUALIZAR_IMAGEM",
        "GALERIA",
        "SUCESSO",
        recurso="IMAGEM",
        recurso_id=str(imagem_id),
        detalhes={"publicado": dados.publicado}
    )

    return {
        "id": imagem_id,
        "titulo": titulo,
        "publicado": dados.publicado,
        "mensagem": "Imagem atualizada com sucesso"
    }


class OrdenarImagemRequest(BaseModel):
    posicao: int


@router_galeria.put("/{imagem_id}/ordem")
def ordenar_imagem(
    imagem_id: int,
    dados: OrdenarImagemRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = exigir_administrador(authorization)

    with router_galeria.state_conectar() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM intranet_galeria ORDER BY ordem, id FOR UPDATE"
            )
            ids = [r[0] for r in cur.fetchall()]

            if imagem_id not in ids:
                raise HTTPException(404, "Imagem nao encontrada")

            if dados.posicao < 1 or dados.posicao > len(ids):
                raise HTTPException(400, "Posicao invalida")

            ids.remove(imagem_id)
            ids.insert(dados.posicao - 1, imagem_id)

            for posicao, id_atual in enumerate(ids, start=1):
                cur.execute("""
                    UPDATE intranet_galeria
                    SET ordem = %s, atualizado_em = NOW()
                    WHERE id = %s
                """, (posicao, id_atual))

    router_galeria.state_auditoria(
        router_galeria.state_conectar,
        usuario[0],
        usuario[6],
        "ADMINISTRACAO",
        "ORDENAR_IMAGEM",
        "GALERIA",
        "SUCESSO",
        recurso="IMAGEM",
        recurso_id=str(imagem_id),
        detalhes={"posicao": dados.posicao}
    )

    return {
        "id": imagem_id,
        "posicao": dados.posicao,
        "mensagem": "Ordem atualizada com sucesso"
    }


@router_galeria.delete("/{imagem_id}")
def excluir_imagem(
    imagem_id: int,
    authorization: Optional[str] = Header(None)
):
    usuario = exigir_administrador(authorization)

    with router_galeria.state_conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT arquivo
                FROM intranet_galeria
                WHERE id = %s
                FOR UPDATE
            """, (imagem_id,))

            registro = cur.fetchone()

            if not registro:
                raise HTTPException(404, "Imagem nao encontrada")

            nome_arquivo = registro[0]

            cur.execute(
                "DELETE FROM intranet_galeria WHERE id = %s",
                (imagem_id,)
            )

    router_galeria.state_auditoria(
        router_galeria.state_conectar,
        usuario[0],
        usuario[6],
        "ADMINISTRACAO",
        "EXCLUIR_IMAGEM",
        "GALERIA",
        "SUCESSO",
        recurso="IMAGEM",
        recurso_id=str(imagem_id)
    )

    caminho = PASTA_GALERIA / Path(nome_arquivo).name

    try:
        caminho.unlink(missing_ok=True)
    except OSError:
        pass

    return {
        "id": imagem_id,
        "mensagem": "Imagem excluida com sucesso"
    }


@router_galeria.put("/{imagem_id}/substituir")
async def substituir_imagem(
    imagem_id: int,
    arquivo: UploadFile = File(...),
    authorization: Optional[str] = Header(None)
):
    import io
    import uuid

    usuario = exigir_administrador(authorization)

    conteudo = await arquivo.read(LIMITE_IMAGEM + 1)
    await arquivo.close()

    if not conteudo:
        raise HTTPException(400, "Arquivo vazio")

    if len(conteudo) > LIMITE_IMAGEM:
        raise HTTPException(413, "Imagem excede 10 MB")

    try:
        with Image.open(io.BytesIO(conteudo)) as imagem:
            formato = imagem.format
            imagem.verify()
    except Exception:
        raise HTTPException(400, "Imagem invalida")

    if formato not in FORMATOS_PERMITIDOS:
        raise HTTPException(400, "Formato nao permitido")

    extensao = {
        "JPEG": ".jpg",
        "PNG": ".png",
        "WEBP": ".webp"
    }[formato]

    PASTA_GALERIA.mkdir(parents=True, exist_ok=True)

    novo_nome = f"{uuid.uuid4().hex}{extensao}"
    novo_caminho = PASTA_GALERIA / novo_nome

    with router_galeria.state_conectar() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT arquivo FROM intranet_galeria WHERE id = %s FOR UPDATE",
                (imagem_id,)
            )
            registro = cur.fetchone()

            if not registro:
                raise HTTPException(404, "Imagem nao encontrada")

            nome_anterior = registro[0]

            try:
                novo_caminho.write_bytes(conteudo)

                cur.execute("""
                    UPDATE intranet_galeria
                    SET arquivo = %s, atualizado_em = NOW()
                    WHERE id = %s
                """, (novo_nome, imagem_id))

            except Exception:
                novo_caminho.unlink(missing_ok=True)
                raise

    try:
        router_galeria.state_auditoria(
            router_galeria.state_conectar,
            usuario[0],
            usuario[6],
            "ADMINISTRACAO",
            "SUBSTITUIR_IMAGEM",
            "GALERIA",
            "SUCESSO",
            recurso="IMAGEM",
            recurso_id=str(imagem_id)
        )
    except Exception:
        pass

    caminho_anterior = PASTA_GALERIA / Path(nome_anterior).name

    if caminho_anterior != novo_caminho:
        try:
            caminho_anterior.unlink(missing_ok=True)
        except OSError:
            pass

    return {
        "id": imagem_id,
        "arquivo": novo_nome,
        "mensagem": "Imagem substituida com sucesso"
    }

# VISUALIZACAO PROTEGIDA DE IMAGENS
@router_galeria.get("/{imagem_id}/imagem")
def visualizar_imagem(
    imagem_id: int,
    authorization: Optional[str] = Header(None)
):
    from fastapi.responses import FileResponse

    usuario = router_galeria.state_autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatoria")

    with router_galeria.state_conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT arquivo, publicado
                FROM intranet_galeria
                WHERE id = %s
            """, (imagem_id,))
            registro = cur.fetchone()

    if not registro:
        raise HTTPException(404, "Imagem nao encontrada")

    nome_arquivo, publicado = registro

    if not publicado and usuario[3] != 3:
        raise HTTPException(403, "Imagem nao publicada")

    caminho = PASTA_GALERIA / Path(nome_arquivo).name

    if not caminho.is_file():
        raise HTTPException(404, "Arquivo nao encontrado")

    return FileResponse(
        path=str(caminho),
        headers={"Cache-Control": "private, no-store"}
    )
