from auditoria_api import registrar_auditoria
import os
import re
import secrets
import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import bcrypt
import psycopg
from fastapi import FastAPI, HTTPException, Header, Form, File, UploadFile
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="API Intranet Megavig")

HORAS_SESSAO = 8


class LoginRequest(BaseModel):
    cpf: str
    senha: str


class TrocarSenhaRequest(BaseModel):
    senha_atual: str
    nova_senha: str
class NavegacaoRequest(BaseModel):
    modulo: str



def conectar():
    return psycopg.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        connect_timeout=5
    )


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def autenticar(authorization: Optional[str]):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sessão não informada")

    token = authorization[7:]
    if not token:
        raise HTTPException(401, "Sessão inválida")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.cpf, u.nome, u.nivel_acesso,
                       u.tipo, u.trocar_senha, s.id
                FROM intranet_sessoes s
                JOIN intranet_usuarios u ON u.id = s.usuario_id
                WHERE s.token_hash = %s
                  AND s.revogado_em IS NULL
                  AND s.expira_em > NOW()
                  AND u.ativo = TRUE
            """, (hash_token(token),))
            usuario = cur.fetchone()

    if not usuario:
        raise HTTPException(401, "Sessão inválida ou expirada")

    return usuario


@app.get("/api/health")
def health():
    return {"status": "online"}
@app.post("/api/auditoria/navegacao")
def registrar_navegacao(
    dados: NavegacaoRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    modulos = {

        "comunicados": 1,
        "documentos": 2,
        "cursos": 1,
        "videos": 1,
        "galeria": 1,
        "links": 1,
        "powerbi": 2,
        "avaliacao": 2,
        "admin": 3
    }

    modulo = dados.modulo.strip().lower()

    if modulo not in modulos:
        raise HTTPException(400, "Módulo inválido")

    if usuario[3] < modulos[modulo]:
        raise HTTPException(403, "Acesso não autorizado")

    registrar_auditoria(
        conectar,
        usuario[0],
        usuario[6],
        "NAVEGACAO",
        "ACESSO_MODULO",
        modulo.upper(),
        "SUCESSO"
    )

    return {"success": True}



@app.post("/api/login")
def login(dados: LoginRequest):
    cpf = re.sub(r"\D", "", dados.cpf)

    if len(cpf) != 11:
        registrar_auditoria(
            conectar, None, None,
            "AUTENTICACAO", "LOGIN_NEGADO",
            "LOGIN", "NEGADO"
        )
        raise HTTPException(401, "CPF ou senha inválidos")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, cpf, nome, senha_hash,
                       nivel_acesso, tipo, ativo, trocar_senha
                FROM intranet_usuarios
                WHERE cpf = %s
            """, (cpf,))
            usuario = cur.fetchone()

            if not usuario:
                registrar_auditoria(
                    conectar, None, None,
                    "AUTENTICACAO", "LOGIN_NEGADO",
                    "LOGIN", "NEGADO"
                )
                raise HTTPException(401, "CPF ou senha inválidos")

            (uid, cpf, nome, senha_hash,
             nivel, tipo, ativo, trocar_senha) = usuario

            if not senha_hash or not bcrypt.checkpw(
                dados.senha.encode(),
                senha_hash.encode()
            ):
                registrar_auditoria(
                    conectar, uid, None,
                    "AUTENTICACAO", "LOGIN_NEGADO",
                    "LOGIN", "NEGADO"
                )
                raise HTTPException(401, "CPF ou senha inválidos")

            if not ativo:
                registrar_auditoria(
                    conectar, uid, None,
                    "AUTENTICACAO", "LOGIN_NEGADO",
                    "LOGIN", "NEGADO"
                )
                raise HTTPException(403, "Usuário inativo")

            token = secrets.token_urlsafe(48)

            cur.execute("""
                INSERT INTO intranet_sessoes
                    (usuario_id, token_hash, expira_em)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (
                uid,
                hash_token(token),
                datetime.now(timezone.utc) +
                timedelta(hours=HORAS_SESSAO)
            ))
            sessao_id = cur.fetchone()[0]

            cur.execute("""
                INSERT INTO intranet_auditoria (
                    usuario_id, sessao_id, tipo_evento,
                    acao, modulo, resultado
                )
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                uid, str(sessao_id), "AUTENTICACAO",
                "LOGIN", "LOGIN", "SUCESSO"
            ))

            cur.execute("""
                UPDATE intranet_usuarios
                SET ultimo_acesso = NOW()
                WHERE id = %s
            """, (uid,))

    return {
        "autenticado": True,
        "token": token,
        "tipo_token": "Bearer",
        "trocar_senha": trocar_senha,
        "usuario": {
            "id": uid,
            "nome": nome,
            "nivel_acesso": nivel,
            "tipo": tipo
        }
    }


@app.get("/api/me")
def meu_usuario(authorization: Optional[str] = Header(None)):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, matricula, cpf, nome, email, telefone,
                       nivel_acesso, tipo, data_nascimento,
                       data_admissao, data_demissao,
                       centro_custo, departamento, cargo, foto_perfil
                FROM intranet_usuarios
                WHERE id = %s AND ativo = TRUE
            """, (usuario[0],))

            registro = cur.fetchone()
            colunas = [col.name for col in cur.description]

    if registro is None:
        raise HTTPException(404, "Usuário não encontrado")

    return {
        chave: valor.isoformat() if hasattr(valor, "isoformat") else valor
        for chave, valor in zip(colunas, registro)
    }

# FOTO DE PERFIL - V11

@app.post("/api/perfil/foto")
async def salvar_foto_perfil(
    arquivo: UploadFile = File(...),
    authorization: Optional[str] = Header(None)
):
    import uuid
    import struct
    from pathlib import Path

    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    limite = 2 * 1024 * 1024

    try:
        conteudo = await arquivo.read(limite + 1)

        if not conteudo:
            raise HTTPException(400, "Arquivo vazio")

        if len(conteudo) > limite:
            raise HTTPException(413, "Imagem excede 2 MB")

        extensao = Path(arquivo.filename or "").suffix.lower()

        tipos = {
            ".jpg": "jpeg",
            ".jpeg": "jpeg",
            ".png": "png",
            ".webp": "webp"
        }

        if extensao not in tipos:
            raise HTTPException(400, "Formato não permitido")

        tipo_real = None

        if conteudo.startswith(b"\xff\xd8\xff"):
            tipo_real = "jpeg"

        elif conteudo.startswith(b"\x89PNG\r\n\x1a\n") and len(conteudo) >= 24:
            largura, altura = struct.unpack(">II", conteudo[16:24])
            if largura > 0 and altura > 0:
                tipo_real = "png"

        elif (
            len(conteudo) >= 16
            and conteudo[:4] == b"RIFF"
            and conteudo[8:12] == b"WEBP"
            and conteudo[12:16] in (b"VP8 ", b"VP8L", b"VP8X")
        ):
            tipo_real = "webp"

        if tipo_real != tipos[extensao]:
            raise HTTPException(400, "Conteúdo da imagem inválido")

        pasta = Path("/intranet/fotos_perfil")
        pasta.mkdir(parents=True, exist_ok=True)

        nome = uuid.uuid4().hex + "." + tipo_real
        destino = pasta / nome

        with destino.open("xb") as saida:
            saida.write(conteudo)

        try:
            with conectar() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE intranet_usuarios
                        SET foto_perfil = %s
                        WHERE id = %s AND ativo = TRUE
                        RETURNING id
                        """,
                        (nome, usuario[0])
                    )

                    if cur.fetchone() is None:
                        raise HTTPException(404, "Usuário não encontrado")

        except Exception:
            destino.unlink(missing_ok=True)
            raise

        return {
            "mensagem": "Foto atualizada com sucesso",
            "foto_perfil": nome
        }

    finally:
        await arquivo.close()


