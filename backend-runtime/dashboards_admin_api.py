from typing import Optional
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException

router_dashboards_admin = APIRouter(
    prefix="/api/dashboards/admin",
    tags=["Dashboards - Administracao"]
)

_conectar = None
_autenticar = None


def configurar_dashboards_admin(conectar, autenticar):
    global _conectar, _autenticar
    _conectar = conectar
    _autenticar = autenticar


def exigir_admin(authorization: Optional[str]):
    if _autenticar is None:
        raise HTTPException(
            status_code=503,
            detail="Modulo de dashboards nao configurado"
        )

    usuario = _autenticar(authorization)

    if usuario[5]:
        raise HTTPException(
            status_code=403,
            detail="Troca de senha obrigatoria"
        )

    if usuario[3] != 3:
        raise HTTPException(
            status_code=403,
            detail="Acesso exclusivo ao administrador"
        )

    return usuario


@router_dashboards_admin.get("/status")
def status_dashboards_admin(
    authorization: Optional[str] = Header(None)
):
    usuario = exigir_admin(authorization)

    return {
        "status": "online",
        "modulo": "dashboards_admin",
        "usuario_id": usuario[0],
        "nivel_acesso": usuario[3]
    }
# CADASTRO DE CAIXINHAS
from pydantic import BaseModel, Field
from psycopg.errors import UniqueViolation


class CaixinhaCriar(BaseModel):
    nome: str = Field(min_length=1, max_length=120)
    descricao: Optional[str] = None
    icone: Optional[str] = Field(default=None, max_length=80)
    ordem: int = 0


@router_dashboards_admin.post("/caixinhas", status_code=201)
def criar_caixinha(
    dados: CaixinhaCriar,
    authorization: Optional[str] = Header(None)
):
    exigir_admin(authorization)

    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")

    nome = dados.nome.strip()

    if not nome:
        raise HTTPException(422, "Nome obrigatorio")

    try:
        with _conectar() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO intranet_dash_caixinhas
                        (nome, descricao, icone, ordem)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, nome, descricao,
                              icone, ordem, ativo
                    """,
                    (
                        nome,
                        dados.descricao,
                        dados.icone,
                        dados.ordem
                    )
                )

                registro = cur.fetchone()

        return {
            "id": registro[0],
            "nome": registro[1],
            "descricao": registro[2],
            "icone": registro[3],
            "ordem": registro[4],
            "ativo": registro[5]
        }

    except UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="Ja existe uma caixinha com esse nome"
        )

# LISTAGEM ADMINISTRATIVA DE CAIXINHAS
@router_dashboards_admin.get("/caixinhas")
def listar_caixinhas(
    authorization: Optional[str] = Header(None)
):
    exigir_admin(authorization)

    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, nome, descricao, icone,
                       ordem, ativo, criado_em, atualizado_em
                FROM intranet_dash_caixinhas
                ORDER BY ordem, nome, id
            """)

            registros = cur.fetchall()

    return [
        {
            "id": r[0],
            "nome": r[1],
            "descricao": r[2],
            "icone": r[3],
            "ordem": r[4],
            "ativo": r[5],
            "criado_em": r[6],
            "atualizado_em": r[7]
        }
        for r in registros
    ]

# ATUALIZACAO DE CAIXINHAS
class CaixinhaAtualizar(BaseModel):
    nome: str = Field(min_length=1, max_length=120)
    descricao: Optional[str] = None
    icone: Optional[str] = Field(default=None, max_length=80)
    ordem: int = 0
    ativo: bool = True


