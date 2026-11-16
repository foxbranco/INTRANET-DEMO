"""Intranet V12 - Avaliacoes de Desempenho 90/180.

Nao altera schema. Integracao: configurar_avaliacoes(conectar, autenticar).
"""
from datetime import date
from decimal import Decimal
from typing import Optional, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, model_validator
from psycopg.rows import dict_row

router_avaliacoes = APIRouter(prefix='/api/avaliacoes', tags=['Avaliacoes'])
_conectar = None
_autenticar = None


def configurar_avaliacoes(conectar, autenticar):
    global _conectar, _autenticar
    _conectar, _autenticar = conectar, autenticar


def usuario_atual(authorization):
    if _conectar is None or _autenticar is None:
        raise HTTPException(503, 'Avaliacoes nao configuradas')
    u = _autenticar(authorization)
    if u[5]:
        raise HTTPException(403, 'Troca de senha obrigatoria')
    return u


def exigir_admin(u):
    if u[3] == 3:
        return
    if u[3] != 2:
        raise HTTPException(403, 'Acesso administrativo negado')
    with _conectar() as conn, conn.cursor() as cur:
        cur.execute('''SELECT 1 FROM intranet_admin_permissoes p
            JOIN intranet_admin_modulos m ON m.id=p.modulo_id
            WHERE p.usuario_id=%s AND p.ativo=TRUE AND m.ativo=TRUE
              AND m.codigo='AVALIACOES' LIMIT 1''', (u[0],))
        if cur.fetchone() is None:
            raise HTTPException(403, 'Sem permissao para administrar avaliacoes')


def ciclo(cur, ciclo_id, bloquear=False):
    cur.execute('SELECT * FROM intranet_aval_ciclos WHERE id=%s' +
                (' FOR UPDATE' if bloquear else ''), (ciclo_id,))
    c = cur.fetchone()
    if not c:
        raise HTTPException(404, 'Ciclo nao encontrado')
    return c


def exigir_rascunho(c):
    if c['status'] != 'RASCUNHO':
        raise HTTPException(409, 'Somente ciclos em rascunho podem ser alterados')


class CicloNovo(BaseModel):
    nome: str = Field(min_length=3, max_length=150)
    modalidade: Literal[90, 180]
    data_inicio: date
    data_fim: date

    @model_validator(mode='after')
    def validar(self):
        if self.data_fim < self.data_inicio:
            raise ValueError('Data final anterior a inicial')
        if not self.nome.strip():
            raise ValueError('Nome obrigatorio')
        return self


class CriterioNovo(BaseModel):
    titulo: str = Field(min_length=2, max_length=150)
    descricao: Optional[str] = None
    peso: Decimal = Field(default=Decimal('1'), gt=0, le=Decimal('9999.99'))
    ordem: int = 0
    tipo: Literal['CRITERIO', 'META'] = 'CRITERIO'
    meta_valor: Optional[Decimal] = None
    meta_unidade: Optional[str] = Field(default=None, max_length=50)
    meta_direcao: Optional[Literal['MAIOR_MELHOR', 'MENOR_MELHOR']] = None
    competencia_meta: int = Field(default=3, ge=1, le=5)
    operador_meta: Literal['>=', '>', '<=', '<', '='] = '>='

    @model_validator(mode='after')
    def validar(self):
        if not self.titulo.strip():
            raise ValueError('Titulo obrigatorio')
        if self.tipo == 'META' and (self.meta_valor is None or self.meta_direcao is None):
            raise ValueError('Meta exige valor e direcao')
        if self.tipo == 'META' and self.meta_valor < 0:
            raise ValueError('Valor da meta nao pode ser negativo')
        if self.tipo == 'CRITERIO' and any(x is not None for x in (self.meta_valor, self.meta_unidade, self.meta_direcao)):
            raise ValueError('Campos numericos nao se aplicam a competencia')
        return self


class ParticipantesNovos(BaseModel):
    funcionario_ids: list[int] = Field(min_length=1)

    @model_validator(mode='after')
    def validar(self):
        if len(self.funcionario_ids) != len(set(self.funcionario_ids)):
            raise ValueError('Funcionarios duplicados')
        if any(x <= 0 for x in self.funcionario_ids):
            raise ValueError('IDs invalidos')
        return self


class RespostaDados(BaseModel):
    criterio_id: int = Field(gt=0)
    nota: Optional[int] = Field(default=None, ge=1, le=5)
    comentario: Optional[str] = None
    resultado_alcancado: Optional[Decimal] = None


class RespostasLote(BaseModel):
    respostas: list[RespostaDados] = Field(min_length=1)

    @model_validator(mode='after')
    def validar(self):
        ids = [x.criterio_id for x in self.respostas]
        if len(ids) != len(set(ids)):
            raise ValueError('Criterios duplicados')
        return self




# Biblioteca reutilizavel: cada ciclo guarda sua propria copia dos valores.
class BibliotecaDados(BaseModel):
    titulo: str = Field(min_length=2, max_length=150)
    descricao: Optional[str] = None
    tipo: Literal['CRITERIO', 'META'] = 'CRITERIO'
    nota_esperada: Optional[int] = Field(default=3, ge=1, le=5)
    meta_valor: Optional[Decimal] = None
    meta_unidade: Optional[str] = Field(default=None, max_length=50)
    meta_direcao: Optional[Literal['MAIOR_MELHOR', 'MENOR_MELHOR']] = None
    meta_operador: Optional[Literal['>=', '>', '<=', '<', '=']] = '>='
    ativo: bool = True

    @model_validator(mode='after')
    def validar(self):
        if not self.titulo.strip():
            raise ValueError('Titulo obrigatorio')
        if self.tipo == 'META':
            if self.meta_valor is None or self.meta_valor < 0:
                raise ValueError('Meta exige valor esperado nao negativo')
            if self.meta_direcao is None:
                raise ValueError('Informe a direcao da meta')
        return self