@app.get("/api/perfil/foto")
def visualizar_foto_perfil(
    authorization: Optional[str] = Header(None)
):
    from pathlib import Path
    from fastapi.responses import FileResponse

    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT foto_perfil
                FROM intranet_usuarios
                WHERE id = %s AND ativo = TRUE
                """,
                (usuario[0],)
            )
            registro = cur.fetchone()

    if not registro or not registro[0]:
        raise HTTPException(404, "Foto não cadastrada")

    nome = registro[0]

    pasta = Path("/intranet/fotos_perfil").resolve()
    caminho = (pasta / nome).resolve()

    if (
        caminho.parent != pasta
        or caminho.suffix.lower() not in (".jpeg", ".png", ".webp")
        or not caminho.is_file()
    ):
        raise HTTPException(404, "Foto não encontrada")

    return FileResponse(
        caminho,
        headers={"Cache-Control": "private, no-store"}
    )

@app.post("/api/trocar-senha")
def trocar_senha(
    dados: TrocarSenhaRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)
    uid = usuario[0]
    cpf = usuario[1]

    if len(dados.nova_senha) < 8:
        raise HTTPException(400, "A senha deve ter pelo menos 8 caracteres")

    if dados.nova_senha == cpf:
        raise HTTPException(400, "A senha não pode ser o CPF")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT senha_hash
                FROM intranet_usuarios
                WHERE id = %s
                FOR UPDATE
            """, (uid,))
            senha_hash = cur.fetchone()[0]

            if not bcrypt.checkpw(
                dados.senha_atual.encode(),
                senha_hash.encode()
            ):
                raise HTTPException(401, "Senha atual incorreta")

            novo_hash = bcrypt.hashpw(
                dados.nova_senha.encode(),
                bcrypt.gensalt(rounds=12)
            ).decode()

            cur.execute("""
                UPDATE intranet_usuarios
                SET senha_hash = %s,
                    trocar_senha = FALSE,
                    atualizado_em = NOW()
                WHERE id = %s
            """, (novo_hash, uid))

            cur.execute("""
                UPDATE intranet_sessoes
                SET revogado_em = NOW()
                WHERE usuario_id = %s
                  AND revogado_em IS NULL
            """, (uid,))

    return {
        "success": True,
        "mensagem": "Senha alterada. Faça login novamente."
    }


@app.post("/api/logout")
def logout(authorization: Optional[str] = Header(None)):
    usuario = autenticar(authorization)

    token = authorization[7:]

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE intranet_sessoes
                SET revogado_em = NOW()
                WHERE token_hash = %s
                  AND usuario_id = %s
                RETURNING id
            """, (hash_token(token), usuario[0]))

            sessao = cur.fetchone()

            if sessao:
                cur.execute("""
                    INSERT INTO intranet_auditoria (
                        usuario_id, sessao_id, tipo_evento,
                        acao, modulo, resultado
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    usuario[0], str(sessao[0]), "AUTENTICACAO",
                    "LOGOUT", "LOGIN", "SUCESSO"
                ))

    return {"success": True}

from fastapi.responses import FileResponse

@app.get("/login.html", include_in_schema=False)
def pagina_login():
    return FileResponse("/intranet/login.html")

@app.get("/fotos/logo.png", include_in_schema=False)
def logo_login():
    return FileResponse("/intranet/fotos/logo.png")

from fastapi import Request
from fastapi.responses import HTMLResponse

@app.get("/api/intranet", response_class=HTMLResponse)
def abrir_intranet(request: Request):
    authorization = request.headers.get("authorization")
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(
            status_code=403,
            detail="Troca de senha obrigatória"
        )

    with open("/intranet/index-oficial.html", "r", encoding="utf-8") as arquivo:
        return HTMLResponse(content=arquivo.read())

@app.get("/portal.html", include_in_schema=False)
def pagina_portal():
    return FileResponse("/intranet/portal.html")

from fastapi.staticfiles import StaticFiles

app.mount(
    "/fotos",
    StaticFiles(directory="/intranet/fotos"),
    name="fotos"
)

def exigir_nivel(authorization: Optional[str], nivel_minimo: int):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(
            status_code=403,
            detail="Troca de senha obrigatória"
        )

    if usuario[3] < nivel_minimo:
        raise HTTPException(
            status_code=403,
            detail="Acesso não autorizado para este usuário"
        )

    return usuario


@app.get("/api/permissoes")
def consultar_permissoes(authorization: Optional[str] = Header(None)):
    usuario = exigir_nivel(authorization, 1)

    return {
        "nivel_acesso": usuario[3],
        "tipo": usuario[4],
        "politicas": usuario[3] >= 2,
        "powerbi": usuario[3] >= 2,
        "avaliacao": usuario[3] >= 2,
        "admin": usuario[3] >= 3
    }

@app.get("/teste-permissoes.html", include_in_schema=False)
def pagina_teste_permissoes():
    return FileResponse("/intranet/teste-permissoes.html")

from pathlib import Path
from fastapi.responses import Response

@app.get("/api/documentos/{pasta}/{caminho:path}")
def documento_protegido(
    pasta: str,
    caminho: str,
    authorization: Optional[str] = Header(None)
):
    usuario = exigir_nivel(authorization, 2)

    if pasta not in ("documentos", "politicas"):
        raise HTTPException(status_code=404, detail="Pasta não encontrada")

    base = (Path("/intranet") / pasta).resolve()
    arquivo = (base / caminho).resolve()

    if not arquivo.is_relative_to(base):
        raise HTTPException(status_code=403, detail="Caminho não permitido")

    if not arquivo.is_file() or arquivo.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail="Documento não encontrado")

    registrar_auditoria(
        conectar,
        usuario[0],
        usuario[6],
        "DOCUMENTO",
        "VISUALIZACAO_PDF",
        pasta.upper(),
        "SUCESSO",
        recurso=arquivo.name,
        detalhes={"pasta": pasta}
    )

    return FileResponse(
        path=str(arquivo),
        media_type="application/pdf",
        filename=arquivo.name,
        content_disposition_type="inline"
    )
@app.get("/teste-pdf.html", include_in_schema=False)
def pagina_teste_pdf():
    return FileResponse("/intranet/teste-pdf.html")

@app.get("/trocar-senha.html", include_in_schema=False)
def pagina_trocar_senha():
    return FileResponse("/intranet/trocar-senha.html")


class AtualizarPerfilRequest(BaseModel):
    email: Optional[str] = None
    telefone: Optional[str] = None


@app.put("/api/me")
def atualizar_meu_perfil(
    dados: AtualizarPerfilRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    email = (dados.email or "").strip()
    telefone = (dados.telefone or "").strip()

    if len(email) > 150:
        raise HTTPException(400, "E-mail muito longo")

    if email and (
        "@" not in email
        or "." not in email.split("@")[-1]
        or any(c.isspace() for c in email)
    ):
        raise HTTPException(400, "E-mail inválido")

    if len(telefone) > 20:
        raise HTTPException(400, "Telefone muito longo")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE intranet_usuarios
                SET email = %s,
                    telefone = %s,
                    atualizado_em = NOW()
                WHERE id = %s AND ativo = TRUE
                RETURNING id, nome, email, telefone
            """, (
                email or None,
                telefone or None,
                usuario[0]
            ))

            resultado = cur.fetchone()

            if not resultado:
                raise HTTPException(404, "Usuário não encontrado")

    return {
        "mensagem": "Perfil atualizado com sucesso",
        "id": resultado[0],
        "nome": resultado[1],
        "email": resultado[2],
        "telefone": resultado[3]
    }


@app.get("/api/links-uteis")
def listar_links_uteis(
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, titulo, url, descricao, categoria, ordem
                FROM intranet_links_uteis
                WHERE ativo = TRUE
                ORDER BY ordem ASC, id ASC
            """)

            registros = cur.fetchall()

    return [
        {
            "id": r[0],
            "titulo": r[1],
            "url": r[2],
            "descricao": r[3],
            "categoria": r[4],
            "ordem": r[5]
        }
        for r in registros
    ]

from urllib.parse import urlparse

class NovoLinkRequest(BaseModel):
    titulo: str
    url: str
    ordem: int = 0


@app.post("/api/links-uteis")
def cadastrar_link_util(
    dados: NovoLinkRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")

    titulo = dados.titulo.strip()
    url = dados.url.strip()

    if not titulo or len(titulo) > 150:
        raise HTTPException(400, "Nome inválido")

    endereco = urlparse(url)

    if (
        endereco.scheme not in ("http", "https")
        or not endereco.netloc
        or endereco.username
        or endereco.password
    ):
        raise HTTPException(400, "URL inválida")

    if dados.ordem < 0:
        raise HTTPException(400, "Posição inválida")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO intranet_links_uteis
                    (titulo, url, ordem)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (titulo, url, dados.ordem))

            novo_id = cur.fetchone()[0]
            posicionar_link_util(cur, novo_id, dados.ordem)

    return {
        "id": novo_id,
        "mensagem": "Link cadastrado com sucesso"
    }