@router_dashboards_admin.put("/caixinhas/{caixinha_id}")
def atualizar_caixinha(
    caixinha_id: int,
    dados: CaixinhaAtualizar,
    authorization: Optional[str] = Header(None)
):
    exigir_admin(authorization)

    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")

    if caixinha_id <= 0:
        raise HTTPException(422, "ID invalido")

    nome = dados.nome.strip()

    if not nome:
        raise HTTPException(422, "Nome obrigatorio")

    try:
        with _conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE intranet_dash_caixinhas
                    SET nome = %s,
                        descricao = %s,
                        icone = %s,
                        ordem = %s,
                        ativo = %s,
                        atualizado_em = NOW()
                    WHERE id = %s
                    RETURNING id, nome, descricao,
                              icone, ordem, ativo
                """, (
                    nome,
                    dados.descricao,
                    dados.icone,
                    dados.ordem,
                    dados.ativo,
                    caixinha_id
                ))

                registro = cur.fetchone()

        if registro is None:
            raise HTTPException(
                status_code=404,
                detail="Caixinha nao encontrada"
            )

        return {
            "id": registro[0],
            "nome": registro[1],
            "descricao": registro[2],
            "icone": registro[3],
            "ordem": registro[4],
            "ativo": registro[5]
        }

    except UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="Ja existe uma caixinha com esse nome"
        )

# CATALOGO DE DASHBOARDS DESENVOLVIDOS E AUTORIZADOS
DASHBOARDS_DISPONIVEIS = {
    "auditoria": "/dashboard-auditoria.html",
    "pesquisas": "/dashboard-pesquisas.html"
}


def obter_rota_dashboard(codigo: str) -> str:
    rota = DASHBOARDS_DISPONIVEIS.get(codigo)

    if rota is None:
        raise HTTPException(
            status_code=422,
            detail="Dashboard nao autorizado ou nao publicado"
        )

    return rota

# CADASTRO DE DASHBOARDS
class DashboardCriar(BaseModel):
    nome: str = Field(min_length=1, max_length=150)
    descricao: Optional[str] = None
    codigo: str = Field(min_length=1, max_length=100)


@router_dashboards_admin.post("/dashboards", status_code=201)
def criar_dashboard(
    dados: DashboardCriar,
    authorization: Optional[str] = Header(None)
):
    exigir_admin(authorization)

    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")

    nome = dados.nome.strip()
    codigo = dados.codigo.strip().lower()

    if not nome:
        raise HTTPException(422, "Nome obrigatorio")

    rota = obter_rota_dashboard(codigo)

    try:
        with _conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intranet_dash_dashboards
                        (nome, descricao, codigo, rota_interna)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, nome, descricao,
                              codigo, rota_interna, ativo
                """, (
                    nome,
                    dados.descricao,
                    codigo,
                    rota
                ))

                registro = cur.fetchone()

        return {
            "id": registro[0],
            "nome": registro[1],
            "descricao": registro[2],
            "codigo": registro[3],
            "rota_interna": registro[4],
            "ativo": registro[5]
        }

    except UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="Dashboard ja cadastrado"
        )


# IMPORTACAO DE DASHBOARDS HTML
from uuid import uuid4
from fastapi import UploadFile, File, Form

DIRETORIO_DASHBOARDS = Path("/intranet/documentos_upload/dashboards-importados")