class SelecionarBiblioteca(BaseModel):
    biblioteca_id: int = Field(gt=0)
    peso: Decimal = Field(default=Decimal('1'), gt=0, le=Decimal('9999.99'))
    ordem: int = Field(default=0, ge=0)


class OrganizarItem(BaseModel):
    peso: Decimal = Field(gt=0, le=Decimal('9999.99'))
    ordem: int = Field(ge=0)


@router_avaliacoes.get('/admin/biblioteca')
def listar_biblioteca(authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''SELECT b.*, EXISTS(SELECT 1 FROM intranet_aval_criterios c
                       WHERE c.biblioteca_id=b.id) AS utilizada
                       FROM intranet_aval_biblioteca b ORDER BY b.tipo,b.titulo,b.id''')
        return cur.fetchall()


@router_avaliacoes.post('/admin/biblioteca', status_code=201)
def criar_biblioteca(dados: BibliotecaDados, authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''INSERT INTO intranet_aval_biblioteca
            (titulo,descricao,tipo,nota_esperada,meta_valor,meta_unidade,
             meta_direcao,meta_operador,ativo,criado_por)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (dados.titulo.strip(),dados.descricao,dados.tipo,
             dados.nota_esperada if dados.tipo=='CRITERIO' else None,
             dados.meta_valor if dados.tipo=='META' else None,
             dados.meta_unidade if dados.tipo=='META' else None,
             dados.meta_direcao if dados.tipo=='META' else None,
             dados.meta_operador if dados.tipo=='META' else None,dados.ativo,u[0]))
        return cur.fetchone()


@router_avaliacoes.put('/admin/biblioteca/{item_id}')
def editar_biblioteca(item_id: int, dados: BibliotecaDados,
                     authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('SELECT * FROM intranet_aval_biblioteca WHERE id=%s FOR UPDATE',(item_id,))
        anterior=cur.fetchone()
        if not anterior:
            raise HTTPException(404,'Item nao encontrado')
        cur.execute('SELECT 1 FROM intranet_aval_criterios WHERE biblioteca_id=%s LIMIT 1',(item_id,))
        if cur.fetchone() and (dados.titulo.strip()!=anterior['titulo'] or dados.tipo!=anterior['tipo']):
            raise HTTPException(409,'Titulo e tipo bloqueados: item ja utilizado')
        cur.execute('''UPDATE intranet_aval_biblioteca SET titulo=%s,descricao=%s,tipo=%s,
            nota_esperada=%s,meta_valor=%s,meta_unidade=%s,meta_direcao=%s,
            meta_operador=%s,ativo=%s,atualizado_em=CURRENT_TIMESTAMP
            WHERE id=%s RETURNING *''',
            (dados.titulo.strip(),dados.descricao,dados.tipo,
             dados.nota_esperada if dados.tipo=='CRITERIO' else None,
             dados.meta_valor if dados.tipo=='META' else None,
             dados.meta_unidade if dados.tipo=='META' else None,
             dados.meta_direcao if dados.tipo=='META' else None,
             dados.meta_operador if dados.tipo=='META' else None,dados.ativo,item_id))
        return cur.fetchone()


@router_avaliacoes.post('/admin/ciclos/{ciclo_id}/biblioteca', status_code=201)
def selecionar_item_biblioteca(ciclo_id: int, dados: SelecionarBiblioteca,
                              authorization: Optional[str] = Header(None)):
    u=usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        exigir_rascunho(ciclo(cur,ciclo_id,True))
        cur.execute('SELECT * FROM intranet_aval_biblioteca WHERE id=%s AND ativo=TRUE',(dados.biblioteca_id,))
        b=cur.fetchone()
        if not b:
            raise HTTPException(404,'Item ativo nao encontrado na biblioteca')
        cur.execute('SELECT 1 FROM intranet_aval_criterios WHERE ciclo_id=%s AND biblioteca_id=%s',
                    (ciclo_id,dados.biblioteca_id))
        if cur.fetchone():
            raise HTTPException(409,'Este item ja esta no ciclo')
        cur.execute('''INSERT INTO intranet_aval_criterios
            (ciclo_id,biblioteca_id,titulo,descricao,peso,ordem,tipo,meta_valor,
             meta_unidade,meta_direcao,meta_operador,nota_esperada,
             competencia_meta,operador_meta)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (ciclo_id,b['id'],b['titulo'],b['descricao'],dados.peso,dados.ordem,
             b['tipo'],b['meta_valor'],b['meta_unidade'],b['meta_direcao'],
             b['meta_operador'],b['nota_esperada'],b['nota_esperada'] or 3,
             b['meta_operador'] or '>='))
        return cur.fetchone()


@router_avaliacoes.put('/admin/ciclos/{ciclo_id}/organizar/{criterio_id}')
def organizar_criterio(ciclo_id: int, criterio_id: int, dados: OrganizarItem,
                      authorization: Optional[str] = Header(None)):
    u=usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        exigir_rascunho(ciclo(cur,ciclo_id,True))
        cur.execute('''UPDATE intranet_aval_criterios SET peso=%s,ordem=%s
            WHERE id=%s AND ciclo_id=%s RETURNING *''',
            (dados.peso,dados.ordem,criterio_id,ciclo_id))
        r=cur.fetchone()
        if not r:
            raise HTTPException(404,'Item nao encontrado no ciclo')
        return r


@router_avaliacoes.get('/admin/ciclos')
def listar_ciclos(authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''SELECT id,nome,modalidade,data_inicio,data_fim,status,
                       criado_por,criado_em,publicado_em,encerrado_em
                       FROM intranet_aval_ciclos ORDER BY id DESC''')
        return cur.fetchall()


@router_avaliacoes.post('/admin/ciclos', status_code=201)
def criar_ciclo(dados: CicloNovo, authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''INSERT INTO intranet_aval_ciclos
            (nome,modalidade,data_inicio,data_fim,status,criado_por)
            VALUES (%s,%s,%s,%s,'RASCUNHO',%s)
            RETURNING id,nome,modalidade,data_inicio,data_fim,status''',
            (dados.nome.strip(), dados.modalidade, dados.data_inicio, dados.data_fim, u[0]))
        return cur.fetchone()