@app.put("/api/links-uteis/{link_id}")
def editar_link_util(
    link_id: int,
    dados: NovoLinkRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")

    titulo = dados.titulo.strip()
    url = dados.url.strip()
    endereco = urlparse(url)

    if not titulo or len(titulo) > 150:
        raise HTTPException(400, "Nome inválido")

    if (
        endereco.scheme not in ("http", "https")
        or not endereco.netloc
        or endereco.username
        or endereco.password
    ):
        raise HTTPException(400, "URL inválida")

    if dados.ordem < 0:
        raise HTTPException(400, "Posição inválida")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE intranet_links_uteis
                SET titulo = %s,
                    url = %s,
                    ordem = %s,
                    atualizado_em = NOW()
                WHERE id = %s
                RETURNING id
            """, (titulo, url, dados.ordem, link_id))

            resultado = cur.fetchone()

            if not resultado:
                raise HTTPException(404, "Link não encontrado")

            posicionar_link_util(cur, link_id, dados.ordem)

    return {
        "id": resultado[0],
        "mensagem": "Link atualizado com sucesso"
    }

@app.delete("/api/links-uteis/{link_id}")
def excluir_link_util(
    link_id: int,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM intranet_links_uteis
                WHERE id = %s
                RETURNING id
            """, (link_id,))

            if not cur.fetchone():
                raise HTTPException(404, "Link não encontrado")

            normalizar_ordem_links(cur)

    return {"mensagem": "Link excluído com sucesso"}

def normalizar_ordem_links(cur):
    cur.execute("""
        SELECT id
        FROM intranet_links_uteis
        ORDER BY ordem ASC, id ASC
    """)

    ids = [registro[0] for registro in cur.fetchall()]

    for posicao, link_id in enumerate(ids, start=1):
        cur.execute("""
            UPDATE intranet_links_uteis
            SET ordem = %s
            WHERE id = %s
              AND ordem IS DISTINCT FROM %s
        """, (posicao, link_id, posicao))


def posicionar_link_util(cur, link_id, posicao):
    cur.execute("""
        SELECT id
        FROM intranet_links_uteis
        WHERE id <> %s
        ORDER BY ordem ASC, id ASC
        FOR UPDATE
    """, (link_id,))

    ids = [r[0] for r in cur.fetchall()]

    posicao = max(1, min(int(posicao), len(ids) + 1))
    ids.insert(posicao - 1, link_id)

    for numero, id_atual in enumerate(ids, start=1):
        cur.execute("""
            UPDATE intranet_links_uteis
            SET ordem = %s,
                atualizado_em = NOW()
            WHERE id = %s
              AND ordem IS DISTINCT FROM %s
        """, (numero, id_atual, numero))





@app.get("/api/documentos-titulos")
def listar_documentos_titulos(
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.nome, t.ordem,
                       (SELECT COUNT(*)
                        FROM intranet_doc_tipos tp
                        JOIN intranet_doc_arquivos ar ON ar.tipo_id = tp.id
                        WHERE tp.titulo_id = t.id) AS total_documentos
                FROM intranet_doc_titulos t
                ORDER BY t.ordem ASC, t.id ASC
            """)

            registros = cur.fetchall()

    return [
        {
            "id": r[0],
            "nome": r[1],
            "ordem": r[2],
            "total_documentos": r[3]
        }
        for r in registros
    ]

class DocumentoTituloRequest(BaseModel):
    nome: str
    ordem: int = 1


@app.post("/api/documentos-titulos")
def cadastrar_documento_titulo(
    dados: DocumentoTituloRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")

    nome = dados.nome.strip()

    if not nome or len(nome) > 150:
        raise HTTPException(400, "Título inválido")

    if dados.ordem < 1:
        raise HTTPException(400, "Posição inválida")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO intranet_doc_titulos (nome, ordem)
                VALUES (%s, %s)
                RETURNING id
            """, (nome, dados.ordem))

            novo_id = cur.fetchone()[0]

    return {
        "id": novo_id,
        "mensagem": "Título cadastrado com sucesso"
    }

@app.put("/api/documentos-titulos/{titulo_id}")
def editar_documento_titulo(
    titulo_id: int,
    dados: DocumentoTituloRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")

    nome = dados.nome.strip()

    if not nome or len(nome) > 150:
        raise HTTPException(400, "Título inválido")

    if dados.ordem < 1:
        raise HTTPException(400, "Posição inválida")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE intranet_doc_titulos
                SET nome = %s, ordem = %s
                WHERE id = %s
                RETURNING id
            """, (nome, dados.ordem, titulo_id))

            registro = cur.fetchone()

            if not registro:
                raise HTTPException(404, "Título não encontrado")

    return {
        "id": titulo_id,
        "mensagem": "Título atualizado com sucesso"
    }

@app.delete("/api/documentos-titulos/{titulo_id}")
def excluir_documento_titulo(
    titulo_id: int,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id
                FROM intranet_doc_titulos
                WHERE id = %s
            """, (titulo_id,))

            if not cur.fetchone():
                raise HTTPException(404, "Título não encontrado")

            cur.execute("""
                SELECT COUNT(*)
                FROM intranet_doc_tipos
                WHERE titulo_id = %s
            """, (titulo_id,))

            if cur.fetchone()[0] > 0:
                raise HTTPException(
                    409,
                    "Este título possui subtítulos. Exclua-os primeiro."
                )

            cur.execute("""
                DELETE FROM intranet_doc_titulos
                WHERE id = %s
            """, (titulo_id,))

    return {"mensagem": "Título excluído com sucesso"}

class DocumentoTipoRequest(BaseModel):
    titulo_id: int
    nome: str


@app.post("/api/documentos-tipos")
def cadastrar_documento_tipo(
    dados: DocumentoTipoRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")

    nome = dados.nome.strip()

    if not nome or len(nome) > 150:
        raise HTTPException(400, "Subtítulo inválido")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id
                FROM intranet_doc_titulos
                WHERE id = %s
            """, (dados.titulo_id,))

            if not cur.fetchone():
                raise HTTPException(404, "Título não encontrado")

            cur.execute("""
                INSERT INTO intranet_doc_tipos
                    (titulo_id, nome)
                VALUES (%s, %s)
                RETURNING id
            """, (dados.titulo_id, nome))

            novo_id = cur.fetchone()[0]

    return {
        "id": novo_id,
        "mensagem": "Subtítulo cadastrado com sucesso"
    }

@app.get("/api/documentos-tipos")
def listar_documentos_tipos(
    titulo_id: int,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, titulo_id, nome
                FROM intranet_doc_tipos
                WHERE titulo_id = %s
                ORDER BY ordem ASC NULLS LAST, nome ASC, id ASC
            """, (titulo_id,))

            registros = cur.fetchall()

    return [
        {
            "id": r[0],
            "titulo_id": r[1],
            "nome": r[2]
        }
        for r in registros
    ]

@app.put("/api/documentos-tipos/{tipo_id}")
def editar_documento_tipo(
    tipo_id: int,
    dados: DocumentoTipoRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")

    nome = dados.nome.strip()

    if not nome or len(nome) > 150:
        raise HTTPException(400, "Subtítulo inválido")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id
                FROM intranet_doc_titulos
                WHERE id = %s
            """, (dados.titulo_id,))

            if not cur.fetchone():
                raise HTTPException(404, "Título não encontrado")

            cur.execute("""
                UPDATE intranet_doc_tipos
                SET nome = %s, titulo_id = %s
                WHERE id = %s
                RETURNING id
            """, (nome, dados.titulo_id, tipo_id))

            if not cur.fetchone():
                raise HTTPException(404, "Subtítulo não encontrado")

    return {
        "id": tipo_id,
        "mensagem": "Subtítulo atualizado com sucesso"
    }

@app.delete("/api/documentos-tipos/{tipo_id}")
def excluir_documento_tipo(
    tipo_id: int,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id
                FROM intranet_doc_tipos
                WHERE id = %s
            """, (tipo_id,))

            if not cur.fetchone():
                raise HTTPException(404, "Subtítulo não encontrado")

            cur.execute("""
                SELECT COUNT(*)
                FROM intranet_doc_arquivos
                WHERE tipo_id = %s
            """, (tipo_id,))

            if cur.fetchone()[0] > 0:
                raise HTTPException(
                    409,
                    "Este subtítulo possui documentos. Exclua-os primeiro."
                )

            cur.execute("""
                DELETE FROM intranet_doc_tipos
                WHERE id = %s
            """, (tipo_id,))

    return {
        "mensagem": "Subtítulo excluído com sucesso"
    }