@router_dashboards_admin.post("/dashboards/importar", status_code=201)
async def importar_dashboard(
    nome: str = Form(...),
    descricao: str = Form(""),
    arquivo: UploadFile = File(...),
    authorization: Optional[str] = Header(None)
):
    exigir_admin(authorization)

    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")

    nome = nome.strip()

    if not nome or len(nome) > 150:
        raise HTTPException(422, "Nome invalido")

    extensao = Path(arquivo.filename or "").suffix.lower()

    if extensao not in (".html", ".htm"):
        raise HTTPException(422, "Somente arquivos HTML")

    conteudo = await arquivo.read(5 * 1024 * 1024 + 1)

    if len(conteudo) > 5 * 1024 * 1024:
        raise HTTPException(413, "Arquivo excede 5 MB")

    codigo = uuid4().hex
    nome_arquivo = codigo + ".html"
    rota = "/api/dashboards/visualizar/" + codigo

    DIRETORIO_DASHBOARDS.mkdir(parents=True, exist_ok=True)
    destino = DIRETORIO_DASHBOARDS / nome_arquivo

    destino.write_bytes(conteudo)

    try:
        with _conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intranet_dash_dashboards
                        (nome, descricao, codigo, rota_interna, ativo)
                    VALUES (%s, %s, %s, %s, FALSE)
                    RETURNING id, nome, descricao, codigo,
                              rota_interna, ativo
                """, (nome, descricao, codigo, rota))

                r = cur.fetchone()

        return {
            "id": r[0],
            "nome": r[1],
            "descricao": r[2],
            "codigo": r[3],
            "rota_interna": r[4],
            "ativo": r[5]
        }

    except Exception:
        destino.unlink(missing_ok=True)
        raise

# Substituição segura do HTML existente, preservando o cadastro e as permissões.
@router_dashboards_admin.put("/dashboards/{dashboard_id}/arquivo")
async def substituir_arquivo_dashboard(
    dashboard_id: int,
    arquivo: UploadFile = File(...),
    authorization: Optional[str] = Header(None)
):
    exigir_admin(authorization)
    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")
    if dashboard_id <= 0:
        raise HTTPException(422, "ID invalido")
    if Path(arquivo.filename or "").suffix.lower() not in (".html", ".htm"):
        raise HTTPException(422, "Somente arquivos HTML")
    conteudo = await arquivo.read(5 * 1024 * 1024 + 1)
    if not conteudo or len(conteudo) > 5 * 1024 * 1024:
        raise HTTPException(413, "HTML vazio ou excede 5 MB")

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT codigo, rota_interna FROM intranet_dash_dashboards WHERE id = %s", (dashboard_id,))
            registro = cur.fetchone()
    if not registro:
        raise HTTPException(404, "Dashboard nao encontrado")
    codigo, rota = registro
    if not isinstance(codigo, str) or len(codigo) != 32 or any(c not in '0123456789abcdef' for c in codigo):
        raise HTTPException(409, "Somente dashboards HTML importados podem ter o arquivo substituido")
    if rota != '/api/dashboards/visualizar/' + codigo:
        raise HTTPException(409, "Rota do dashboard nao corresponde ao arquivo importado")

    import os
    import tempfile
    destino = DIRETORIO_DASHBOARDS / (codigo + '.html')
    if not destino.is_file():
        raise HTTPException(404, "Arquivo HTML original nao encontrado")
    temporario = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=str(DIRETORIO_DASHBOARDS), prefix=codigo+'-', suffix='.tmp', delete=False) as f:
            temporario = f.name
            f.write(conteudo)
        os.replace(temporario, destino)
    finally:
        if temporario and os.path.exists(temporario):
            os.unlink(temporario)
    return {"ok": True, "id": dashboard_id, "codigo": codigo, "arquivo_substituido": True}

# LISTAGEM ADMINISTRATIVA DE DASHBOARDS
@router_dashboards_admin.get("/dashboards")
def listar_dashboards(
    authorization: Optional[str] = Header(None)
):
    exigir_admin(authorization)

    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, nome, descricao, codigo,
                       rota_interna, ativo,
                       criado_em, atualizado_em
                FROM intranet_dash_dashboards
                ORDER BY nome, id
            """)

            registros = cur.fetchall()

    return [
        {
            "id": r[0],
            "nome": r[1],
            "descricao": r[2],
            "codigo": r[3],
            "rota_interna": r[4],
            "ativo": r[5],
            "criado_em": r[6],
            "atualizado_em": r[7]
        }
        for r in registros
    ]

# ATUALIZACAO DE DASHBOARDS
class DashboardAtualizar(BaseModel):
    nome: str = Field(min_length=1, max_length=150)
    descricao: Optional[str] = None
    ativo: bool = True


@router_dashboards_admin.put("/dashboards/{dashboard_id}")
def atualizar_dashboard(
    dashboard_id: int,
    dados: DashboardAtualizar,
    authorization: Optional[str] = Header(None)
):
    exigir_admin(authorization)

    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")

    if dashboard_id <= 0:
        raise HTTPException(422, "ID invalido")

    nome = dados.nome.strip()

    if not nome:
        raise HTTPException(422, "Nome obrigatorio")

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE intranet_dash_dashboards
                SET nome = %s,
                    descricao = %s,
                    ativo = %s,
                    atualizado_em = NOW()
                WHERE id = %s
                RETURNING id, nome, descricao,
                          codigo, rota_interna, ativo
            """, (
                nome,
                dados.descricao,
                dados.ativo,
                dashboard_id
            ))

            registro = cur.fetchone()

    if registro is None:
        raise HTTPException(
            status_code=404,
            detail="Dashboard nao encontrado"
        )

    return {
        "id": registro[0],
        "nome": registro[1],
        "descricao": registro[2],
        "codigo": registro[3],
        "rota_interna": registro[4],
        "ativo": registro[5]
    }

# VINCULOS ENTRE CAIXINHAS E DASHBOARDS
class VinculoCriar(BaseModel):
    caixinha_id: int = Field(gt=0)
    dashboard_id: int = Field(gt=0)
    ordem: int = 0


@router_dashboards_admin.post("/vinculos", status_code=201)
def criar_vinculo(
    dados: VinculoCriar,
    authorization: Optional[str] = Header(None)
):
    exigir_admin(authorization)

    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")

    with _conectar() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    EXISTS(
                        SELECT 1
                        FROM intranet_dash_caixinhas
                        WHERE id = %s
                    ),
                    EXISTS(
                        SELECT 1
                        FROM intranet_dash_dashboards
                        WHERE id = %s
                    )
            """, (dados.caixinha_id, dados.dashboard_id))

            existe_caixinha, existe_dashboard = cur.fetchone()

            if not existe_caixinha:
                raise HTTPException(404, "Caixinha nao encontrada")

            if not existe_dashboard:
                raise HTTPException(404, "Dashboard nao encontrado")

            cur.execute("""
                INSERT INTO intranet_dash_vinculos
                    (caixinha_id, dashboard_id, ordem)
                VALUES (%s, %s, %s)
                ON CONFLICT (caixinha_id, dashboard_id)
                DO NOTHING
                RETURNING id, caixinha_id, dashboard_id, ordem
            """, (
                dados.caixinha_id,
                dados.dashboard_id,
                dados.ordem
            ))

            registro = cur.fetchone()

            if registro is None:
                raise HTTPException(
                    409,
                    "Dashboard ja vinculado a esta caixinha"
                )

    return {
        "id": registro[0],
        "caixinha_id": registro[1],
        "dashboard_id": registro[2],
        "ordem": registro[3]
    }