@router_avaliacoes.post('/admin/ciclos/{ciclo_id}/duplicar', status_code=201)
def duplicar_ciclo(ciclo_id: int, dados: CicloNovo,
                   authorization: Optional[str] = Header(None)):
    # Duplica somente a configuracao, sem participantes.
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        original = ciclo(cur, ciclo_id, True)
        if original['status'] != 'ENCERRADO':
            raise HTTPException(409, 'Somente ciclos encerrados podem ser duplicados')
        if dados.modalidade != original['modalidade']:
            raise HTTPException(409, 'A modalidade do ciclo duplicado deve ser preservada')
        cur.execute('''INSERT INTO intranet_aval_ciclos
            (nome,modalidade,data_inicio,data_fim,status,criado_por)
            VALUES (%s,%s,%s,%s,'RASCUNHO',%s) RETURNING id,nome,modalidade,data_inicio,data_fim,status''',
            (dados.nome.strip(), original['modalidade'], dados.data_inicio, dados.data_fim, u[0]))
        novo = cur.fetchone()
        cur.execute('''INSERT INTO intranet_aval_criterios
            (ciclo_id,biblioteca_id,titulo,descricao,peso,ordem,tipo,meta_valor,
             meta_unidade,meta_direcao,meta_operador,nota_esperada,
             competencia_meta,operador_meta,ativo)
            SELECT %s,biblioteca_id,titulo,descricao,peso,ordem,tipo,meta_valor,
                   meta_unidade,meta_direcao,meta_operador,nota_esperada,
                   competencia_meta,operador_meta,ativo
            FROM intranet_aval_criterios WHERE ciclo_id=%s''', (novo['id'], ciclo_id))
        return {**novo, 'criterios_copiados': cur.rowcount, 'participantes_copiados': 0}