@app.get("/api/documentos-arquivos")
def listar_documentos_arquivos(
    tipo_id: int,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, tipo_id, titulo, nome_arquivo
                FROM intranet_doc_arquivos
                WHERE tipo_id = %s
                ORDER BY ordem ASC NULLS LAST, titulo ASC, id ASC
            """, (tipo_id,))

            registros = cur.fetchall()

    return [
        {
            "id": r[0],
            "tipo_id": r[1],
            "titulo": r[2],
            "nome_arquivo": r[3]
        }
        for r in registros
    ]


@app.get("/api/documentos-arquivos/{arquivo_id}/info")
def informacoes_documento_arquivo(
    arquivo_id: int,
    authorization: Optional[str] = Header(None)
):
    usuario = exigir_nivel(authorization, 2)
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.id, a.nome_arquivo, a.tipo_id, t.titulo_id
                FROM intranet_doc_arquivos a
                JOIN intranet_doc_tipos t ON t.id = a.tipo_id
                WHERE a.id = %s
            """, (arquivo_id,))
            registro = cur.fetchone()
    if not registro:
        raise HTTPException(404, "Documento não encontrado")
    return {
        "id": registro[0], "nome_arquivo": registro[1],
        "tipo_id": registro[2], "titulo_id": registro[3]
    }


@app.get("/api/documentos-arquivos/{arquivo_id}/download")
def download_documento_arquivo(
    arquivo_id: int,
    authorization: Optional[str] = Header(None)
):
    from pathlib import Path
    from fastapi.responses import FileResponse

    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT nome_arquivo, caminho_arquivo
                FROM intranet_doc_arquivos
                WHERE id = %s
            """, (arquivo_id,))
            registro = cur.fetchone()

    if not registro:
        raise HTTPException(404, "Documento não encontrado")

    nome_arquivo, caminho = registro
    pasta = Path("/intranet/documentos_upload").resolve()
    destino = Path(caminho).resolve()

    if not destino.is_relative_to(pasta):
        raise HTTPException(403, "Caminho de arquivo inválido")

    if not destino.is_file():
        raise HTTPException(404, "Arquivo físico não encontrado")

    registrar_auditoria(
        conectar,
        usuario[0],
        usuario[6],
        "DOCUMENTO",
        "DOWNLOAD",
        "DOCUMENTOS",
        "SUCESSO",
        recurso=nome_arquivo,
        recurso_id=str(arquivo_id)
    )

    return FileResponse(
        path=str(destino),
        filename=nome_arquivo
    )
@app.get("/api/documentos-arquivos/{arquivo_id}/visualizar")
def visualizar_documento_arquivo(
    arquivo_id: int,
    authorization: Optional[str] = Header(None)
):
    from pathlib import Path
    from fastapi.responses import FileResponse

    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT nome_arquivo, caminho_arquivo
                FROM intranet_doc_arquivos
                WHERE id = %s
            """, (arquivo_id,))
            registro = cur.fetchone()

    if not registro:
        raise HTTPException(404, "Documento não encontrado")

    nome_arquivo, caminho = registro
    extensao = Path(nome_arquivo).suffix.lower()
    tipos_visualizacao = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
    if extensao not in tipos_visualizacao:
        raise HTTPException(400, "Visualização disponível apenas para PDF, PNG e JPG")
    pasta = Path("/intranet/documentos_upload").resolve()
    destino = Path(caminho).resolve()

    if not destino.is_relative_to(pasta):
        raise HTTPException(403, "Caminho de arquivo inválido")

    if not destino.is_file():
        raise HTTPException(404, "Arquivo físico não encontrado")

    registrar_auditoria(
        conectar,
        usuario[0],
        usuario[6],
        "DOCUMENTO",
        "VISUALIZACAO_ARQUIVO",
        "DOCUMENTOS",
        "SUCESSO",
        recurso=nome_arquivo,
        recurso_id=str(arquivo_id)
    )

    return FileResponse(
        path=str(destino),
        filename=nome_arquivo, media_type=tipos_visualizacao[extensao], content_disposition_type="inline"
    )

@app.put("/api/documentos-arquivos/{arquivo_id}")
def editar_documento_arquivo(
    arquivo_id: int,
    dados: dict,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")

    titulo = str(dados.get("titulo", "")).strip()

    if not titulo or len(titulo) > 200:
        raise HTTPException(400, "Nome do documento inválido")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE intranet_doc_arquivos
                SET titulo = %s
                WHERE id = %s
                RETURNING id
            """, (titulo, arquivo_id))

            if not cur.fetchone():
                raise HTTPException(404, "Documento não encontrado")

    return {"mensagem": "Documento atualizado com sucesso"}


@app.delete("/api/documentos-arquivos/{arquivo_id}")
def excluir_documento_arquivo(
    arquivo_id: int,
    authorization: Optional[str] = Header(None)
):
    from pathlib import Path

    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT caminho_arquivo
                FROM intranet_doc_arquivos
                WHERE id = %s
            """, (arquivo_id,))

            registro = cur.fetchone()

            if not registro:
                raise HTTPException(404, "Documento não encontrado")

            caminho = Path(registro[0]).resolve()
            pasta = Path("/intranet/documentos_upload").resolve()

            if not caminho.is_relative_to(pasta):
                raise HTTPException(403, "Caminho de arquivo inválido")

            if caminho.exists():
                caminho.unlink()

            cur.execute("""
                DELETE FROM intranet_doc_arquivos
                WHERE id = %s
            """, (arquivo_id,))

    return {"mensagem": "Documento excluído com sucesso"}

@app.post("/api/documentos-arquivos/upload")
async def upload_documento_arquivo(
    tipo_id: int = Form(...),
    titulo: str = Form(...),
    arquivo: UploadFile = File(...),
    authorization: Optional[str] = Header(None)
):
    import os
    import uuid
    from pathlib import Path

    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador")

    titulo = titulo.strip()

    if not titulo or len(titulo) > 200:
        raise HTTPException(400, "Título do documento inválido")

    nome_original = Path(arquivo.filename or "").name
    extensao = Path(nome_original).suffix.lower()

    extensoes_permitidas = {
        ".pdf", ".doc", ".docx",
        ".xls", ".xlsx", ".ppt", ".pptx",
        ".txt", ".csv", ".png", ".jpg", ".jpeg"
    }

    if extensao not in extensoes_permitidas:
        raise HTTPException(400, "Tipo de arquivo não permitido")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM intranet_doc_tipos WHERE id = %s",
                (tipo_id,)
            )

            if not cur.fetchone():
                raise HTTPException(404, "Subtítulo não encontrado")

    pasta = Path("/intranet/documentos_upload")
    pasta.mkdir(parents=True, exist_ok=True)

    nome_salvo = f"{uuid.uuid4().hex}{extensao}"
    destino = pasta / nome_salvo

    limite = 10 * 1024 * 1024
    tamanho = 0

    try:
        with destino.open("wb") as saida:
            while True:
                bloco = await arquivo.read(1024 * 1024)

                if not bloco:
                    break

                tamanho += len(bloco)

                if tamanho > limite:
                    raise HTTPException(
                        413,
                        "Arquivo excede o limite de 10 MB"
                    )

                saida.write(bloco)

        with conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intranet_doc_arquivos
                        (tipo_id, titulo, nome_arquivo, caminho_arquivo)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                """, (
                    tipo_id,
                    titulo,
                    nome_original,
                    str(destino)
                ))

                novo_id = cur.fetchone()[0]

    except Exception:
        destino.unlink(missing_ok=True)
        raise

    finally:
        await arquivo.close()

    return {
        "id": novo_id,
        "titulo": titulo,
        "nome_arquivo": nome_original,
        "mensagem": "Documento enviado com sucesso"
    }


@app.get("/api/usuarios")
def listar_usuarios(authorization: Optional[str] = Header(None)):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo ao administrador")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, matricula, cpf, nome, email, telefone,
                       nivel_acesso, tipo, ativo,
                       data_nascimento, data_admissao, data_demissao,
                       centro_custo, departamento, cargo, foto_perfil
                FROM intranet_usuarios
                ORDER BY nome
            """)

            colunas = [col.name for col in cur.description]
            registros = cur.fetchall()

    return [
        {
            chave: valor.isoformat() if hasattr(valor, "isoformat") else valor
            for chave, valor in zip(colunas, registro)
        }
        for registro in registros
    ]