# LISTAGEM ADMINISTRATIVA DOS VINCULOS
@router_dashboards_admin.get("/vinculos")
def listar_vinculos(
    authorization: Optional[str] = Header(None)
):
    exigir_admin(authorization)

    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    v.id,
                    v.caixinha_id,
                    c.nome,
                    v.dashboard_id,
                    d.nome,
                    v.ordem,
                    c.ativo,
                    d.ativo
                FROM intranet_dash_vinculos v
                JOIN intranet_dash_caixinhas c
                    ON c.id = v.caixinha_id
                JOIN intranet_dash_dashboards d
                    ON d.id = v.dashboard_id
                ORDER BY c.ordem, c.nome,
                         v.ordem, d.nome, v.id
            """)

            registros = cur.fetchall()

    return [
        {
            "id": r[0],
            "caixinha_id": r[1],
            "caixinha_nome": r[2],
            "dashboard_id": r[3],
            "dashboard_nome": r[4],
            "ordem": r[5],
            "caixinha_ativa": r[6],
            "dashboard_ativo": r[7]
        }
        for r in registros
    ]

# ATUALIZACAO DA ORDEM DOS VINCULOS
class VinculoAtualizar(BaseModel):
    ordem: int = Field(ge=0)


@router_dashboards_admin.put("/vinculos/{vinculo_id}")
def atualizar_vinculo(
    vinculo_id: int,
    dados: VinculoAtualizar,
    authorization: Optional[str] = Header(None)
):
    exigir_admin(authorization)

    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")

    if vinculo_id <= 0:
        raise HTTPException(422, "ID invalido")

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE intranet_dash_vinculos
                SET ordem = %s
                WHERE id = %s
                RETURNING id, caixinha_id,
                          dashboard_id, ordem
            """, (
                dados.ordem,
                vinculo_id
            ))

            registro = cur.fetchone()

    if registro is None:
        raise HTTPException(
            status_code=404,
            detail="Vinculo nao encontrado"
        )

    return {
        "id": registro[0],
        "caixinha_id": registro[1],
        "dashboard_id": registro[2],
        "ordem": registro[3]
    }