@router_avaliacoes.put('/admin/ciclos/{ciclo_id}')
def atualizar_ciclo(ciclo_id: int, dados: CicloNovo,
                    authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        atual = ciclo(cur, ciclo_id, True)
        exigir_rascunho(atual)
        if dados.modalidade != atual['modalidade']:
            raise HTTPException(409, 'Modalidade 90/180 nao pode ser alterada apos criar o ciclo')
        cur.execute("""UPDATE intranet_aval_ciclos SET nome=%s,modalidade=%s,
            data_inicio=%s,data_fim=%s,atualizado_em=CURRENT_TIMESTAMP
            WHERE id=%s RETURNING id,nome,modalidade,data_inicio,data_fim,status""",
            (dados.nome.strip(),dados.modalidade,dados.data_inicio,dados.data_fim,ciclo_id))
        return cur.fetchone()


@router_avaliacoes.delete('/admin/ciclos/{ciclo_id}')
def excluir_ciclo(ciclo_id: int, authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        atual = ciclo(cur, ciclo_id, True)
        exigir_rascunho(atual)
        cur.execute('SELECT 1 FROM intranet_aval_avaliacoes a JOIN intranet_aval_participantes p ON p.id=a.participante_id WHERE p.ciclo_id=%s LIMIT 1', (ciclo_id,))
        if cur.fetchone():
            raise HTTPException(409, 'Ciclo possui avaliacoes; exclusao bloqueada')
        cur.execute('DELETE FROM intranet_aval_participantes WHERE ciclo_id=%s', (ciclo_id,))
        cur.execute('DELETE FROM intranet_aval_criterios WHERE ciclo_id=%s', (ciclo_id,))
        cur.execute('DELETE FROM intranet_aval_ciclos WHERE id=%s', (ciclo_id,))
        return {'excluido': True, 'id': ciclo_id}


@router_avaliacoes.get('/admin/ciclos/{ciclo_id}')
def detalhe_ciclo_admin(ciclo_id: int, authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        c = ciclo(cur, ciclo_id)
        cur.execute('''SELECT * FROM intranet_aval_criterios WHERE ciclo_id=%s
                       ORDER BY ordem,id''', (ciclo_id,))
        criterios = cur.fetchall()
        cur.execute('''SELECT p.*, f.nome AS funcionario_nome, g.nome AS gestor_nome
            FROM intranet_aval_participantes p
            JOIN intranet_usuarios f ON f.id=p.funcionario_id
            JOIN intranet_usuarios g ON g.id=p.gestor_id
            WHERE p.ciclo_id=%s ORDER BY f.nome''', (ciclo_id,))
        return {'ciclo': c, 'criterios': criterios, 'participantes': cur.fetchall()}


@router_avaliacoes.post('/admin/ciclos/{ciclo_id}/criterios', status_code=201)
def adicionar_criterio(ciclo_id: int, dados: CriterioNovo,
                       authorization: Optional[str] = Header(None)):
    raise HTTPException(409, 'Selecione um item da biblioteca para incluir no ciclo')
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        exigir_rascunho(ciclo(cur, ciclo_id, True))
        cur.execute('''INSERT INTO intranet_aval_criterios
            (ciclo_id,titulo,descricao,peso,ordem,tipo,meta_valor,meta_unidade,meta_direcao,competencia_meta,operador_meta)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (ciclo_id,dados.titulo.strip(),dados.descricao,dados.peso,dados.ordem,
             dados.tipo,dados.meta_valor,dados.meta_unidade,dados.meta_direcao,dados.competencia_meta,dados.operador_meta))
        return cur.fetchone()


@router_avaliacoes.put('/admin/ciclos/{ciclo_id}/criterios/{criterio_id}')
def atualizar_criterio(ciclo_id: int, criterio_id: int, dados: CriterioNovo,
                      authorization: Optional[str] = Header(None)):
    raise HTTPException(409, 'Use a biblioteca para editar dados e organizar para peso e ordem')
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        exigir_rascunho(ciclo(cur, ciclo_id, True))
        cur.execute("""UPDATE intranet_aval_criterios SET titulo=%s,descricao=%s,
            peso=%s,ordem=%s,tipo=%s,meta_valor=%s,meta_unidade=%s,meta_direcao=%s,
             competencia_meta=%s,operador_meta=%s
            WHERE id=%s AND ciclo_id=%s RETURNING *""",
            (dados.titulo.strip(),dados.descricao,dados.peso,dados.ordem,dados.tipo,
             dados.meta_valor,dados.meta_unidade,dados.meta_direcao,dados.competencia_meta,dados.operador_meta,criterio_id,ciclo_id))
        result = cur.fetchone()
        if not result:
            raise HTTPException(404, 'Criterio nao encontrado')
        return result


@router_avaliacoes.delete('/admin/ciclos/{ciclo_id}/criterios/{criterio_id}')
def excluir_criterio(ciclo_id: int, criterio_id: int,
                     authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        exigir_rascunho(ciclo(cur, ciclo_id, True))
        cur.execute('''DELETE FROM intranet_aval_criterios
                       WHERE id=%s AND ciclo_id=%s RETURNING id''', (criterio_id,ciclo_id))
        if not cur.fetchone():
            raise HTTPException(404, 'Criterio nao encontrado')
        return {'excluido': True}


@router_avaliacoes.get('/admin/ciclos/{ciclo_id}/elegiveis')
def listar_funcionarios_elegiveis(ciclo_id: int, authorization: Optional[str] = Header(None)):
    """Lista apenas funcionarios ativos com vinculo direto ativo a gestor ativo tipo 2."""
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        atual = ciclo(cur, ciclo_id)
        exigir_rascunho(atual)
        cur.execute('''
            WITH vinculos_ativos AS (
                SELECT DISTINCT ON (v.funcionario_id)
                    v.id AS vinculo_id, v.funcionario_id, v.gestor_id
                FROM intranet_gestores_funcionarios v
                JOIN intranet_usuarios f
                  ON f.id = v.funcionario_id AND f.ativo = TRUE
                JOIN intranet_usuarios g
                  ON g.id = v.gestor_id AND g.ativo = TRUE AND g.tipo = 2
                WHERE v.ativo = TRUE AND v.funcionario_id <> v.gestor_id
                ORDER BY v.funcionario_id, v.inicio_em DESC NULLS LAST, v.id DESC
            )
            SELECT f.id AS funcionario_id, f.nome AS funcionario_nome,
                   f.departamento, f.centro_custo, f.cargo,
                   va.gestor_id, g.nome AS gestor_nome, va.vinculo_id
            FROM vinculos_ativos va
            JOIN intranet_usuarios f ON f.id = va.funcionario_id AND f.ativo = TRUE
            JOIN intranet_usuarios g ON g.id = va.gestor_id AND g.ativo = TRUE AND g.tipo = 2
            ORDER BY f.nome
        ''')
        funcionarios = cur.fetchall()
        cur.execute('''SELECT funcionario_id FROM intranet_aval_participantes
                       WHERE ciclo_id=%s AND status <> 'CANCELADO' ''', (ciclo_id,))
        ja_incluidos = [r['funcionario_id'] for r in cur.fetchall()]
        gestores = sorted(
            [{'gestor_id': r['gestor_id'], 'gestor_nome': r['gestor_nome']}
             for r in funcionarios],
            key=lambda x: x['gestor_nome'].casefold()
        )
        gestores = list({x['gestor_id']: x for x in gestores}.values())
        departamentos = sorted({r['departamento'] for r in funcionarios if r['departamento']})
        centros_custo = sorted({r['centro_custo'] for r in funcionarios if r['centro_custo']})
        return {
            'ciclo_id': ciclo_id,
            'modalidade': atual['modalidade'],
            'funcionarios': funcionarios,
            'gestores': gestores,
            'departamentos': departamentos,
            'centros_custo': centros_custo,
            'ja_incluidos': ja_incluidos,
            'total_elegiveis': len(funcionarios)
        }


@router_avaliacoes.post('/admin/ciclos/{ciclo_id}/participantes')
def adicionar_participantes(ciclo_id: int, dados: ParticipantesNovos,
                           authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        exigir_rascunho(ciclo(cur, ciclo_id, True))
        inseridos = []
        for funcionario_id in dados.funcionario_ids:
            cur.execute('SELECT id FROM intranet_usuarios WHERE id=%s AND ativo=TRUE',
                        (funcionario_id,))
            if not cur.fetchone():
                raise HTTPException(422, f'Funcionario {funcionario_id} inexistente ou inativo')
            cur.execute('''SELECT v.id,v.gestor_id FROM intranet_gestores_funcionarios v
                JOIN intranet_usuarios f ON f.id=v.funcionario_id AND f.ativo=TRUE
                JOIN intranet_usuarios g ON g.id=v.gestor_id AND g.ativo=TRUE AND g.tipo=2
                WHERE v.funcionario_id=%s AND v.ativo=TRUE AND v.funcionario_id<>v.gestor_id
                ORDER BY v.inicio_em DESC NULLS LAST, v.id DESC LIMIT 1''',
                (funcionario_id,))
            vinculo = cur.fetchone()
            if not vinculo or vinculo['gestor_id'] == funcionario_id:
                raise HTTPException(422, f'Funcionario {funcionario_id} sem gestor direto valido')
            cur.execute('''INSERT INTO intranet_aval_participantes
                (ciclo_id,funcionario_id,gestor_id,vinculo_gestor_id,incluido_por)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (ciclo_id,funcionario_id) DO NOTHING RETURNING id''',
                (ciclo_id,funcionario_id,vinculo['gestor_id'],vinculo['id'],u[0]))
            row = cur.fetchone()
            if row:
                inseridos.append(row['id'])
        return {'incluidos': len(inseridos), 'participante_ids': inseridos}


@router_avaliacoes.delete('/admin/ciclos/{ciclo_id}/participantes/{participante_id}')
def excluir_participante(ciclo_id: int, participante_id: int,
                        authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        exigir_rascunho(ciclo(cur, ciclo_id, True))
        cur.execute('''DELETE FROM intranet_aval_participantes
                       WHERE id=%s AND ciclo_id=%s RETURNING id''', (participante_id,ciclo_id))
        if not cur.fetchone():
            raise HTTPException(404, 'Participante nao encontrado')
        return {'excluido': True}


@router_avaliacoes.post('/admin/ciclos/{ciclo_id}/publicar')
def publicar_ciclo(ciclo_id: int, authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        c = ciclo(cur, ciclo_id, True)
        exigir_rascunho(c)
        cur.execute('''SELECT count(*) AS n FROM intranet_aval_criterios
                       WHERE ciclo_id=%s AND ativo=TRUE''', (ciclo_id,))
        if cur.fetchone()['n'] == 0:
            raise HTTPException(409, 'Cadastre pelo menos um criterio')
        cur.execute('''SELECT id,funcionario_id,gestor_id FROM intranet_aval_participantes
                       WHERE ciclo_id=%s AND status='PENDENTE' ORDER BY id FOR UPDATE''',
                    (ciclo_id,))
        participantes = cur.fetchall()
        if not participantes:
            raise HTTPException(409, 'Inclua participantes antes de publicar')
        for p in participantes:
            cur.execute('''INSERT INTO intranet_aval_avaliacoes
                (participante_id,tipo,avaliador_id)
                VALUES (%s,'GESTOR',%s)''', (p['id'],p['gestor_id']))
            if c['modalidade'] == 180:
                cur.execute('''INSERT INTO intranet_aval_avaliacoes
                    (participante_id,tipo,avaliador_id)
                    VALUES (%s,'AUTOAVALIACAO',%s)''', (p['id'],p['funcionario_id']))
        cur.execute('''UPDATE intranet_aval_ciclos
                       SET status='PUBLICADO',publicado_em=CURRENT_TIMESTAMP,
                           atualizado_em=CURRENT_TIMESTAMP WHERE id=%s''', (ciclo_id,))
        return {'ciclo_id': ciclo_id, 'status': 'PUBLICADO',
                'participantes': len(participantes),
                'avaliacoes': len(participantes)*(2 if c['modalidade']==180 else 1)}


@router_avaliacoes.post('/admin/ciclos/{ciclo_id}/encerrar')
def encerrar_ciclo(ciclo_id: int, authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        c = ciclo(cur, ciclo_id, True)
        if c['status'] != 'PUBLICADO':
            raise HTTPException(409, 'Somente ciclos publicados podem ser encerrados')
        cur.execute('''UPDATE intranet_aval_ciclos SET status='ENCERRADO',
                       encerrado_em=CURRENT_TIMESTAMP,atualizado_em=CURRENT_TIMESTAMP
                       WHERE id=%s''', (ciclo_id,))
        return {'ciclo_id': ciclo_id, 'status': 'ENCERRADO'}


@router_avaliacoes.get('/meus')
def minhas_avaliacoes(authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''SELECT a.id AS avaliacao_id,a.tipo,a.status AS avaliacao_status,
                p.id AS participante_id,p.status AS participante_status,
                c.id AS ciclo_id,c.nome,c.modalidade,c.data_inicio,c.data_fim,
                 p.funcionario_id,p.gestor_id,f.nome AS funcionario_nome,
                 CASE WHEN c.modalidade=180 AND a.tipo='GESTOR'
                      THEN (SELECT au.status FROM intranet_aval_avaliacoes au
                            WHERE au.participante_id=p.id AND au.tipo='AUTOAVALIACAO'
                            LIMIT 1)
                      ELSE NULL END AS autoavaliacao_status
            FROM intranet_aval_avaliacoes a
            JOIN intranet_aval_participantes p ON p.id=a.participante_id
            JOIN intranet_aval_ciclos c ON c.id=p.ciclo_id
            JOIN intranet_usuarios f ON f.id=p.funcionario_id
            WHERE c.status IN ('PUBLICADO','ENCERRADO') AND a.avaliador_id=%s
              AND p.status<>'CANCELADO'
            ORDER BY c.data_inicio DESC,a.id DESC''', (u[0],))
        return cur.fetchall()


def avaliacao_permitida(cur, avaliacao_id, usuario_id, bloquear=False):
    cur.execute('''SELECT a.*,p.ciclo_id,p.funcionario_id,p.gestor_id,
                  p.status AS participante_status,c.status AS ciclo_status,
                  c.data_inicio,c.data_fim,c.modalidade
        FROM intranet_aval_avaliacoes a
        JOIN intranet_aval_participantes p ON p.id=a.participante_id
        JOIN intranet_aval_ciclos c ON c.id=p.ciclo_id
        WHERE a.id=%s''' + (' FOR UPDATE OF a' if bloquear else ''), (avaliacao_id,))
    a = cur.fetchone()
    if not a:
        raise HTTPException(404, 'Avaliacao nao encontrada')
    if a['avaliador_id'] != usuario_id:
        raise HTTPException(403, 'Avaliacao nao destinada a este usuario')
    if a['tipo'] == 'GESTOR' and a['gestor_id'] != usuario_id:
        raise HTTPException(403, 'Gestor nao autorizado')
    if a['tipo'] == 'AUTOAVALIACAO' and (a['modalidade'] != 180 or a['funcionario_id'] != usuario_id):
        raise HTTPException(403, 'Autoavaliacao nao autorizada')
    if a['participante_status'] == 'CANCELADO':
        raise HTTPException(409, 'Participacao cancelada')
    return a


@router_avaliacoes.get('/meus/{avaliacao_id}')
def detalhe_minha_avaliacao(avaliacao_id: int, authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        a = avaliacao_permitida(cur, avaliacao_id, u[0])
        if a['ciclo_status'] not in ('PUBLICADO', 'ENCERRADO'):
            raise HTTPException(404, 'Avaliacao indisponivel')
        cur.execute('''SELECT c.id,c.titulo,c.descricao,c.peso,c.ordem,c.tipo,
                c.meta_valor,c.meta_unidade,c.meta_direcao,c.competencia_meta,c.operador_meta,
                r.nota,r.comentario,r.resultado_alcancado,r.percentual_atingimento
            FROM intranet_aval_criterios c
            LEFT JOIN intranet_aval_respostas r
              ON r.criterio_id=c.id AND r.avaliacao_id=%s
            WHERE c.ciclo_id=%s AND c.ativo=TRUE ORDER BY c.ordem,c.id''',
            (avaliacao_id,a['ciclo_id']))
        return {'avaliacao': a, 'criterios': cur.fetchall()}


def percentual_meta(meta, resultado, operador):
    """Indicadores numericos: somente 100 (atingiu) ou 0 (nao atingiu)."""
    if meta is None or resultado is None:
        return None
    if resultado < 0:
        raise HTTPException(422, 'Resultado nao pode ser negativo')
    operacoes = {
        '>=': resultado >= meta, '>': resultado > meta,
        '<=': resultado <= meta, '<': resultado < meta, '=': resultado == meta
    }
    if operador not in operacoes:
        raise HTTPException(422, 'Operador de meta invalido')
    return Decimal('100.00') if operacoes[operador] else Decimal('0.00')


@router_avaliacoes.put('/meus/{avaliacao_id}/respostas')
def salvar_respostas(avaliacao_id: int, dados: RespostasLote,
                    authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        a = avaliacao_permitida(cur, avaliacao_id, u[0], True)
        if a['ciclo_status'] != 'PUBLICADO' or a['status'] == 'CONCLUIDA':
            raise HTTPException(409, 'Avaliacao fechada para edicao')
        if not (a['data_inicio'] <= date.today() <= a['data_fim']):
            raise HTTPException(409, 'Fora do periodo de avaliacao')
        for r in dados.respostas:
            cur.execute('''SELECT id,tipo,meta_valor,meta_direcao,operador_meta FROM intranet_aval_criterios
                WHERE id=%s AND ciclo_id=%s AND ativo=TRUE''', (r.criterio_id,a['ciclo_id']))
            crit = cur.fetchone()
            if not crit:
                raise HTTPException(422, f'Criterio {r.criterio_id} nao pertence ao ciclo')
            if crit['tipo'] == 'CRITERIO':
                if r.resultado_alcancado is not None:
                    raise HTTPException(422, 'Resultado quantitativo somente para indicadores')
            elif r.nota is not None:
                raise HTTPException(422, 'Indicadores numericos nao recebem nota')
            pct = None
            if crit['tipo'] == 'META' and r.resultado_alcancado is not None:
                pct = percentual_meta(crit['meta_valor'],r.resultado_alcancado,crit['operador_meta'])
            cur.execute('''INSERT INTO intranet_aval_respostas
                (avaliacao_id,criterio_id,nota,comentario,resultado_alcancado,percentual_atingimento)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (avaliacao_id,criterio_id) DO UPDATE SET
                nota=EXCLUDED.nota,comentario=EXCLUDED.comentario,
                resultado_alcancado=EXCLUDED.resultado_alcancado,
                percentual_atingimento=EXCLUDED.percentual_atingimento,
                atualizado_em=CURRENT_TIMESTAMP''',
                (avaliacao_id,r.criterio_id,r.nota,r.comentario,r.resultado_alcancado,pct))
        cur.execute('''UPDATE intranet_aval_avaliacoes SET status='EM_ANDAMENTO',
            iniciado_em=COALESCE(iniciado_em,CURRENT_TIMESTAMP),
            atualizado_em=CURRENT_TIMESTAMP WHERE id=%s''', (avaliacao_id,))
        cur.execute('''UPDATE intranet_aval_participantes SET status='EM_ANDAMENTO'
            WHERE id=%s AND status='PENDENTE' ''', (a['participante_id'],))
        return {'avaliacao_id': avaliacao_id, 'respostas_salvas': len(dados.respostas)}


@router_avaliacoes.post('/meus/{avaliacao_id}/concluir')
def concluir_avaliacao(avaliacao_id: int, authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        a = avaliacao_permitida(cur, avaliacao_id, u[0], True)
        if a['ciclo_status'] != 'PUBLICADO' or a['status'] == 'CONCLUIDA':
            raise HTTPException(409, 'Avaliacao indisponivel para conclusao')
        if not (a['data_inicio'] <= date.today() <= a['data_fim']):
            raise HTTPException(409, 'Fora do periodo de avaliacao')
        cur.execute('''SELECT c.id,c.titulo,c.tipo,c.meta_valor,c.operador_meta,r.nota,r.comentario,r.resultado_alcancado
            FROM intranet_aval_criterios c
            LEFT JOIN intranet_aval_respostas r
              ON r.criterio_id=c.id AND r.avaliacao_id=%s
            WHERE c.ciclo_id=%s AND c.ativo=TRUE''', (avaliacao_id,a['ciclo_id']))
        criterios = cur.fetchall()
        if not criterios or any((x['tipo']=='CRITERIO' and x['nota'] is None) or
                                (x['tipo']=='META' and x['resultado_alcancado'] is None)
                                for x in criterios):
            raise HTTPException(409, 'Preencha todas as competencias e indicadores')
        sem_justificativa = [x['titulo'] for x in criterios
                             if x['tipo'] == 'CRITERIO' and x['nota'] in (1, 2)
                             and not (x['comentario'] or '').strip()]
        if sem_justificativa:
            raise HTTPException(409, 'Justificativa obrigatoria para notas 1 e 2: ' + ', '.join(sem_justificativa))
        metas_sem_justificativa = [x['titulo'] for x in criterios
            if x['tipo'] == 'META' and x['resultado_alcancado'] is not None
            and percentual_meta(x['meta_valor'], x['resultado_alcancado'], x['operador_meta']) == Decimal('0.00')
            and not (x['comentario'] or '').strip()]
        if metas_sem_justificativa:
            raise HTTPException(409, 'Justificativa obrigatoria para metas nao atingidas: ' + ', '.join(metas_sem_justificativa))
        cur.execute('''UPDATE intranet_aval_avaliacoes
            SET status='CONCLUIDA',concluido_em=CURRENT_TIMESTAMP,
                iniciado_em=COALESCE(iniciado_em,CURRENT_TIMESTAMP),
                atualizado_em=CURRENT_TIMESTAMP WHERE id=%s''', (avaliacao_id,))
        cur.execute('''SELECT count(*) FILTER (WHERE status<>'CONCLUIDA') AS pendentes
            FROM intranet_aval_avaliacoes WHERE participante_id=%s''', (a['participante_id'],))
        pendentes = cur.fetchone()['pendentes']
        if pendentes == 0:
            cur.execute('''UPDATE intranet_aval_participantes
                SET status='CONCLUIDO',concluido_em=CURRENT_TIMESTAMP
                WHERE id=%s''', (a['participante_id'],))
        if pendentes == 0:
            cur.execute('''INSERT INTO intranet_aval_relatorios (participante_id, consolidado_em)
                VALUES (%s,CURRENT_TIMESTAMP) ON CONFLICT (participante_id) DO NOTHING''',
                (a['participante_id'],))
        return {'avaliacao_id': avaliacao_id, 'status': 'CONCLUIDA',
                'participante_concluido': pendentes == 0}


@router_avaliacoes.get('/admin/ciclos/{ciclo_id}/resultados')
def resultados_admin(ciclo_id: int, authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        ciclo(cur, ciclo_id)
        cur.execute('''SELECT p.id AS participante_id,p.status AS participante_status,
            f.nome AS funcionario_nome,g.nome AS gestor_nome,
            a.id AS avaliacao_id,a.tipo,a.status AS avaliacao_status,
            COUNT(r.id) FILTER (WHERE c.tipo='CRITERIO' AND r.nota IS NOT NULL) AS notas_preenchidas,
            ROUND(SUM(r.nota*c.peso) FILTER (WHERE c.tipo='CRITERIO') /
              NULLIF(SUM(c.peso) FILTER (WHERE c.tipo='CRITERIO' AND r.nota IS NOT NULL),0),2) AS media_ponderada,
            COUNT(r.id) FILTER (WHERE c.tipo='META' AND r.resultado_alcancado IS NOT NULL) AS indicadores_preenchidos,
            COUNT(r.id) FILTER (WHERE c.tipo='META' AND r.percentual_atingimento=100) AS indicadores_atingidos
            FROM intranet_aval_participantes p
            JOIN intranet_usuarios f ON f.id=p.funcionario_id
            JOIN intranet_usuarios g ON g.id=p.gestor_id
            LEFT JOIN intranet_aval_avaliacoes a ON a.participante_id=p.id
            LEFT JOIN intranet_aval_respostas r ON r.avaliacao_id=a.id
            LEFT JOIN intranet_aval_criterios c ON c.id=r.criterio_id
            WHERE p.ciclo_id=%s
            GROUP BY p.id,p.status,f.nome,g.nome,a.id,a.tipo,a.status
            ORDER BY f.nome,a.tipo''', (ciclo_id,))
        return cur.fetchall()


@router_avaliacoes.get('/admin/ciclos/{ciclo_id}/radar')
def radar_admin(ciclo_id: int, authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        ciclo(cur, ciclo_id)
        cur.execute('''SELECT p.id AS participante_id,f.nome AS funcionario_nome,
            a.tipo AS avaliador_tipo,a.status AS avaliacao_status,
            c.id AS criterio_id,c.titulo,c.tipo,c.ordem,c.competencia_meta,
            c.meta_valor,c.meta_unidade,c.operador_meta,c.peso,
            r.nota,r.resultado_alcancado,r.percentual_atingimento
            FROM intranet_aval_participantes p
            JOIN intranet_usuarios f ON f.id=p.funcionario_id
            JOIN intranet_aval_avaliacoes a ON a.participante_id=p.id
            JOIN intranet_aval_criterios c ON c.ciclo_id=p.ciclo_id AND c.ativo=TRUE
            LEFT JOIN intranet_aval_respostas r ON r.avaliacao_id=a.id AND r.criterio_id=c.id
            WHERE p.ciclo_id=%s ORDER BY f.nome,p.id,a.tipo,c.ordem,c.id''', (ciclo_id,))
        rows=cur.fetchall()
        for x in rows:
            x['meta_radar'] = x['competencia_meta']*20 if x['tipo']=='CRITERIO' else 100
            x['resultado_radar'] = (x['nota']*20 if x['nota'] is not None else None) if x['tipo']=='CRITERIO' else (int(x['percentual_atingimento']) if x['percentual_atingimento'] is not None else None)
        return rows


# Relatorios individuais: controle de acesso no servidor, nunca apenas no HTML.
def _relatorio_permitido(cur, participante_id, usuario_id, bloquear=False, admin_nivel=None):
    cur.execute('''SELECT p.id,p.funcionario_id,p.gestor_id,p.status AS participante_status,
        c.nome AS ciclo_nome,c.modalidade,c.status AS ciclo_status,
        f.nome AS funcionario_nome,g.nome AS gestor_nome,
        f.cargo AS funcionario_cargo,f.departamento AS funcionario_departamento,f.centro_custo AS funcionario_centro_custo,
        g.cargo AS gestor_cargo,g.departamento AS gestor_departamento,g.centro_custo AS gestor_centro_custo,
        r.consolidado_em,r.liberado_em,r.liberado_por,r.ciente_em,
        (SELECT MAX(av.concluido_em) FROM intranet_aval_avaliacoes av
         WHERE av.participante_id=p.id AND av.tipo='GESTOR' AND av.status='CONCLUIDA') AS gestor_concluido_em,
        (SELECT MAX(av.concluido_em) FROM intranet_aval_avaliacoes av
         WHERE av.participante_id=p.id AND av.tipo='AUTOAVALIACAO' AND av.status='CONCLUIDA') AS auto_concluido_em
        FROM intranet_aval_participantes p
        JOIN intranet_aval_ciclos c ON c.id=p.ciclo_id
        JOIN intranet_usuarios f ON f.id=p.funcionario_id
        JOIN intranet_usuarios g ON g.id=p.gestor_id
        LEFT JOIN intranet_aval_relatorios r ON r.participante_id=p.id
        WHERE p.id=%s''' + (' FOR UPDATE OF p' if bloquear else ''), (participante_id,))
    p=cur.fetchone()
    if not p or p['participante_status']=='CANCELADO':
        raise HTTPException(404,'Relatorio nao encontrado')
    if usuario_id not in (p['funcionario_id'],p['gestor_id']) and admin_nivel != 3:
        # Admin do modulo pode consultar; nivel 2 somente com permissao explicita.
        if admin_nivel != 2:
            raise HTTPException(403,'Sem acesso ao relatorio')
        cur.execute('''SELECT 1 FROM intranet_admin_permissoes ap
            JOIN intranet_admin_modulos am ON am.id=ap.modulo_id
            WHERE ap.usuario_id=%s AND ap.ativo=TRUE AND am.ativo=TRUE
              AND am.codigo='AVALIACOES' LIMIT 1''',(usuario_id,))
        if cur.fetchone() is None:
            raise HTTPException(403,'Sem acesso ao relatorio')
    return p


@router_avaliacoes.get('/relatorios')
def listar_meus_relatorios(authorization: Optional[str] = Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''SELECT p.id AS participante_id,p.funcionario_id,p.gestor_id,
            f.nome AS funcionario_nome,c.nome AS ciclo_nome,c.modalidade,
            r.consolidado_em,r.liberado_em,r.ciente_em,
            CASE WHEN p.funcionario_id=%s THEN 'FUNCIONARIO' ELSE 'GESTOR' END AS papel
            FROM intranet_aval_participantes p
            JOIN intranet_aval_ciclos c ON c.id=p.ciclo_id
            JOIN intranet_usuarios f ON f.id=p.funcionario_id
            LEFT JOIN intranet_aval_relatorios r ON r.participante_id=p.id
            WHERE p.status<>'CANCELADO' AND c.status IN ('PUBLICADO','ENCERRADO')
              AND (p.funcionario_id=%s OR p.gestor_id=%s)
            ORDER BY p.id DESC''',(u[0],u[0],u[0]))
        return cur.fetchall()


@router_avaliacoes.get('/relatorios/{participante_id}')
def consultar_relatorio(participante_id:int,authorization: Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        p=_relatorio_permitido(cur,participante_id,u[0], admin_nivel=u[3])
        if p['consolidado_em'] is None:
            raise HTTPException(409,'Aguardando conclusao das avaliacoes')
        if u[0]==p['funcionario_id'] and u[0]!=p['gestor_id'] and p['liberado_em'] is None:
            raise HTTPException(403,'Relatorio ainda nao liberado pelo gestor')
        cur.execute('''SELECT k.id AS criterio_id,k.titulo,k.tipo,k.peso,k.competencia_meta,
            k.meta_valor,k.meta_unidade,k.operador_meta,
            a.tipo AS avaliador_tipo,r.nota,r.comentario,r.resultado_alcancado,r.percentual_atingimento
            FROM intranet_aval_participantes p
            JOIN intranet_aval_criterios k ON k.ciclo_id=p.ciclo_id AND k.ativo=TRUE
            JOIN intranet_aval_avaliacoes a ON a.participante_id=p.id AND a.status='CONCLUIDA'
            LEFT JOIN intranet_aval_respostas r ON r.avaliacao_id=a.id AND r.criterio_id=k.id
            WHERE p.id=%s ORDER BY k.ordem,k.id,a.tipo''',(participante_id,))
        return {'relatorio':p,'respostas':cur.fetchall()}


@router_avaliacoes.post('/relatorios/{participante_id}/liberar')
def liberar_relatorio(participante_id:int,authorization: Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        p=_relatorio_permitido(cur,participante_id,u[0],True,admin_nivel=u[3])
        if p['gestor_id']!=u[0]:
            raise HTTPException(403,'Somente o gestor responsavel pode liberar')
        if p['consolidado_em'] is None:
            raise HTTPException(409,'Relatorio ainda nao consolidado')
        cur.execute('''UPDATE intranet_aval_relatorios SET liberado_em=CURRENT_TIMESTAMP,
            liberado_por=%s WHERE participante_id=%s AND liberado_em IS NULL
            RETURNING liberado_em''',(u[0],participante_id))
        row=cur.fetchone()
        if not row:
            raise HTTPException(409,'Relatorio ja liberado')
        return {'participante_id':participante_id,'liberado_em':row['liberado_em']}


@router_avaliacoes.post('/relatorios/{participante_id}/ciencia')
def confirmar_ciencia(participante_id:int,authorization: Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        p=_relatorio_permitido(cur,participante_id,u[0],True,admin_nivel=u[3])
        if p['funcionario_id']!=u[0]:
            raise HTTPException(403,'Somente o funcionario pode confirmar ciencia')
        if p['liberado_em'] is None:
            raise HTTPException(409,'Relatorio nao liberado')
        cur.execute('''UPDATE intranet_aval_relatorios SET ciente_em=CURRENT_TIMESTAMP
            WHERE participante_id=%s AND ciente_em IS NULL RETURNING ciente_em''',(participante_id,))
        row=cur.fetchone()
        if not row:
            raise HTTPException(409,'Ciencia ja registrada')
        return {'participante_id':participante_id,'ciente_em':row['ciente_em']}