# Visualizacao administrativa de fotos - V11
@app.get("/api/admin/usuarios/{usuario_id}/foto")
def foto_usuario_admin(
    usuario_id: int,
    authorization: Optional[str] = Header(None)
):
    import re
    from pathlib import Path
    from fastapi.responses import FileResponse

    administrador = autenticar(authorization)

    if administrador[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if administrador[3] != 3:
        raise HTTPException(403, "Acesso exclusivo ao administrador")

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT foto_perfil FROM intranet_usuarios WHERE id = %s",
                (usuario_id,)
            )
            registro = cur.fetchone()

    if not registro or not registro[0]:
        raise HTTPException(404, "Foto não cadastrada")

    nome = registro[0]

    if not re.fullmatch(r"[a-f0-9]{32}\.(jpeg|png|webp)", nome):
        raise HTTPException(404, "Foto inválida")

    pasta = Path("/intranet/fotos_perfil").resolve()
    caminho = (pasta / nome).resolve()

    if caminho.parent != pasta or not caminho.is_file():
        raise HTTPException(404, "Foto não encontrada")

    return FileResponse(
        caminho,
        headers={"Cache-Control": "private, no-store"}
    )

class CadastrarUsuarioRequest(BaseModel):
    matricula: Optional[str] = None
    cpf: str
    nome: str
    email: Optional[str] = None
    telefone: Optional[str] = None
    data_nascimento: Optional[date] = None
    data_admissao: Optional[date] = None
    data_demissao: Optional[date] = None
    centro_custo: Optional[str] = None
    departamento: Optional[str] = None
    cargo: Optional[str] = None
    nivel_acesso: int = 1
    tipo: int = 1
    ativo: bool = True


@app.post("/api/usuarios", status_code=201)
def cadastrar_usuario(
    dados: CadastrarUsuarioRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = autenticar(authorization)

    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if usuario[3] != 3:
        raise HTTPException(403, "Acesso exclusivo ao administrador")

    cpf = re.sub(r"\D", "", dados.cpf)
    nome = dados.nome.strip()

    if len(cpf) != 11 or len(set(cpf)) == 1:
        raise HTTPException(400, "CPF inválido")

    def cpf_valido(valor):
        for tamanho in (9, 10):
            soma = sum(
                int(valor[i]) * (tamanho + 1 - i)
                for i in range(tamanho)
            )
            digito = (soma * 10) % 11
            if digito == 10:
                digito = 0
            if digito != int(valor[tamanho]):
                return False
        return True

    if not cpf_valido(cpf):
        raise HTTPException(400, "CPF inválido")

    if not nome or len(nome) > 150:
        raise HTTPException(400, "Nome inválido")

    if dados.nivel_acesso not in (1, 2, 3):
        raise HTTPException(400, "Nível de acesso inválido")

    if dados.tipo not in (1, 2):
        raise HTTPException(400, "Tipo de usuário inválido")

    limites = {
        "matricula": 30,
        "email": 150,
        "telefone": 20,
        "centro_custo": 100,
        "departamento": 100,
        "cargo": 150
    }

    campos = {}
    for campo, limite in limites.items():
        valor = getattr(dados, campo)
        valor = valor.strip() if valor is not None else None
        if valor and len(valor) > limite:
            raise HTTPException(400, f"{campo}: tamanho inválido")
        campos[campo] = valor or None

    if dados.data_admissao and dados.data_demissao:
        if dados.data_demissao < dados.data_admissao:
            raise HTTPException(
                400, "Demissão anterior à admissão"
            )

    senha_hash = bcrypt.hashpw(
        cpf.encode("utf-8"),
        bcrypt.gensalt(rounds=12)
    ).decode("utf-8")

    try:
        with conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intranet_usuarios (
                        matricula, cpf, nome, email, telefone,
                        data_nascimento, data_admissao, data_demissao,
                        centro_custo, departamento, cargo,
                        nivel_acesso, tipo, ativo,
                        senha_hash, trocar_senha
                    )
                    VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, TRUE
                    )
                    RETURNING id
                """, (
                    campos["matricula"], cpf, nome,
                    campos["email"], campos["telefone"],
                    dados.data_nascimento, dados.data_admissao,
                    dados.data_demissao,
                    campos["centro_custo"],
                    campos["departamento"], campos["cargo"],
                    dados.nivel_acesso, dados.tipo, dados.ativo,
                    senha_hash
                ))
                novo_id = cur.fetchone()[0]

    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "CPF já cadastrado")

    return {
        "id": novo_id,
        "mensagem": "Usuário cadastrado com sucesso",
        "trocar_senha": True
    }



class EditarUsuarioRequest(BaseModel):
    matricula: Optional[str] = None
    nome: str
    email: Optional[str] = None
    telefone: Optional[str] = None
    data_nascimento: Optional[date] = None
    data_admissao: Optional[date] = None
    data_demissao: Optional[date] = None
    centro_custo: Optional[str] = None
    departamento: Optional[str] = None
    cargo: Optional[str] = None
    nivel_acesso: int
    tipo: int
    ativo: bool


@app.put("/api/usuarios/{usuario_id}")
def editar_usuario(
    usuario_id: int,
    dados: EditarUsuarioRequest,
    authorization: Optional[str] = Header(None)
):
    administrador = autenticar(authorization)

    if administrador[5]:
        raise HTTPException(403, "Troca de senha obrigatória")

    if administrador[3] != 3:
        raise HTTPException(403, "Acesso exclusivo ao administrador")

    nome = dados.nome.strip()

    if not nome or len(nome) > 150:
        raise HTTPException(400, "Nome inválido")

    if dados.nivel_acesso not in (1, 2, 3):
        raise HTTPException(400, "Nível de acesso inválido")

    if dados.tipo not in (1, 2):
        raise HTTPException(400, "Tipo de usuário inválido")

    limites = {
        "matricula": 30,
        "email": 150,
        "telefone": 20,
        "centro_custo": 100,
        "departamento": 100,
        "cargo": 150
    }

    campos = {}

    for campo, limite in limites.items():
        valor = getattr(dados, campo)
        valor = valor.strip() if valor is not None else None

        if valor and len(valor) > limite:
            raise HTTPException(
                400, f"{campo}: tamanho inválido"
            )

        campos[campo] = valor or None

    if dados.data_admissao and dados.data_demissao:
        if dados.data_demissao < dados.data_admissao:
            raise HTTPException(
                400, "Demissão anterior à admissão"
            )

    if usuario_id == administrador[0]:
        if dados.nivel_acesso != 3 or not dados.ativo:
            raise HTTPException(
                400,
                "Não é permitido remover o próprio acesso administrativo"
            )

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE intranet_usuarios
                SET matricula = %s,
                    nome = %s,
                    email = %s,
                    telefone = %s,
                    data_nascimento = %s,
                    data_admissao = %s,
                    data_demissao = %s,
                    centro_custo = %s,
                    departamento = %s,
                    cargo = %s,
                    nivel_acesso = %s,
                    tipo = %s,
                    ativo = %s,
                    atualizado_em = NOW()
                WHERE id = %s
                RETURNING id
            """, (
                campos["matricula"],
                nome,
                campos["email"],
                campos["telefone"],
                dados.data_nascimento,
                dados.data_admissao,
                dados.data_demissao,
                campos["centro_custo"],
                campos["departamento"],
                campos["cargo"],
                dados.nivel_acesso,
                dados.tipo,
                dados.ativo,
                usuario_id
            ))

            resultado = cur.fetchone()

            if not resultado:
                raise HTTPException(
                    404, "Usuário não encontrado"
                )

    return {
        "id": usuario_id,
        "mensagem": "Usuário atualizado com sucesso"
    }

# ==================================================
# COMUNICADOS - Consulta para todos os funcionarios
# ==================================================

from comunicados_api import listar_comunicados_publicados

@app.get("/api/comunicados")
def consultar_comunicados(
    authorization: Optional[str] = Header(None)
):
    exigir_nivel(authorization, 1)
    return listar_comunicados_publicados(conectar)