# REMOCAO DE VINCULOS
@router_dashboards_admin.delete("/vinculos/{vinculo_id}")
def excluir_vinculo(
    vinculo_id: int,
    authorization: Optional[str] = Header(None)
):
    exigir_admin(authorization)

    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")

    if vinculo_id <= 0:
        raise HTTPException(422, "ID invalido")

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM intranet_dash_vinculos
                WHERE id = %s
                RETURNING id, caixinha_id, dashboard_id
            """, (vinculo_id,))

            registro = cur.fetchone()

    if registro is None:
        raise HTTPException(
            status_code=404,
            detail="Vinculo nao encontrado"
        )

    return {
        "status": "excluido",
        "id": registro[0],
        "caixinha_id": registro[1],
        "dashboard_id": registro[2]
    }

# PERMISSOES DIRETAMENTE POR VINCULO (CAIXINHA + DASHBOARD)
from pydantic import model_validator


class PermissaoCriar(BaseModel):
    vinculo_id: int = Field(gt=0)
    modalidade: str
    usuario_id: Optional[int] = Field(default=None, gt=0)
    cargo: Optional[str] = Field(default=None, max_length=150)
    departamento: Optional[str] = Field(default=None, max_length=150)

    @model_validator(mode="after")
    def validar_permissao(self):
        self.modalidade = self.modalidade.strip().upper()
        self.cargo = self.cargo.strip() or None if self.cargo is not None else None
        self.departamento = self.departamento.strip() or None if self.departamento is not None else None
        if self.modalidade == "USUARIO":
            valido = self.usuario_id is not None and self.cargo is None and self.departamento is None
        elif self.modalidade == "CARGO":
            valido = self.usuario_id is None and self.cargo is not None and self.departamento is None
        elif self.modalidade == "DEPARTAMENTO":
            valido = self.usuario_id is None and self.cargo is None and self.departamento is not None
        else:
            raise ValueError("Modalidade invalida")
        if not valido:
            raise ValueError("Informe somente o destinatario correspondente a modalidade")
        return self


@router_dashboards_admin.post("/permissoes", status_code=201)
def criar_permissao(dados: PermissaoCriar, authorization: Optional[str] = Header(None)):
    exigir_admin(authorization)
    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM intranet_dash_vinculos v
                JOIN intranet_dash_caixinhas c ON c.id = v.caixinha_id
                JOIN intranet_dash_dashboards d ON d.id = v.dashboard_id
                WHERE v.id = %s AND c.ativo = TRUE AND d.ativo = TRUE
            """, (dados.vinculo_id,))
            if cur.fetchone() is None:
                raise HTTPException(404, "Vinculo inexistente ou inativo")
            if dados.modalidade == "USUARIO":
                cur.execute("SELECT 1 FROM intranet_usuarios WHERE id = %s AND ativo = TRUE", (dados.usuario_id,))
            elif dados.modalidade == "CARGO":
                cur.execute("SELECT 1 FROM intranet_usuarios WHERE cargo = %s AND ativo = TRUE LIMIT 1", (dados.cargo,))
            else:
                cur.execute("SELECT 1 FROM intranet_usuarios WHERE departamento = %s AND ativo = TRUE LIMIT 1", (dados.departamento,))
            if cur.fetchone() is None:
                raise HTTPException(422, "Usuario, cargo ou departamento nao encontrado")
            cur.execute("""
                INSERT INTO intranet_dash_permissoes_vinculos
                    (vinculo_id, modalidade, usuario_id, cargo, departamento)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING RETURNING id
            """, (dados.vinculo_id, dados.modalidade, dados.usuario_id, dados.cargo, dados.departamento))
            registro = cur.fetchone()
            if registro is None:
                raise HTTPException(409, "Permissao ja cadastrada")
    return {"id": registro[0], "status": "criada", "vinculo_id": dados.vinculo_id, "modalidade": dados.modalidade}


@router_dashboards_admin.get("/permissoes")
def listar_permissoes(authorization: Optional[str] = Header(None)):
    exigir_admin(authorization)
    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id, p.vinculo_id, v.caixinha_id, c.nome AS caixinha_nome,
                       v.dashboard_id, d.nome AS dashboard_nome, p.modalidade,
                       p.usuario_id, u.nome AS usuario_nome, p.cargo,
                       p.departamento, p.criado_em
                FROM intranet_dash_permissoes_vinculos p
                JOIN intranet_dash_vinculos v ON v.id = p.vinculo_id
                JOIN intranet_dash_caixinhas c ON c.id = v.caixinha_id
                JOIN intranet_dash_dashboards d ON d.id = v.dashboard_id
                LEFT JOIN intranet_usuarios u ON u.id = p.usuario_id
                ORDER BY c.nome, d.nome, p.modalidade, p.id
            """)
            colunas = [col.name for col in cur.description]
            registros = [dict(zip(colunas, linha)) for linha in cur.fetchall()]
    return {"total": len(registros), "permissoes": registros}


@router_dashboards_admin.delete("/permissoes/{permissao_id}")
def excluir_permissao(permissao_id: int, authorization: Optional[str] = Header(None)):
    exigir_admin(authorization)
    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")
    if permissao_id <= 0:
        raise HTTPException(422, "ID invalido")
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM intranet_dash_permissoes_vinculos
                WHERE id = %s
                RETURNING id, vinculo_id, modalidade, usuario_id, cargo, departamento
            """, (permissao_id,))
            registro = cur.fetchone()
    if registro is None:
        raise HTTPException(404, "Permissao nao encontrada")
    return {"status": "revogada", "id": registro[0], "vinculo_id": registro[1],
            "modalidade": registro[2], "usuario_id": registro[3],
            "cargo": registro[4], "departamento": registro[5]}