@app.post("/api/comunicados/{comunicado_id}/visualizacao")
def registrar_visualizacao_comunicado(
    comunicado_id: int,
    authorization: Optional[str] = Header(None)
):
    exigir_nivel(authorization, 1)

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE intranet_comunicados
                SET visualizacoes = COALESCE(visualizacoes, 0) + 1
                WHERE id = %s
                  AND ativo = TRUE
                  AND publicado = TRUE
                  AND (data_inicio IS NULL OR data_inicio <= NOW())
                  AND (data_fim IS NULL OR data_fim >= NOW())
                RETURNING visualizacoes
            """, (comunicado_id,))

            registro = cur.fetchone()

            if registro is None:
                raise HTTPException(
                    404,
                    "Comunicado nao encontrado ou indisponivel"
                )

    return {
        "id": comunicado_id,
        "visualizacoes": registro[0]
    }

from comunicados_api import listar_comunicados_admin

@app.get("/api/admin/comunicados")
def consultar_comunicados_admin(
    authorization: Optional[str] = Header(None)
):
    exigir_nivel(authorization, 3)
    return listar_comunicados_admin(conectar)

from comunicados_api import ComunicadoRequest, criar_comunicado

@app.post("/api/admin/comunicados", status_code=201)
def cadastrar_comunicado_admin(
    dados: ComunicadoRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = exigir_nivel(authorization, 3)
    return criar_comunicado(conectar, dados, usuario[0])

from comunicados_api import atualizar_comunicado

@app.put("/api/admin/comunicados/{comunicado_id}")
def editar_comunicado_admin(
    comunicado_id: int,
    dados: ComunicadoRequest,
    authorization: Optional[str] = Header(None)
):
    usuario = exigir_nivel(authorization, 3)

    return atualizar_comunicado(
        conectar,
        comunicado_id,
        dados,
        usuario[0]
    )

@app.post("/api/admin/comunicados/upload-imagem")
async def upload_imagem_comunicado(
    arquivo: UploadFile = File(...),
    authorization: Optional[str] = Header(None)
):
    import uuid
    import struct
    from pathlib import Path

    exigir_nivel(authorization, 3)

    limite = 5 * 1024 * 1024
    conteudo = await arquivo.read(limite + 1)

    try:
        if len(conteudo) > limite:
            raise HTTPException(413, "Imagem excede 5 MB")

        nome = Path(arquivo.filename or "").name
        extensao = Path(nome).suffix.lower()

        tipos = {
            ".jpg": "jpeg",
            ".jpeg": "jpeg",
            ".png": "png",
            ".webp": "webp"
        }

        if extensao not in tipos:
            raise HTTPException(400, "Formato de imagem não permitido")

        def validar_imagem(dados):
            if dados.startswith(b"\xff\xd8\xff"):
                return "jpeg"

            if dados.startswith(b"\x89PNG\r\n\x1a\n") and len(dados) >= 24:
                largura, altura = struct.unpack(">II", dados[16:24])
                if largura > 0 and altura > 0:
                    return "png"

            if (
                len(dados) >= 16
                and dados[:4] == b"RIFF"
                and dados[8:12] == b"WEBP"
                and dados[12:16] in (b"VP8 ", b"VP8L", b"VP8X")
            ):
                return "webp"

            return None

        tipo_real = validar_imagem(conteudo)

        if tipo_real != tipos[extensao]:
            raise HTTPException(400, "Conteúdo da imagem inválido")

        pasta = Path("/intranet/documentos_upload/comunicados")
        pasta.mkdir(parents=True, exist_ok=True)

        nome_final = uuid.uuid4().hex + "." + tipo_real
        destino = pasta / nome_final

        with destino.open("xb") as saida:
            saida.write(conteudo)

        return {
            "mensagem": "Imagem enviada com sucesso",
            "imagem_url": "/api/comunicados/imagens/" + nome_final
        }

    finally:
        await arquivo.close()


@app.get("/api/comunicados/imagens/{nome_arquivo}")
def visualizar_imagem_comunicado(nome_arquivo: str):
    from pathlib import Path
    from fastapi.responses import FileResponse

    pasta = Path("/intranet/documentos_upload/comunicados").resolve()
    caminho = (pasta / nome_arquivo).resolve()

    if caminho.parent != pasta or caminho.suffix.lower() not in (
        ".jpeg", ".png", ".webp"
    ):
        raise HTTPException(404, "Imagem não encontrada")

    if not caminho.is_file():
        raise HTTPException(404, "Imagem não encontrada")

    return FileResponse(caminho)

# Recuperação de senha por e-mail - V8
from recuperacao_senha import router as router_recuperacao, configurar as configurar_recuperacao

configurar_recuperacao(conectar, hash_token)
app.include_router(router_recuperacao)
# Paginas de recuperacao de senha - V8
@app.get("/esqueci-senha.html", include_in_schema=False)
def pagina_esqueci_senha():
    return FileResponse("/intranet/esqueci-senha.html")

@app.get("/redefinir-senha.html", include_in_schema=False)
def pagina_redefinir_senha():
    from fastapi.responses import Response
    return FileResponse(
        "/intranet/redefinir-senha.html",
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer"
        }
    )

@app.get("/index-v9-teste.html", include_in_schema=False)
def pagina_index_v9_teste():
    from fastapi.responses import FileResponse
    return FileResponse("/intranet/index-oficial.html")


# MODULO GALERIA DE IMAGENS
from galeria_api import router_galeria, configurar_galeria

configurar_galeria(
    conectar,
    autenticar,
    registrar_auditoria
)

app.include_router(router_galeria)
# MODULO DE VIDEOS DO INSTAGRAM
from videos_api import router_videos, configurar_videos

configurar_videos(
    conectar,
    autenticar,
    registrar_auditoria
)

app.include_router(router_videos)


# MODULO DE PESQUISAS
from pesquisas_api import router_pesquisas, configurar_pesquisas

configurar_pesquisas(
    conectar,
    autenticar,
    registrar_auditoria
)

app.include_router(router_pesquisas)

# MODULO DASHBOARDS - AUDITORIA
from dashboard_api import router_dashboards, configurar_dashboards

configurar_dashboards(
    conectar,
    autenticar
)

app.include_router(router_dashboards)

# TELA EXPERIMENTAL DO DASHBOARD DE AUDITORIA
from fastapi.responses import FileResponse

@app.get("/dashboard-auditoria.html", include_in_schema=False)
def tela_dashboard_auditoria():
    return FileResponse("/intranet/DASHBOARDS-DEV/dashboard-auditoria.html")

# DASHBOARD DE RESULTADOS DAS PESQUISAS
from dashboard_pesquisas_api import (
    router_pesquisas_dashboard,
    configurar_pesquisas_dashboard
)

configurar_pesquisas_dashboard(conectar, autenticar)
app.include_router(router_pesquisas_dashboard)

# DASHBOARDS V12 - ADMINISTRACAO E CONTROLE DE ACESSO

from dashboards_admin_api import (
    router_dashboards_admin,
    configurar_dashboards_admin
)

configurar_dashboards_admin(conectar, autenticar)
app.include_router(router_dashboards_admin)

from dashboards_acesso_api import (
    router_dashboards_acesso,
    configurar_dashboards_acesso
)

configurar_dashboards_acesso(conectar, autenticar)
app.include_router(router_dashboards_acesso)

# MEGAVIG PULSE V2 - somente catalogo e rascunhos
from pulse_api import router_pulse, configurar_pulse
configurar_pulse(conectar, autenticar)
app.include_router(router_pulse)



# TELA DO DASHBOARD DE RESULTADOS DAS PESQUISAS
@app.get("/dashboard-pesquisas.html", include_in_schema=False)
def tela_dashboard_pesquisas():
    return FileResponse("/intranet/DASHBOARDS-DEV/dashboard-pesquisas.html")


# MODULO ANIVERSARIANTES V1
from aniversariantes_api import router_aniversariantes, configurar_aniversariantes
configurar_aniversariantes(conectar, autenticar)
app.include_router(router_aniversariantes)


# MODULO EVENTOS E AVISOS V12
from eventos_avisos_api import router_eventos_avisos, configurar_eventos_avisos
configurar_eventos_avisos(conectar, autenticar)
app.include_router(router_eventos_avisos)

# PERMISSOES ADMINISTRATIVAS POR MODULO - V12
from admin_permissoes_api import (
    router_admin_permissoes,
    configurar_admin_permissoes
)

configurar_admin_permissoes(conectar, autenticar)
app.include_router(router_admin_permissoes)

# MODULO GESTORES E EQUIPES - V12
from gestores_equipes_api import (
    router_gestores_equipes,
    configurar_gestores_equipes
)

configurar_gestores_equipes(conectar, autenticar)
app.include_router(router_gestores_equipes)

# MODULO TREINAMENTOS V12
from treinamentos_api import router_treinamentos, configurar_treinamentos
configurar_treinamentos(conectar, autenticar, registrar_auditoria)
app.include_router(router_treinamentos)

# MODULO AVALIACOES DE DESEMPENHO V12
from avaliacoes_api import router_avaliacoes, configurar_avaliacoes
configurar_avaliacoes(conectar, autenticar)
app.include_router(router_avaliacoes)


# Registro complementar da rota de duração do EAD
from fastapi.routing import APIRoute

_rota_duracao = next(
    (
        rota for rota in router_treinamentos.routes
        if isinstance(rota, APIRoute)
        and rota.path.endswith("/aulas/{aula_id}/duracao")
    ),
    None
)

if _rota_duracao is not None:
    app.router.routes.append(_rota_duracao)

# Registro complementar das rotas de Avaliacao EAD
from fastapi.routing import APIRoute

for _rota_ead in router_treinamentos.routes:
    if (
        isinstance(_rota_ead, APIRoute)
        and "/avaliacao" in _rota_ead.path
        and not any(
            isinstance(r, APIRoute)
            and r.path == _rota_ead.path
            and r.methods == _rota_ead.methods
            for r in app.routes
        )
    ):
        app.router.routes.append(_rota_ead)


# Rotas complementares do publico EAD
from fastapi.routing import APIRoute
for _rota in router_treinamentos.routes:
    if isinstance(_rota, APIRoute) and (
        "/publico" in _rota.path
    ):
        if not any(
            isinstance(r, APIRoute)
            and r.path == _rota.path
            and r.methods == _rota.methods
            for r in app.routes
        ):
            app.router.routes.append(_rota)

# Rotas complementares EAD - Execucao do funcionario
from fastapi.routing import APIRoute
for _rota in router_treinamentos.routes:
    if isinstance(_rota, APIRoute) and "/meus/" in _rota.path:
        if not any(
            isinstance(r, APIRoute)
            and r.path == _rota.path
            and r.methods == _rota.methods
            for r in app.routes
        ):
            app.router.routes.append(_rota)



# Configuração de identidade da empresa (instalação única)
from fastapi.responses import FileResponse as EmpresaFileResponse
from pathlib import Path as EmpresaPath
import uuid as empresa_uuid

EMPRESA_DIR = EmpresaPath('/intranet/documentos_upload/empresa_assets')

def empresa_criar_tabela(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS intranet_empresa_config (
      id integer PRIMARY KEY CHECK (id=1), nome varchar(160) NOT NULL DEFAULT 'Megavig',
      nome_curto varchar(80) NOT NULL DEFAULT 'Megavig', cnpj varchar(18) NOT NULL DEFAULT '',
      telefone varchar(30) NOT NULL DEFAULT '', endereco text NOT NULL DEFAULT '',
      cor_principal varchar(7) NOT NULL DEFAULT '#2477ca',
      logo text, banners jsonb NOT NULL DEFAULT '[]'::jsonb,
      hashes jsonb NOT NULL DEFAULT '{}'::jsonb,
      atualizado_em timestamptz NOT NULL DEFAULT now())""")
    cur.execute("ALTER TABLE intranet_empresa_config ADD COLUMN IF NOT EXISTS hashes jsonb NOT NULL DEFAULT '{}'::jsonb")
    cur.execute("ALTER TABLE intranet_empresa_config ADD COLUMN IF NOT EXISTS ativo boolean NOT NULL DEFAULT TRUE")
    cur.execute("ALTER TABLE intranet_empresa_config ADD COLUMN IF NOT EXISTS url_publica text NOT NULL DEFAULT ''")
    cur.execute("INSERT INTO intranet_empresa_config(id) VALUES(1) ON CONFLICT DO NOTHING")

def empresa_dados(cur):
    empresa_criar_tabela(cur)
    cur.execute('SELECT nome,nome_curto,cnpj,telefone,endereco,cor_principal,logo,banners,ativo,url_publica FROM intranet_empresa_config WHERE id=1')
    r=cur.fetchone()
    return dict(zip(('nome','nome_curto','cnpj','telefone','endereco','cor_principal','logo','banners','ativo','url_publica'),r))

@app.get('/api/empresa/config')
def empresa_consultar(authorization: Optional[str]=Header(None)):
    exigir_nivel(authorization,1)
    with conectar() as conn:
        with conn.cursor() as cur: return empresa_dados(cur)

class EmpresaConfigRequest(BaseModel):
    nome: str
    nome_curto: str = ''
    cnpj: str = ''
    telefone: str = ''
    endereco: str = ''
    cor_principal: str = '#2477ca'
    ativo: bool = True
    url_publica: str = ''

@app.put('/api/empresa/config')
def empresa_atualizar(dados: EmpresaConfigRequest, authorization: Optional[str]=Header(None)):
    exigir_nivel(authorization,3)
    nome=dados.nome.strip()
    if not nome or len(nome)>160: raise HTTPException(400,'Nome da empresa inválido')
    curto=dados.nome_curto.strip() or nome
    if len(curto)>80 or len(dados.cnpj)>18 or len(dados.telefone)>30 or len(dados.endereco)>500:
        raise HTTPException(400,'Campo excede o tamanho permitido')
    if dados.cnpj and (len(re.sub(r'\D','',dados.cnpj))!=14): raise HTTPException(400,'CNPJ deve conter 14 dígitos')
    if not re.fullmatch(r'#[0-9a-fA-F]{6}',dados.cor_principal): raise HTTPException(400,'Cor inválida')
    from urllib.parse import urlsplit
    url_publica = dados.url_publica.strip().rstrip('/')
    if url_publica:
        try:
            partes = urlsplit(url_publica)
            valido = (partes.scheme == 'https' and bool(partes.hostname) and not partes.username
                      and not partes.password and not partes.path and not partes.query
                      and not partes.fragment and not any(c.isspace() for c in url_publica)
                      and len(url_publica) <= 500)
            if partes.port is not None and not (1 <= partes.port <= 65535): valido = False
        except ValueError:
            valido = False
        if not valido: raise HTTPException(400, 'Informe uma URL publica HTTPS sem caminho ou parametros')
    with conectar() as conn:
        with conn.cursor() as cur:
            empresa_criar_tabela(cur)
            cur.execute("UPDATE intranet_empresa_config SET nome=%s,nome_curto=%s,cnpj=%s,telefone=%s,endereco=%s,cor_principal=%s,ativo=%s,url_publica=%s,atualizado_em=now() WHERE id=1",
                (nome,curto,dados.cnpj.strip(),dados.telefone.strip(),dados.endereco.strip(),dados.cor_principal,dados.ativo,url_publica))
            return empresa_dados(cur)

@app.post('/api/empresa/imagem/{tipo}')
async def empresa_enviar_imagem(tipo: str, arquivo: UploadFile=File(...), authorization: Optional[str]=Header(None)):
    exigir_nivel(authorization,3)
    if tipo!='logo' and tipo not in [f'banner{i}' for i in range(1,6)]: raise HTTPException(400,'Tipo inválido')
    conteudo=await arquivo.read(5*1024*1024+1)
    await arquivo.close()
    if not conteudo or len(conteudo)>5*1024*1024: raise HTTPException(413,'Imagem vazia ou maior que 5 MB')
    if conteudo.startswith(b'\xff\xd8\xff'): ext='jpg'
    elif conteudo.startswith(b'\x89PNG\r\n\x1a\n'): ext='png'
    elif len(conteudo)>12 and conteudo[:4]==b'RIFF' and conteudo[8:12]==b'WEBP': ext='webp'
    else: raise HTTPException(400,'Envie JPG, PNG ou WebP válido')
    EMPRESA_DIR.mkdir(parents=True,exist_ok=True)
    nome=empresa_uuid.uuid4().hex+'.'+ext
    caminho=EMPRESA_DIR/nome
    # Nome físico aleatório; SHA-256 separado para auditoria de integridade.
    digest = hashlib.sha256(conteudo).hexdigest()
    with caminho.open('xb') as destino:
        destino.write(conteudo)
    try:
        with conectar() as conn:
            with conn.cursor() as cur:
                cfg=empresa_dados(cur)
                if tipo=='logo':
                    cur.execute('UPDATE intranet_empresa_config SET logo=%s,atualizado_em=now() WHERE id=1',(nome,))
                else:
                    import json
                    banners=list(cfg['banners'] or [])
                    while len(banners)<5: banners.append(None)
                    banners[int(tipo[-1])-1]=nome
                    cur.execute('UPDATE intranet_empresa_config SET banners=%s::jsonb,atualizado_em=now() WHERE id=1',(json.dumps(banners),))
                cur.execute("UPDATE intranet_empresa_config SET hashes = jsonb_set(COALESCE(hashes, '{}'::jsonb), %s::text[], to_jsonb(%s::text), true) WHERE id=1", ([tipo], digest))
                return empresa_dados(cur)
    except Exception:
        caminho.unlink(missing_ok=True)
        raise

# Logotipo público para telas anteriores à autenticação.
# Expõe exclusivamente a imagem ativa cadastrada, sem dados administrativos.
@app.get('/api/empresa/logo-publico', include_in_schema=False)
def empresa_logo_publico():
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT logo FROM intranet_empresa_config WHERE id=1')
            registro = cur.fetchone()
    nome = registro[0] if registro else None
    if not nome or not re.fullmatch(r'[a-f0-9]{32}\.(jpg|png|webp)', nome):
        raise HTTPException(404, 'Logotipo não cadastrado')
    caminho = EMPRESA_DIR / nome
    if not caminho.is_file():
        raise HTTPException(404, 'Logotipo não encontrado')
    return EmpresaFileResponse(caminho, headers={'Cache-Control': 'public, max-age=60'})

@app.get('/api/empresa/imagem/{nome}')
def empresa_ver_imagem(nome: str, authorization: Optional[str]=Header(None)):
    exigir_nivel(authorization,1)
    if not re.fullmatch(r'[a-f0-9]{32}\.(jpg|png|webp)',nome): raise HTTPException(404,'Imagem não encontrada')
    with conectar() as conn:
        with conn.cursor() as cur:
            cfg=empresa_dados(cur)
    if nome not in [cfg['logo'],*(cfg['banners'] or [])]: raise HTTPException(404,'Imagem não encontrada')
    caminho=EMPRESA_DIR/nome
    if not caminho.is_file(): raise HTTPException(404,'Imagem não encontrada')
    return EmpresaFileResponse(caminho,headers={'Cache-Control':'private, max-age=300'})


# Megavig Pulse: inicialização opcional do motor de execução.
# Ative somente após homologação: MEGAVIG_PULSE_AGENDADOR=1
from pulse_agendador import iniciar_agendador, parar_agendador

@app.on_event('startup')
def iniciar_pulse_v2():
    iniciar_agendador(conectar)

@app.on_event('shutdown')
def parar_pulse_v2():
    parar_agendador()

# IMPORTACAO DE FUNCIONARIOS - VALIDACAO INICIAL SEM GRAVACAO
from importacao_funcionarios_api import router_importacao_funcionarios, configurar_importacao_funcionarios
configurar_importacao_funcionarios(conectar, autenticar)
app.include_router(router_importacao_funcionarios)

# BUSCA TEXTUAL EM DOCUMENTOS - SEM API PAGA
from rag_documentos_api import router_rag_documentos, configurar_rag_documentos
configurar_rag_documentos(conectar, exigir_nivel)
app.include_router(router_rag_documentos)


# Ordenação manual de Políticas e Documentos — V12
class MoverDocumentoRequest(BaseModel):
    direcao: int

@app.post("/api/documentos-ordenacao/{nivel}/{item_id}")
def mover_documento(nivel: str, item_id: int, dados: MoverDocumentoRequest,
                    authorization: Optional[str] = Header(None)):
    usuario = exigir_nivel(authorization, 3)
    tabelas = {"titulo": ("intranet_doc_titulos", None),
               "tipo": ("intranet_doc_tipos", "titulo_id"),
               "arquivo": ("intranet_doc_arquivos", "tipo_id")}
    if nivel not in tabelas or dados.direcao not in (-1, 1):
        raise HTTPException(400, "Movimentação inválida")
    tabela, grupo = tabelas[nivel]
    with conectar() as conn:
        with conn.cursor() as cur:
            # Impede concorrência entre movimentações do mesmo nível.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("ordem_doc_" + nivel,))
            cur.execute(f"SELECT {grupo if grupo else 'id'} FROM {tabela} WHERE id = %s", (item_id,))
            atual = cur.fetchone()
            if not atual:
                raise HTTPException(404, "Item não encontrado")
            if grupo:
                cur.execute(f"SELECT id FROM {tabela} WHERE {grupo} = %s ORDER BY ordem ASC NULLS LAST, id ASC FOR UPDATE", (atual[0],))
            else:
                cur.execute(f"SELECT id FROM {tabela} ORDER BY ordem ASC NULLS LAST, id ASC FOR UPDATE")
            ids = [r[0] for r in cur.fetchall()]
            pos = ids.index(item_id)
            destino = pos + dados.direcao
            if not 0 <= destino < len(ids):
                return {"movido": False}
            ids[pos], ids[destino] = ids[destino], ids[pos]
            for indice, identificador in enumerate(ids, 1):
                cur.execute(f"UPDATE {tabela} SET ordem = %s WHERE id = %s AND ordem IS DISTINCT FROM %s",
                            (indice, identificador, indice))
    return {"movido": True}




# POSTOS OPERACIONAIS V12 — cadastro geográfico
from decimal import Decimal
from math import isfinite

class PostoGeograficoRequest(BaseModel):
    cliente: Optional[str] = None
    tipo_servico: Optional[str] = None
    cep: Optional[str] = None
    logradouro: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    uf: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    precisao_localizacao: str = 'PENDENTE'
    status: str = 'ATIVO'

def _validar_posto(dados):
    limites = {'cliente':150,'tipo_servico':100,'logradouro':200,'numero':20,'complemento':100,'bairro':100,'cidade':100}
    valores = {}
    for campo, limite in limites.items():
        valor = (getattr(dados,campo) or '').strip()
        if len(valor)>limite: raise HTTPException(400, f'{campo}: tamanho inválido')
        valores[campo]=valor or None
    cep = re.sub(r'\D','',dados.cep or '')
    if cep and len(cep)!=8: raise HTTPException(400,'CEP deve ter 8 dígitos')
    valores['cep']=cep or None
    uf=(dados.uf or '').strip().upper()
    if uf and (len(uf)!=2 or not uf.isalpha()): raise HTTPException(400,'UF inválida')
    valores['uf']=uf or None
    if dados.status not in ('ATIVO','INATIVO'): raise HTTPException(400,'Status inválido')
    if dados.precisao_localizacao not in ('PENDENTE','APROXIMADA','EXATA'): raise HTTPException(400,'Precisão inválida')
    lat,lon=dados.latitude,dados.longitude
    if (lat is None)!=(lon is None): raise HTTPException(400,'Informe latitude e longitude juntas')
    if lat is not None and (not isfinite(lat) or not isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180):
        raise HTTPException(400,'Coordenadas inválidas')
    if lat is None and dados.precisao_localizacao!='PENDENTE': raise HTTPException(400,'Localização sem coordenadas deve ficar pendente')
    valores.update(latitude=lat,longitude=lon,precisao_localizacao=dados.precisao_localizacao,status=dados.status)
    return valores

@app.get('/api/postos')
def listar_postos_geograficos(authorization: Optional[str] = Header(None)):
    exigir_nivel(authorization, 3)
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute('''SELECT p.id,p.centro_custo_nominal,p.cliente,p.tipo_servico,p.cep,p.logradouro,p.numero,
                 p.complemento,p.bairro,p.cidade,p.uf,p.latitude,p.longitude,p.precisao_localizacao,p.status,
                 COUNT(u.id) AS funcionarios
                 FROM intranet_postos p LEFT JOIN intranet_usuarios u
                 ON TRIM(u.centro_custo)=p.centro_custo_nominal AND u.ativo=TRUE
                 GROUP BY p.id ORDER BY p.centro_custo_nominal''')
            nomes=[c.name for c in cur.description]
            return [{k:float(v) if isinstance(v,Decimal) else v for k,v in zip(nomes,r)} for r in cur.fetchall()]

@app.put('/api/postos/{posto_id}')
def atualizar_posto_geografico(posto_id:int,dados:PostoGeograficoRequest,authorization:Optional[str]=Header(None)):
    exigir_nivel(authorization,3)
    valores=_validar_posto(dados)
    campos=list(valores)
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE intranet_postos SET '+', '.join(f'{campo}=%s' for campo in campos)+', atualizado_em=NOW() WHERE id=%s RETURNING id',tuple(valores.values())+(posto_id,))
            if cur.fetchone() is None: raise HTTPException(404,'Posto não encontrado')
    return {'mensagem':'Posto atualizado','id':posto_id}


# PILOTO ISOLADO - CONFIRMACAO DE AVISO POR LINK
from avisos_whatsapp_teste_api import configurar as configurar_avisos_link_teste
app.include_router(configurar_avisos_link_teste(conectar, exigir_nivel))
