"""Intranet V12 - Treinamentos, fase 1. Usa apenas tabelas intranet_trein_* existentes.
Nao cria/altera schema. Nao concede permissoes administrativas a nivel 2 sem delegacao.
"""
from typing import Optional, Literal
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

router_treinamentos = APIRouter(prefix='/api/treinamentos', tags=['Treinamentos'])
_conectar = None
_autenticar = None


def configurar_treinamentos(conectar, autenticar, registrar_auditoria=None):
    global _conectar, _autenticar
    _conectar, _autenticar = conectar, autenticar




def _nome_empresa_certificado():
    """Nome corporativo atual; não altera dados históricos dos certificados."""
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM intranet_empresa_config WHERE id=1 AND ativo=TRUE")
        cfg = cur.fetchone()
    if not cfg:
        return 'INTRANET'
    return str(cfg.get('nome_curto') or cfg.get('nome') or 'INTRANET').strip().upper()[:90]

def usuario_atual(authorization):
    if _conectar is None or _autenticar is None:
        raise HTTPException(503, 'Treinamentos nÃ£o configurados')
    u = _autenticar(authorization)
    if u[5]:
        raise HTTPException(403, 'Troca de senha obrigatÃ³ria')
    return u


def exigir_admin(u):
    """Nivel 3 ou nivel 2 com delegacao ativa para TREINAMENTOS."""
    if u[3] == 3:
        return
    if u[3] != 2:
        raise HTTPException(403, 'Administração de treinamentos não autorizada')
    with _conectar() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT 1
            FROM intranet_admin_permissoes p
            JOIN intranet_admin_modulos m ON m.id = p.modulo_id
            WHERE p.usuario_id = %s
              AND p.ativo = TRUE
              AND m.ativo = TRUE
              AND m.codigo = 'TREINAMENTOS'
            LIMIT 1
        """, (u[0],))
        if cur.fetchone() is None:
            raise HTTPException(403, 'Administração de treinamentos não autorizada')


class CursoNovo(BaseModel):
    titulo: str = Field(min_length=3, max_length=200)
    descricao: str = ''
    modalidade: Literal['EAD', 'PRESENCIAL']
    obrigatorio: bool = False
    carga_horaria_min: int = Field(gt=0)


@router_treinamentos.get('/meus')
def meus(authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''
            SELECT p.id AS participante_id, p.curso_id, c.titulo, c.descricao,
                   c.modalidade, c.carga_horaria_min, p.status,
                   p.convocado_em, p.iniciado_em, p.concluido_em, p.prazo_limite,
                   p.agendamento_id, a.data_curso, a.inicio, a.fim, a.local
            FROM intranet_trein_participantes p
            JOIN intranet_trein_cursos c ON c.id=p.curso_id
            LEFT JOIN intranet_trein_agendamentos a ON a.id=p.agendamento_id
            WHERE p.usuario_id=%s
            ORDER BY p.criado_em DESC, p.id DESC
        ''', (u[0],))
        return cur.fetchall()


@router_treinamentos.get('/catalogo')
def catalogo(authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''
            SELECT c.id, c.titulo, c.descricao, c.carga_horaria_min, c.imagem_capa
            FROM intranet_trein_cursos c
            WHERE c.modalidade='EAD' AND c.obrigatorio=FALSE
              AND c.status='PUBLICADO'
              AND EXISTS (SELECT 1 FROM intranet_trein_publicos pub
                JOIN intranet_usuarios destinatario ON destinatario.id=%s AND destinatario.ativo=TRUE
                WHERE pub.curso_id=c.id AND
                (pub.tipo='TODOS' OR
                 (pub.tipo='DEPARTAMENTO' AND pub.valor=destinatario.departamento) OR
                 (pub.tipo='CARGO' AND pub.valor=destinatario.cargo) OR
                 (pub.tipo='CENTRO_CUSTO' AND pub.valor=destinatario.centro_custo)))
              AND NOT EXISTS (
                SELECT 1 FROM intranet_trein_participantes p
                WHERE p.curso_id=c.id AND p.usuario_id=%s
              )
            ORDER BY c.titulo, c.id
        ''', (u[0],u[0]))
        return cur.fetchall()


@router_treinamentos.post('/catalogo/{curso_id}/iniciar')
def iniciar(curso_id: int, authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        # Serializa autoinscricoes do mesmo usuario/curso sem exigir novo indice.
        cur.execute('SELECT pg_advisory_xact_lock(%s, %s)', (u[0], curso_id))
        cur.execute('''SELECT id FROM intranet_trein_cursos
                       WHERE id=%s AND modalidade='EAD'
                       AND obrigatorio=FALSE AND status='PUBLICADO'
                       AND EXISTS (SELECT 1 FROM intranet_trein_publicos pub
                           JOIN intranet_usuarios destinatario ON destinatario.id=%s AND destinatario.ativo=TRUE
                           WHERE pub.curso_id=intranet_trein_cursos.id AND
                           (pub.tipo='TODOS' OR
                            (pub.tipo='DEPARTAMENTO' AND pub.valor=destinatario.departamento) OR
                            (pub.tipo='CARGO' AND pub.valor=destinatario.cargo) OR
                            (pub.tipo='CENTRO_CUSTO' AND pub.valor=destinatario.centro_custo))) ''', (curso_id,u[0]))
        if not cur.fetchone():
            raise HTTPException(404, 'Curso nÃ£o disponÃ­vel para autoinscriÃ§Ã£o')
        cur.execute('''SELECT id, status FROM intranet_trein_participantes
                       WHERE curso_id=%s AND usuario_id=%s
                       ORDER BY id DESC LIMIT 1''', (curso_id, u[0]))
        existente = cur.fetchone()
        if existente:
            return {'participante_id': existente['id'], 'status': existente['status'], 'ja_inscrito': True}
        cur.execute('''
            INSERT INTO intranet_trein_participantes
              (curso_id, usuario_id, origem, status, iniciado_em)
            VALUES (%s, %s, 'AUTOINSCRICAO', 'EM_ANDAMENTO', NOW())
            RETURNING id
        ''', (curso_id, u[0]))
        participante_id = cur.fetchone()['id']
        cur.execute('''INSERT INTO intranet_trein_auditoria
            (usuario_id, curso_id, entidade, entidade_id, acao, detalhes)
            VALUES (%s,%s,'PARTICIPANTE',%s,'AUTOINSCRICAO',%s::jsonb)''',
            (u[0], curso_id, participante_id, '{}'))
        return {'participante_id': participante_id, 'status': 'EM_ANDAMENTO', 'ja_inscrito': False}


def _bloquear_concluido(cur,curso_id):
    cur.execute('''SELECT 1 FROM intranet_trein_participantes
      WHERE curso_id=%s AND (status='CONCLUIDO' OR concluido_em IS NOT NULL) LIMIT 1''',(curso_id,))
    if cur.fetchone():
        raise HTTPException(409,'Curso com conclusão: somente consulta e desativação permitidas')

@router_treinamentos.get('/admin/cursos/{curso_id}/consulta')
def consulta_curso_admin(curso_id:int,authorization:Optional[str]=Header(None)):
    exigir_admin(usuario_atual(authorization))
    with _conectar() as conn,conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''SELECT id,titulo,descricao,modalidade,obrigatorio,status,carga_horaria_min
                       FROM intranet_trein_cursos WHERE id=%s''',(curso_id,))
        curso=cur.fetchone()
        if not curso: raise HTTPException(404,'Curso não encontrado')
        cur.execute('SELECT tipo,valor FROM intranet_trein_publicos WHERE curso_id=%s ORDER BY id',(curso_id,))
        publico=cur.fetchall()
        cur.execute('''SELECT a.titulo,a.descricao,m.tipo,m.duracao_segundos,m.referencia
          FROM intranet_trein_aulas a JOIN intranet_trein_modulos mo ON mo.id=a.modulo_id
          LEFT JOIN intranet_trein_materiais m ON m.aula_id=a.id
          WHERE mo.curso_id=%s AND NOT EXISTS
          (SELECT 1 FROM intranet_trein_auditoria au WHERE au.curso_id=%s
          AND au.entidade='AULA' AND au.entidade_id=a.id AND au.acao='EXCLUSAO_LOGICA')
          ORDER BY mo.ordem,a.ordem,m.ordem''',(curso_id,curso_id))
        aulas=cur.fetchall()
        cur.execute('''SELECT av.titulo,q.enunciado,al.texto,al.correta
          FROM intranet_trein_avaliacoes av
          JOIN intranet_trein_avaliacao_questoes aq ON aq.avaliacao_id=av.id
          JOIN intranet_trein_questoes q ON q.id=aq.questao_id
          JOIN intranet_trein_alternativas al ON al.questao_id=q.id
          WHERE av.curso_id=%s AND q.ativa=TRUE ORDER BY q.id,al.ordem''',(curso_id,))
        questoes=cur.fetchall()
        cur.execute('''SELECT p.status,COUNT(*) AS quantidade FROM intranet_trein_participantes p
          WHERE p.curso_id=%s GROUP BY p.status''',(curso_id,))
        situacoes=cur.fetchall()
        turmas=[]
        if curso['modalidade']=='PRESENCIAL':
            cur.execute('''SELECT a.id,a.data_curso,a.inicio,a.fim,a.local,a.instrutor_nome,
              a.instrutor_empresa,a.status,a.encerrado_em,admin.nome AS encerrado_por_nome
              FROM intranet_trein_agendamentos a
              LEFT JOIN intranet_usuarios admin ON admin.id=a.encerrado_por
              WHERE a.curso_id=%s ORDER BY a.data_curso,a.inicio,a.id''',(curso_id,))
            turmas=cur.fetchall()
            for turma in turmas:
                cur.execute('''SELECT p.id AS participante_id,u.nome,u.cargo,u.centro_custo,
                  p.status,p.concluido_em,pr.presente,
                  cert.codigo_validacao,cert.liberado_em AS certificado_liberado_em,
                  cert.revogado_em AS certificado_revogado_em
                  FROM intranet_trein_participantes p
                  JOIN intranet_usuarios u ON u.id=p.usuario_id
                  LEFT JOIN intranet_trein_presencas pr ON pr.participante_id=p.id
                  LEFT JOIN intranet_trein_certificados cert ON cert.participante_id=p.id
                  WHERE p.curso_id=%s AND p.agendamento_id=%s ORDER BY u.nome''',(curso_id,turma['id']))
                turma['participantes']=cur.fetchall()
        return {'curso':curso,'publico':publico,'aulas':aulas,
                 'questoes':questoes,'situacoes':situacoes,'turmas':turmas,
                 'configuracao':_config_ead(cur,curso_id) if curso['modalidade']=='EAD' else {}}

@router_treinamentos.get('/admin/cursos')
def cursos_admin(authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''SELECT c.id,c.titulo,c.descricao,c.modalidade,c.obrigatorio,
          c.status,c.carga_horaria_min,c.criado_em,c.publicado_em,
          (SELECT COUNT(DISTINCT p.usuario_id) FROM intranet_trein_participantes p
             WHERE p.curso_id=c.id) AS inscritos,
          (SELECT COUNT(*) FROM intranet_trein_participantes p
             WHERE p.curso_id=c.id AND p.status='CONCLUIDO') AS concluidos,
          (SELECT COUNT(*) FROM intranet_trein_participantes p
             WHERE p.curso_id=c.id AND p.status='EM_ANDAMENTO') AS em_andamento,
          (SELECT COUNT(*) FROM intranet_trein_participantes p
             WHERE p.curso_id=c.id AND p.status='PENDENTE') AS pendentes,
          (SELECT COUNT(DISTINCT u.id) FROM intranet_usuarios u
             WHERE u.ativo=TRUE AND EXISTS
             (SELECT 1 FROM intranet_trein_publicos pub WHERE pub.curso_id=c.id
              AND (pub.tipo='TODOS' OR
               (pub.tipo='DEPARTAMENTO' AND btrim(pub.valor)=btrim(u.departamento)) OR
               (pub.tipo='CARGO' AND btrim(pub.valor)=btrim(u.cargo)) OR
               (pub.tipo='CENTRO_CUSTO' AND btrim(pub.valor)=btrim(u.centro_custo))))) AS publico_total
          FROM intranet_trein_cursos c ORDER BY c.id DESC''')
        return cur.fetchall()


@router_treinamentos.post('/admin/cursos', status_code=201)
def criar_curso(dados: CursoNovo, authorization: Optional[str] = Header(None)):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''
            INSERT INTO intranet_trein_cursos
              (titulo, descricao, modalidade, obrigatorio, status,
               carga_horaria_min, certificado_liberacao, criado_por)
            VALUES (%s,%s,%s,%s,'RASCUNHO',%s,%s,%s)
            RETURNING id
        ''', (dados.titulo, dados.descricao, dados.modalidade,
              dados.obrigatorio, dados.carga_horaria_min,
              'AUTOMATICA' if dados.modalidade == 'EAD' else 'MANUAL', u[0]))
        curso_id = cur.fetchone()['id']
        if dados.modalidade == 'EAD':
            cur.execute('''
                INSERT INTO intranet_trein_modulos
                    (curso_id, titulo, descricao, ordem)
                VALUES (%s, %s, %s, 1)
            ''', (curso_id, 'ConteÃºdo do Curso', 'MÃ³dulo inicial automÃ¡tico'))
        cur.execute('''INSERT INTO intranet_trein_auditoria
            (usuario_id, curso_id, entidade, entidade_id, acao, detalhes)
            VALUES (%s,%s,'CURSO',%s,'CRIACAO',%s::jsonb)''',
            (u[0], curso_id, curso_id, '{}'))
        return {'id': curso_id, 'status': 'RASCUNHO'}

class CursoEdicao(BaseModel):
    titulo: str = Field(min_length=3, max_length=200)
    descricao: str = ''
    modalidade: Literal['EAD', 'PRESENCIAL']
    obrigatorio: bool = False
    carga_horaria_min: int = Field(gt=0)


@router_treinamentos.put('/admin/cursos/{curso_id}')
def editar_curso(
    curso_id: int,
    dados: CursoEdicao,
    authorization: Optional[str] = Header(None)
):
    u = usuario_atual(authorization)
    exigir_admin(u)

    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            'SELECT id, status, modalidade FROM intranet_trein_cursos WHERE id=%s FOR UPDATE',
            (curso_id,)
        )
        curso = cur.fetchone()

        if not curso:
            raise HTTPException(404, 'Curso nÃ£o encontrado')

        cur.execute('''SELECT 1 FROM intranet_trein_participantes
                       WHERE curso_id=%s AND (status='CONCLUIDO' OR concluido_em IS NOT NULL)
                       LIMIT 1''',(curso_id,))
        if cur.fetchone():
            raise HTTPException(409,'Curso com conclusão registrada: somente desativação permitida')
        if dados.modalidade != curso['modalidade']:
            raise HTTPException(409, 'A modalidade do curso não pode ser alterada após a criação')
        if curso['status'] != 'RASCUNHO':
            raise HTTPException(409, 'Somente rascunhos podem ser editados')

        cur.execute('''
            UPDATE intranet_trein_cursos
            SET titulo=%s, descricao=%s, modalidade=%s,
                obrigatorio=%s, carga_horaria_min=%s,
                certificado_liberacao=%s
            WHERE id=%s
        ''', (
            dados.titulo, dados.descricao, dados.modalidade,
            dados.obrigatorio, dados.carga_horaria_min,
            'AUTOMATICA' if dados.modalidade == 'EAD' else 'MANUAL',
            curso_id
        ))

        cur.execute('''
            INSERT INTO intranet_trein_auditoria
              (usuario_id, curso_id, entidade, entidade_id, acao, detalhes)
            VALUES (%s,%s,'CURSO',%s,'EDICAO',%s::jsonb)
        ''', (u[0], curso_id, curso_id, '{}'))

        return {'id': curso_id, 'status': 'RASCUNHO', 'atualizado': True}


@router_treinamentos.delete('/admin/cursos/{curso_id}')
def excluir_curso(
    curso_id: int,
    authorization: Optional[str] = Header(None)
):
    u = usuario_atual(authorization)
    exigir_admin(u)

    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            'SELECT id, status FROM intranet_trein_cursos WHERE id=%s FOR UPDATE',
            (curso_id,)
        )
        curso = cur.fetchone()

        if not curso:
            raise HTTPException(404, 'Curso nÃ£o encontrado')

        if curso['status'] == 'DESATIVADO':
            raise HTTPException(409, 'Curso já desativado')

        cur.execute('''
            UPDATE intranet_trein_cursos
            SET status='DESATIVADO'
            WHERE id=%s
        ''', (curso_id,))

        cur.execute('''
            INSERT INTO intranet_trein_auditoria
              (usuario_id, curso_id, entidade, entidade_id, acao, detalhes)
            VALUES (%s,%s,'CURSO',%s,'EXCLUSAO_LOGICA',%s::jsonb)
        ''', (u[0], curso_id, curso_id, '{}'))

        return {'id': curso_id, 'excluido': True}


class AulaVideoNova(BaseModel):
    titulo: str = Field(min_length=3, max_length=200)
    descricao: str = ''
    video_url: str = Field(min_length=8)
    duracao_minutos: int = Field(gt=0)


@router_treinamentos.post('/admin/cursos/{curso_id}/aulas', status_code=201)
def criar_aula_video(
    curso_id: int,
    dados: AulaVideoNova,
    authorization: Optional[str] = Header(None)
):
    u = usuario_atual(authorization)
    exigir_admin(u)

    from urllib.parse import urlparse
    endereco = urlparse(dados.video_url.strip())

    if endereco.scheme != 'https' or not endereco.hostname:
        raise HTTPException(422, 'Informe um link HTTPS vÃ¡lido')

    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        _bloquear_concluido(cur,curso_id)
        cur.execute('''
            SELECT id, status, modalidade
            FROM intranet_trein_cursos
            WHERE id=%s FOR UPDATE
        ''', (curso_id,))
        curso = cur.fetchone()

        if not curso:
            raise HTTPException(404, 'Curso nÃ£o encontrado')

        if curso['status'] != 'RASCUNHO' or curso['modalidade'] != 'EAD':
            raise HTTPException(409, 'Somente cursos EAD em rascunho podem receber aulas')

        cur.execute('''
            SELECT id FROM intranet_trein_modulos
            WHERE curso_id=%s
            ORDER BY ordem LIMIT 1
        ''', (curso_id,))
        modulo = cur.fetchone()

        if not modulo:
            # Cursos antigos ou rascunhos sem módulo: criar automaticamente.
            cur.execute('''INSERT INTO intranet_trein_modulos
                (curso_id,titulo,descricao,ordem)
                VALUES (%s,%s,%s,1) RETURNING id''',
                (curso_id,'Conteúdo do curso','Módulo inicial automático'))
            modulo = cur.fetchone()

        modulo_id = modulo['id']

        cur.execute('''
            SELECT COALESCE(MAX(ordem),0)+1 AS proxima
            FROM intranet_trein_aulas WHERE modulo_id=%s
        ''', (modulo_id,))
        ordem = cur.fetchone()['proxima']

        cur.execute('''
            INSERT INTO intranet_trein_aulas
                (modulo_id,titulo,descricao,ordem,obrigatoria)
            VALUES (%s,%s,%s,%s,TRUE)
            RETURNING id
        ''', (modulo_id,dados.titulo,dados.descricao,ordem))

        aula_id = cur.fetchone()['id']

        cur.execute('''
            INSERT INTO intranet_trein_materiais
                (aula_id,titulo,tipo,referencia,ordem,
                 duracao_segundos,obrigatorio)
            VALUES (%s,%s,'VIDEO',%s,1,%s,TRUE)
            RETURNING id
        ''', (
            aula_id,dados.titulo,dados.video_url.strip(),
            dados.duracao_minutos*60
        ))

        material_id = cur.fetchone()['id']

        cur.execute('''
            INSERT INTO intranet_trein_auditoria
                (usuario_id,curso_id,entidade,entidade_id,acao,detalhes)
            VALUES (%s,%s,'AULA',%s,'CRIACAO',%s::jsonb)
        ''', (u[0],curso_id,aula_id,'{}'))

        return {
            'aula_id': aula_id,
            'material_id': material_id,
            'modulo_id': modulo_id,
            'criado': True
        }

@router_treinamentos.get('/admin/cursos/{curso_id}/aulas')
def listar_aulas_curso(
    curso_id: int,
    authorization: Optional[str] = Header(None)
):
    u = usuario_atual(authorization)
    exigir_admin(u)

    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''
            SELECT a.id, a.titulo, a.descricao, a.ordem,
                   m.id AS material_id, m.tipo,
                   m.referencia AS video_url,
                   m.duracao_segundos
            FROM intranet_trein_aulas a
            JOIN intranet_trein_modulos mo ON mo.id=a.modulo_id
            LEFT JOIN intranet_trein_materiais m ON m.aula_id=a.id
            WHERE mo.curso_id=%s
              AND NOT EXISTS (
                SELECT 1 FROM intranet_trein_auditoria aud
                WHERE aud.curso_id=mo.curso_id
                  AND aud.entidade='AULA' AND aud.entidade_id=a.id
                  AND aud.acao='EXCLUSAO_LOGICA'
              )
            ORDER BY mo.ordem, a.ordem, m.ordem
        ''', (curso_id,))
        return cur.fetchall()


class AulaVideoEdicao(BaseModel):
    titulo: str = Field(min_length=3, max_length=200)
    descricao: str = ''
    video_url: str = Field(min_length=8)
    # Se omitida, a duraÃ§Ã£o jÃ¡ cadastrada Ã© preservada.
    duracao_segundos: Optional[int] = Field(default=None, ge=0)


def _aula_editavel(cur, curso_id, aula_id):
    _bloquear_concluido(cur,curso_id)
    cur.execute('''SELECT a.id, a.titulo, a.descricao, a.modulo_id,
                          c.status, c.modalidade
                   FROM intranet_trein_aulas a
                   JOIN intranet_trein_modulos mo ON mo.id=a.modulo_id
                   JOIN intranet_trein_cursos c ON c.id=mo.curso_id
                   WHERE c.id=%s AND a.id=%s
                   FOR UPDATE OF a''', (curso_id, aula_id))
    aula = cur.fetchone()
    if not aula:
        raise HTTPException(404, 'Aula nÃ£o encontrada neste curso')
    if aula['status'] != 'RASCUNHO' or aula['modalidade'] != 'EAD':
        raise HTTPException(409, 'Somente aulas de cursos EAD em rascunho podem ser alteradas')
    cur.execute('''SELECT 1 FROM intranet_trein_auditoria
                   WHERE curso_id=%s AND entidade='AULA' AND entidade_id=%s
                     AND acao='EXCLUSAO_LOGICA' LIMIT 1''', (curso_id, aula_id))
    if cur.fetchone():
        raise HTTPException(409, 'Esta aula jÃ¡ foi excluÃ­da')
    return aula


@router_treinamentos.put('/admin/cursos/{curso_id}/aulas/{aula_id}')
def editar_aula_video(
    curso_id: int, aula_id: int, dados: AulaVideoEdicao,
    authorization: Optional[str] = Header(None)
):
    u = usuario_atual(authorization)
    exigir_admin(u)
    from urllib.parse import urlparse
    endereco = urlparse(dados.video_url.strip())
    if endereco.scheme != 'https' or not endereco.hostname:
        raise HTTPException(422, 'Informe um link HTTPS vÃ¡lido')
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        _aula_editavel(cur, curso_id, aula_id)
        cur.execute('''SELECT id FROM intranet_trein_materiais
                       WHERE aula_id=%s AND tipo='VIDEO'
                       ORDER BY ordem LIMIT 1 FOR UPDATE''', (aula_id,))
        material = cur.fetchone()
        if not material:
            raise HTTPException(409, 'Aula sem material de vÃ­deo')
        cur.execute('''UPDATE intranet_trein_aulas
                       SET titulo=%s, descricao=%s WHERE id=%s''',
                    (dados.titulo, dados.descricao, aula_id))
        if dados.duracao_segundos is None:
            cur.execute('''UPDATE intranet_trein_materiais
                           SET titulo=%s, referencia=%s WHERE id=%s''',
                        (dados.titulo, dados.video_url.strip(), material['id']))
        else:
            cur.execute('''UPDATE intranet_trein_materiais
                           SET titulo=%s, referencia=%s, duracao_segundos=%s
                           WHERE id=%s''',
                        (dados.titulo, dados.video_url.strip(),
                         dados.duracao_segundos, material['id']))
        cur.execute('''INSERT INTO intranet_trein_auditoria
                       (usuario_id,curso_id,entidade,entidade_id,acao,detalhes)
                       VALUES (%s,%s,'AULA',%s,'EDICAO',%s::jsonb)''',
                    (u[0], curso_id, aula_id, '{}'))
        return {'aula_id': aula_id, 'atualizado': True}


@router_treinamentos.delete('/admin/cursos/{curso_id}/aulas/{aula_id}')
def excluir_aula_video(
    curso_id: int, aula_id: int,
    authorization: Optional[str] = Header(None)
):
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        _aula_editavel(cur, curso_id, aula_id)
        # ExclusÃ£o lÃ³gica: mantÃ©m aula, materiais, progresso e vÃ­nculos histÃ³ricos.
        cur.execute('''INSERT INTO intranet_trein_auditoria
                       (usuario_id,curso_id,entidade,entidade_id,acao,detalhes)
                       VALUES (%s,%s,'AULA',%s,'EXCLUSAO_LOGICA',%s::jsonb)''',
                    (u[0], curso_id, aula_id, '{}'))
        return {'aula_id': aula_id, 'excluido': True}


class DuracaoVideo(BaseModel):
    duracao_segundos: int = Field(gt=0, le=86400)


@router_treinamentos.put('/admin/cursos/{curso_id}/aulas/{aula_id}/duracao')
def atualizar_duracao_aula(
    curso_id: int, aula_id: int, dados: DuracaoVideo,
    authorization: Optional[str] = Header(None)
):
    # Sincronizacao de duracao e independente da edicao de aulas.
    import json
    u = usuario_atual(authorization)
    exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('SELECT status, modalidade FROM intranet_trein_cursos WHERE id=%s FOR UPDATE', (curso_id,))
        curso = cur.fetchone()
        if not curso:
            raise HTTPException(404, 'Curso nao encontrado')
        if curso['modalidade'] != 'EAD' or curso['status'] not in ('RASCUNHO', 'PUBLICADO'):
            raise HTTPException(409, 'Somente cursos EAD ativos podem sincronizar a duracao')
        _bloquear_concluido(cur, curso_id)
        cur.execute("""SELECT m.id, m.duracao_segundos
                       FROM intranet_trein_materiais m
                       JOIN intranet_trein_aulas a ON a.id=m.aula_id
                       JOIN intranet_trein_modulos mo ON mo.id=a.modulo_id
                       WHERE mo.curso_id=%s AND a.id=%s AND m.tipo='VIDEO'
                         AND NOT EXISTS (
                           SELECT 1 FROM intranet_trein_auditoria aud
                           WHERE aud.curso_id=%s AND aud.entidade='AULA'
                             AND aud.entidade_id=a.id AND aud.acao='EXCLUSAO_LOGICA')
                       ORDER BY m.ordem, m.id LIMIT 1 FOR UPDATE OF m""",
                    (curso_id, aula_id, curso_id))
        material = cur.fetchone()
        if not material:
            raise HTTPException(404, 'Video ativo nao encontrado nesta aula')
        anterior = material['duracao_segundos']
        if anterior != dados.duracao_segundos:
            cur.execute('UPDATE intranet_trein_materiais SET duracao_segundos=%s WHERE id=%s',
                        (dados.duracao_segundos, material['id']))
            detalhes = json.dumps({'material_id': material['id'],
                                   'duracao_anterior_segundos': anterior,
                                   'duracao_nova_segundos': dados.duracao_segundos})
            cur.execute("""INSERT INTO intranet_trein_auditoria
                           (usuario_id,curso_id,entidade,entidade_id,acao,detalhes)
                           VALUES (%s,%s,'AULA',%s,'DURACAO_ATUALIZADA',%s::jsonb)""",
                        (u[0], curso_id, aula_id, detalhes))
        return {'aula_id': aula_id, 'material_id': material['id'],
                'duracao_segundos': dados.duracao_segundos,
                'atualizado': anterior != dados.duracao_segundos}


# EAD V12 - Cadastro real de prova objetiva (schema existente).
class QuestaoEAD(BaseModel):
    enunciado: str = Field(min_length=5, max_length=3000)
    alternativas: list[str] = Field(min_length=4, max_length=4)
    correta_indice: int = Field(ge=0, le=3)


def _avaliacao_editavel(cur, curso_id):
    _bloquear_concluido(cur,curso_id)
    cur.execute("SELECT id,status,modalidade FROM intranet_trein_cursos WHERE id=%s FOR UPDATE", (curso_id,))
    curso = cur.fetchone()
    if not curso:
        raise HTTPException(404, 'Curso nÃ£o encontrado')
    if curso['status'] != 'RASCUNHO' or curso['modalidade'] != 'EAD':
        raise HTTPException(409, 'Somente cursos EAD em rascunho podem ter a prova alterada')
    cur.execute("SELECT id FROM intranet_trein_avaliacoes WHERE curso_id=%s ORDER BY id LIMIT 1", (curso_id,))
    return cur.fetchone()


def _sem_tentativas(cur, avaliacao_id):
    cur.execute('SELECT 1 FROM intranet_trein_tentativas WHERE avaliacao_id=%s LIMIT 1', (avaliacao_id,))
    if cur.fetchone():
        raise HTTPException(409, 'JÃ¡ existem tentativas registradas. A prova nÃ£o pode ser modificada.')


@router_treinamentos.get('/admin/cursos/{curso_id}/avaliacao')
def listar_avaliacao_ead(curso_id: int, authorization: Optional[str] = Header(None)):
    exigir_admin(usuario_atual(authorization))
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('SELECT id,titulo,quantidade_questoes,sortear_questoes,embaralhar_alternativas FROM intranet_trein_avaliacoes WHERE curso_id=%s ORDER BY id LIMIT 1', (curso_id,))
        avaliacao = cur.fetchone()
        if not avaliacao:
            return {'avaliacao': None, 'questoes': []}
        cur.execute('''SELECT q.id,q.enunciado,q.tipo FROM intranet_trein_questoes q
                       JOIN intranet_trein_avaliacao_questoes aq ON aq.questao_id=q.id
                       WHERE aq.avaliacao_id=%s AND q.ativa=TRUE ORDER BY q.id''', (avaliacao['id'],))
        questoes = cur.fetchall()
        for questao in questoes:
            cur.execute('SELECT id,texto,correta,ordem FROM intranet_trein_alternativas WHERE questao_id=%s ORDER BY ordem,id', (questao['id'],))
            questao['alternativas'] = cur.fetchall()
        return {'avaliacao': avaliacao, 'questoes': questoes}


@router_treinamentos.post('/admin/cursos/{curso_id}/avaliacao/questoes', status_code=201)
def criar_questao_ead(curso_id: int, dados: QuestaoEAD, authorization: Optional[str] = Header(None)):
    u=usuario_atual(authorization)
    exigir_admin(u)
    alternativas=[a.strip() for a in dados.alternativas]
    if any(len(a)<1 for a in alternativas):
        raise HTTPException(422,'Preencha as quatro alternativas')
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        avaliacao=_avaliacao_editavel(cur,curso_id)
        if avaliacao:
            _sem_tentativas(cur, avaliacao['id'])
            avaliacao_id=avaliacao['id']
        else:
            cur.execute('''INSERT INTO intranet_trein_avaliacoes
                (curso_id,titulo,quantidade_questoes,sortear_questoes,embaralhar_alternativas)
                VALUES (%s,'AvaliaÃ§Ã£o final',1,FALSE,FALSE) RETURNING id''',(curso_id,))
            avaliacao_id=cur.fetchone()['id']
        cur.execute('''INSERT INTO intranet_trein_questoes (enunciado,tipo,ativa,criado_por)
                       VALUES (%s,'ESCOLHA_UNICA',TRUE,%s) RETURNING id''',(dados.enunciado.strip(),u[0]))
        questao_id=cur.fetchone()['id']
        for i,texto in enumerate(alternativas):
            cur.execute('''INSERT INTO intranet_trein_alternativas (questao_id,texto,correta,ordem)
                           VALUES (%s,%s,%s,%s)''',(questao_id,texto,i==dados.correta_indice,i+1))
        cur.execute('INSERT INTO intranet_trein_avaliacao_questoes (avaliacao_id,questao_id) VALUES (%s,%s)',(avaliacao_id,questao_id))
        cur.execute('UPDATE intranet_trein_avaliacoes SET quantidade_questoes=(SELECT COUNT(*) FROM intranet_trein_avaliacao_questoes WHERE avaliacao_id=%s) WHERE id=%s',(avaliacao_id,avaliacao_id))
        cur.execute('''INSERT INTO intranet_trein_auditoria (usuario_id,curso_id,entidade,entidade_id,acao,detalhes)
                       VALUES (%s,%s,'QUESTAO',%s,'CRIACAO',%s::jsonb)''',(u[0],curso_id,questao_id,'{}'))
        return {'id':questao_id,'avaliacao_id':avaliacao_id,'criado':True}


@router_treinamentos.put('/admin/cursos/{curso_id}/avaliacao/questoes/{questao_id}')
def editar_questao_ead(curso_id: int, questao_id: int, dados: QuestaoEAD, authorization: Optional[str] = Header(None)):
    u=usuario_atual(authorization); exigir_admin(u)
    alternativas=[a.strip() for a in dados.alternativas]
    if any(not a for a in alternativas):
        raise HTTPException(422,'Preencha as quatro alternativas')
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        avaliacao=_avaliacao_editavel(cur,curso_id)
        if not avaliacao: raise HTTPException(404,'AvaliaÃ§Ã£o nÃ£o encontrada')
        _sem_tentativas(cur,avaliacao['id'])
        cur.execute('''SELECT q.id FROM intranet_trein_questoes q JOIN intranet_trein_avaliacao_questoes aq ON aq.questao_id=q.id
                       WHERE aq.avaliacao_id=%s AND q.id=%s AND q.ativa=TRUE FOR UPDATE OF q''',(avaliacao['id'],questao_id))
        if not cur.fetchone(): raise HTTPException(404,'QuestÃ£o nÃ£o encontrada')
        cur.execute('UPDATE intranet_trein_questoes SET enunciado=%s,atualizado_em=NOW() WHERE id=%s',(dados.enunciado.strip(),questao_id))
        cur.execute('DELETE FROM intranet_trein_alternativas WHERE questao_id=%s',(questao_id,))
        for i,texto in enumerate(alternativas):
            cur.execute('INSERT INTO intranet_trein_alternativas (questao_id,texto,correta,ordem) VALUES (%s,%s,%s,%s)',(questao_id,texto,i==dados.correta_indice,i+1))
        cur.execute('''INSERT INTO intranet_trein_auditoria (usuario_id,curso_id,entidade,entidade_id,acao,detalhes)
                       VALUES (%s,%s,'QUESTAO',%s,'EDICAO',%s::jsonb)''',(u[0],curso_id,questao_id,'{}'))
        return {'id':questao_id,'atualizado':True}


@router_treinamentos.delete('/admin/cursos/{curso_id}/avaliacao/questoes/{questao_id}')
def excluir_questao_ead(curso_id: int, questao_id: int, authorization: Optional[str] = Header(None)):
    u=usuario_atual(authorization); exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        avaliacao=_avaliacao_editavel(cur,curso_id)
        if not avaliacao: raise HTTPException(404,'AvaliaÃ§Ã£o nÃ£o encontrada')
        _sem_tentativas(cur,avaliacao['id'])
        cur.execute('DELETE FROM intranet_trein_avaliacao_questoes WHERE avaliacao_id=%s AND questao_id=%s RETURNING questao_id',(avaliacao['id'],questao_id))
        if not cur.fetchone(): raise HTTPException(404,'QuestÃ£o nÃ£o encontrada')
        cur.execute('UPDATE intranet_trein_questoes SET ativa=FALSE,atualizado_em=NOW() WHERE id=%s',(questao_id,))
        cur.execute('UPDATE intranet_trein_avaliacoes SET quantidade_questoes=(SELECT COUNT(*) FROM intranet_trein_avaliacao_questoes WHERE avaliacao_id=%s) WHERE id=%s',(avaliacao['id'],avaliacao['id']))
        cur.execute('''INSERT INTO intranet_trein_auditoria (usuario_id,curso_id,entidade,entidade_id,acao,detalhes)
                       VALUES (%s,%s,'QUESTAO',%s,'EXCLUSAO_LOGICA',%s::jsonb)''',(u[0],curso_id,questao_id,'{}'))
        return {'id':questao_id,'excluido':True}



# Configuracao EAD sem migracao de schema: ultimo evento de configuracao prevalece.
class ConfiguracaoConclusaoEAD(BaseModel):
    exigir_prova: bool = False
    nota_minima: int = Field(default=70, ge=0, le=100)
    tentativas_maximas: int = Field(default=3, ge=1, le=20)

def _config_ead(cur, curso_id):
    cur.execute('''SELECT detalhes FROM intranet_trein_auditoria
        WHERE curso_id=%s AND entidade='CURSO' AND acao='CONFIG_CONCLUSAO_EAD'
        ORDER BY id DESC LIMIT 1''', (curso_id,))
    row=cur.fetchone()
    d=row['detalhes'] if row else {}
    return {'exigir_prova':bool(d.get('exigir_prova',False)),
            'nota_minima':int(d.get('nota_minima',70)),
            'tentativas_maximas':int(d.get('tentativas_maximas',3))}

@router_treinamentos.get('/admin/cursos/{curso_id}/configuracao-ead')
def obter_config_ead(curso_id:int, authorization:Optional[str]=Header(None)):
    exigir_admin(usuario_atual(authorization))
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('SELECT id FROM intranet_trein_cursos WHERE id=%s AND modalidade=%s', (curso_id,'EAD'))
        if not cur.fetchone(): raise HTTPException(404,'Curso EAD não encontrado')
        return _config_ead(cur,curso_id)

@router_treinamentos.put('/admin/cursos/{curso_id}/configuracao-ead')
def salvar_config_ead(curso_id:int, dados:ConfiguracaoConclusaoEAD, authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization); exigir_admin(u)
    import json
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        _bloquear_concluido(cur,curso_id)
        cur.execute('SELECT status,modalidade FROM intranet_trein_cursos WHERE id=%s FOR UPDATE',(curso_id,))
        curso=cur.fetchone()
        if not curso: raise HTTPException(404,'Curso não encontrado')
        if curso['status']!='RASCUNHO' or curso['modalidade']!='EAD':
            raise HTTPException(409,'Somente cursos EAD em rascunho podem ser configurados')
        cur.execute('''INSERT INTO intranet_trein_auditoria
            (usuario_id,curso_id,entidade,entidade_id,acao,detalhes)
            VALUES (%s,%s,'CURSO',%s,'CONFIG_CONCLUSAO_EAD',%s::jsonb)''',
            (u[0],curso_id,curso_id,json.dumps(dados.model_dump())))
        return dados.model_dump()

@router_treinamentos.post('/admin/cursos/{curso_id}/publicar')
def publicar_ead(curso_id:int, authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization); exigir_admin(u)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('SELECT status,modalidade FROM intranet_trein_cursos WHERE id=%s FOR UPDATE',(curso_id,))
        curso=cur.fetchone()
        if not curso: raise HTTPException(404,'Curso não encontrado')
        if curso['status']!='RASCUNHO' or curso['modalidade']!='EAD':
            raise HTTPException(409,'Somente cursos EAD em rascunho podem ser publicados')
        cur.execute('''SELECT COUNT(*) AS total FROM intranet_trein_materiais m
          JOIN intranet_trein_aulas a ON a.id=m.aula_id
          JOIN intranet_trein_modulos mo ON mo.id=a.modulo_id
          WHERE mo.curso_id=%s AND m.tipo='VIDEO' AND NOT EXISTS
          (SELECT 1 FROM intranet_trein_auditoria aud WHERE aud.curso_id=%s
           AND aud.entidade='AULA' AND aud.entidade_id=a.id AND aud.acao='EXCLUSAO_LOGICA')''',(curso_id,curso_id))
        if cur.fetchone()['total']<1: raise HTTPException(409,'Cadastre pelo menos uma aula de vídeo')
        config=_config_ead(cur,curso_id)
        if config['exigir_prova']:
            cur.execute('''SELECT COUNT(*) AS total FROM intranet_trein_avaliacao_questoes aq
              JOIN intranet_trein_avaliacoes av ON av.id=aq.avaliacao_id
              JOIN intranet_trein_questoes q ON q.id=aq.questao_id
              WHERE av.curso_id=%s AND q.ativa=TRUE''',(curso_id,))
            if cur.fetchone()['total']<1: raise HTTPException(409,'Cadastre questões antes de exigir prova')
        cur.execute('SELECT tipo,valor FROM intranet_trein_publicos WHERE curso_id=%s ORDER BY id',(curso_id,))
        publico=cur.fetchall()
        if not publico: raise HTTPException(409,'Defina e salve o público antes de publicar')
        tipo=publico[0]['tipo']
        if any(p['tipo']!=tipo for p in publico): raise HTTPException(409,'Público inconsistente')
        if tipo=='TODOS':
            cur.execute('SELECT id FROM intranet_usuarios WHERE ativo=TRUE')
        else:
            coluna={'DEPARTAMENTO':'departamento','CARGO':'cargo','CENTRO_CUSTO':'centro_custo'}.get(tipo)
            if not coluna: raise HTTPException(409,'Tipo de público não permitido')
            valores=[p['valor'] for p in publico]
            cur.execute(f'SELECT id FROM intranet_usuarios WHERE ativo=TRUE AND btrim({coluna})=ANY(%s)',(valores,))
        destinatarios=[r['id'] for r in cur.fetchall()]
        if not destinatarios: raise HTTPException(409,'Nenhum funcionário ativo corresponde ao público')
        # Cursos obrigatórios geram convocação; opcionais ficam no catálogo segmentado.
        cur.execute('SELECT obrigatorio FROM intranet_trein_cursos WHERE id=%s',(curso_id,))
        obrigatorio=cur.fetchone()['obrigatorio']
        if obrigatorio:
            for usuario_id in destinatarios:
                cur.execute('''INSERT INTO intranet_trein_participantes
                    (curso_id,usuario_id,origem,status,convocado_em,incluido_por)
                    SELECT %s,%s,'GRUPO','PENDENTE',NOW(),%s
                    WHERE NOT EXISTS(SELECT 1 FROM intranet_trein_participantes
                                     WHERE curso_id=%s AND usuario_id=%s)''',
                    (curso_id,usuario_id,u[0],curso_id,usuario_id))
        cur.execute("UPDATE intranet_trein_cursos SET status='PUBLICADO',publicado_em=NOW() WHERE id=%s",(curso_id,))
        cur.execute('''INSERT INTO intranet_trein_auditoria
          (usuario_id,curso_id,entidade,entidade_id,acao,detalhes)
          VALUES (%s,%s,'CURSO',%s,'PUBLICACAO',%s::jsonb)''',(u[0],curso_id,curso_id,'{}'))
        return {'id':curso_id,'status':'PUBLICADO','configuracao':config,'destinatarios':len(destinatarios),'convocacao':bool(obrigatorio)}


# Publico EAD: somente TODOS, DEPARTAMENTO, CARGO e CENTRO_CUSTO.
class PublicoTreinamento(BaseModel):
    tipo: Literal['TODOS','DEPARTAMENTO','CARGO','CENTRO_CUSTO']
    valores: list[str] = Field(default_factory=list)

@router_treinamentos.get('/admin/publico/opcoes')
def opcoes_publico_treinamento(authorization: Optional[str] = Header(None)):
    exigir_admin(usuario_atual(authorization))
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        resultado={}
        for tipo,coluna in [('DEPARTAMENTO','departamento'),('CARGO','cargo'),('CENTRO_CUSTO','centro_custo')]:
            cur.execute(f'''SELECT DISTINCT btrim({coluna}) AS valor FROM intranet_usuarios
                WHERE ativo=TRUE AND {coluna} IS NOT NULL AND btrim({coluna})<>''
                ORDER BY valor''')
            resultado[tipo]=[r['valor'] for r in cur.fetchall()]
        cur.execute('SELECT COUNT(*) AS total FROM intranet_usuarios WHERE ativo=TRUE')
        resultado['total_ativos']=cur.fetchone()['total']
        return resultado

@router_treinamentos.get('/admin/cursos/{curso_id}/publico')
def obter_publico_treinamento(curso_id:int, authorization:Optional[str]=Header(None)):
    exigir_admin(usuario_atual(authorization))
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute('SELECT tipo,valor FROM intranet_trein_publicos WHERE curso_id=%s ORDER BY id',(curso_id,))
        rows=cur.fetchall()
        return {'tipo':rows[0]['tipo'] if rows else None,
                'valores':[r['valor'] for r in rows if r['valor'] is not None]}

@router_treinamentos.put('/admin/cursos/{curso_id}/publico')
def salvar_publico_treinamento(curso_id:int, dados:PublicoTreinamento, authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization); exigir_admin(u)
    valores=list(dict.fromkeys(v.strip() for v in dados.valores if v.strip()))
    if dados.tipo=='TODOS':
        valores=[]
    elif not valores:
        raise HTTPException(422,'Selecione pelo menos um valor para o público')
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        _bloquear_concluido(cur,curso_id)
        cur.execute('SELECT status FROM intranet_trein_cursos WHERE id=%s FOR UPDATE',(curso_id,))
        curso=cur.fetchone()
        if not curso: raise HTTPException(404,'Curso não encontrado')
        if curso['status']!='RASCUNHO': raise HTTPException(409,'Somente rascunhos podem alterar o público')
        if dados.tipo!='TODOS':
            coluna={'DEPARTAMENTO':'departamento','CARGO':'cargo','CENTRO_CUSTO':'centro_custo'}[dados.tipo]
            cur.execute(f'''SELECT DISTINCT btrim({coluna}) AS valor FROM intranet_usuarios
                WHERE ativo=TRUE AND {coluna} IS NOT NULL AND btrim({coluna})<>'' ''')
            existentes={r['valor'] for r in cur.fetchall()}
            if any(v not in existentes for v in valores):
                raise HTTPException(422,'Há valores que não constam no cadastro de funcionários ativos')
        cur.execute('DELETE FROM intranet_trein_publicos WHERE curso_id=%s',(curso_id,))
        for valor in ([None] if dados.tipo=='TODOS' else valores):
            cur.execute('''INSERT INTO intranet_trein_publicos(curso_id,tipo,valor,definido_por)
                VALUES (%s,%s,%s,%s)''',(curso_id,dados.tipo,valor,u[0]))
        return {'salvo':True,'tipo':dados.tipo,'valores':valores}


# EAD funcionario - materiais, progresso e conclusao persistidos.
@router_treinamentos.get('/meus/{curso_id}/conteudo')
def conteudo_funcionario(curso_id:int, authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""SELECT p.id AS participante_id,p.status,c.titulo,c.descricao,c.modalidade
            FROM intranet_trein_participantes p JOIN intranet_trein_cursos c ON c.id=p.curso_id
            WHERE p.curso_id=%s AND p.usuario_id=%s AND c.status='PUBLICADO'
            ORDER BY p.id DESC LIMIT 1""",(curso_id,u[0]))
        participante=cur.fetchone()
        if not participante: raise HTTPException(404,'Treinamento nao encontrado para este funcionario')
        if participante['modalidade']!='EAD': raise HTTPException(409,'Curso nao e EAD')
        cur.execute("""SELECT a.id AS aula_id,a.titulo AS aula_titulo,a.descricao AS aula_descricao,
            m.id AS material_id,m.tipo,m.referencia,m.duracao_segundos,
            COALESCE(pr.posicao_segundos,0) AS posicao_segundos,
            COALESCE(pr.percentual_assistido,0) AS percentual_assistido,
            pr.concluido_em IS NOT NULL AS concluido,
            COALESCE(pr.segundos_assistidos,'[]'::jsonb) AS segundos_assistidos
            FROM intranet_trein_modulos mo JOIN intranet_trein_aulas a ON a.modulo_id=mo.id
            JOIN intranet_trein_materiais m ON m.aula_id=a.id
            LEFT JOIN intranet_trein_progresso pr ON pr.material_id=m.id AND pr.participante_id=%s
            WHERE mo.curso_id=%s AND m.tipo='VIDEO' AND NOT EXISTS
            (SELECT 1 FROM intranet_trein_auditoria au WHERE au.curso_id=%s
             AND au.entidade='AULA' AND au.entidade_id=a.id AND au.acao='EXCLUSAO_LOGICA')
            ORDER BY mo.ordem,a.ordem,m.ordem""",(participante['participante_id'],curso_id,curso_id))
        aulas=cur.fetchall()
        config=_config_ead(cur,curso_id)
        return {'participante':participante,'aulas':aulas,'configuracao':config}

class ProgressoMaterialEAD(BaseModel):
    posicao_segundos:int=Field(ge=0)
    percentual_assistido:float=Field(ge=0,le=100)
    concluido:bool=False
    duracao_real_segundos:Optional[int]=Field(default=None,ge=1)
    segundos_assistidos:list[int]=Field(default_factory=list)

@router_treinamentos.put('/meus/{curso_id}/materiais/{material_id}/progresso')
def salvar_progresso_ead(curso_id:int,material_id:int,dados:ProgressoMaterialEAD,authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""SELECT p.id,p.status,p.iniciado_em FROM intranet_trein_participantes p
          JOIN intranet_trein_cursos c ON c.id=p.curso_id
          WHERE p.curso_id=%s AND p.usuario_id=%s AND c.status='PUBLICADO'
          ORDER BY p.id DESC LIMIT 1 FOR UPDATE OF p""",(curso_id,u[0]))
        p=cur.fetchone()
        if not p: raise HTTPException(404,'Inscricao nao encontrada')
        if p['status'] in ('CONCLUIDO','CANCELADO'):
            raise HTTPException(409,'Treinamento encerrado')
        cur.execute("""SELECT m.id,m.duracao_segundos FROM intranet_trein_materiais m
          JOIN intranet_trein_aulas a ON a.id=m.aula_id
          JOIN intranet_trein_modulos mo ON mo.id=a.modulo_id
          WHERE mo.curso_id=%s AND m.id=%s AND m.tipo='VIDEO' AND NOT EXISTS
          (SELECT 1 FROM intranet_trein_auditoria au WHERE au.curso_id=%s
           AND au.entidade='AULA' AND au.entidade_id=a.id AND au.acao='EXCLUSAO_LOGICA')""",(curso_id,material_id,curso_id))
        m=cur.fetchone()
        if not m: raise HTTPException(404,'Material nao encontrado neste curso')
        # Mantem a duracao real do player em vez de usar a duracao estimada do cadastro.
        dur=int(dados.duracao_real_segundos or m['duracao_segundos'] or 0)
        if dur<=0: raise HTTPException(422,'Duracao do video indisponivel')
        if dur>86400: raise HTTPException(422,'Duracao do video invalida')
        pos=max(0,min(dados.posicao_segundos,dur))
        from datetime import datetime, timezone
        inicio=p.get('iniciado_em')
        decorrido=max(0,(datetime.now(timezone.utc)-inicio).total_seconds()) if inicio else 0
        cur.execute("""SELECT segundos_assistidos,concluido_em FROM intranet_trein_progresso
          WHERE participante_id=%s AND material_id=%s FOR UPDATE""",(p['id'],material_id))
        anterior=cur.fetchone()
        existentes={int(v) for v in ((anterior or {}).get('segundos_assistidos') or []) if isinstance(v,int) and 0<=v<dur}
        recebidos={v for v in dados.segundos_assistidos if isinstance(v,int) and 0<=v<dur}
        # Limita a quantidade total ao tempo de inscricao. Nao e uma prova antifraude,
        # mas evita conclusao instantanea por valores artificiais.
        uniao=existentes | recebidos
        limite=min(dur,int(decorrido)+2)
        if len(uniao)>limite:
            # Nao aceitar um pacote que extrapole o tempo de participacao.
            uniao=existentes if len(existentes)<=limite else set(sorted(existentes)[:limite])
        segundos=sorted(uniao)
        pct=round(100*len(segundos)/dur,4)
        concluir=bool(dados.concluido and pct>=95 and pos>=dur*.95)
        import json
        cur.execute("""INSERT INTO intranet_trein_progresso
          (participante_id,material_id,posicao_segundos,percentual_assistido,segundos_assistidos,concluido_em)
          VALUES (%s,%s,%s,%s,%s::jsonb,CASE WHEN %s THEN NOW() ELSE NULL END)
          ON CONFLICT (participante_id,material_id) DO UPDATE SET
          posicao_segundos=EXCLUDED.posicao_segundos,
          percentual_assistido=EXCLUDED.percentual_assistido,
          segundos_assistidos=EXCLUDED.segundos_assistidos,
          concluido_em=COALESCE(intranet_trein_progresso.concluido_em,EXCLUDED.concluido_em),
          atualizado_em=NOW()""",(p['id'],material_id,pos,pct,json.dumps(segundos),concluir))
        cur.execute("""SELECT COUNT(*) AS total,COUNT(pr.concluido_em) AS concluidos
          FROM intranet_trein_materiais m JOIN intranet_trein_aulas a ON a.id=m.aula_id
          JOIN intranet_trein_modulos mo ON mo.id=a.modulo_id
          LEFT JOIN intranet_trein_progresso pr ON pr.material_id=m.id AND pr.participante_id=%s
          WHERE mo.curso_id=%s AND m.tipo='VIDEO' AND NOT EXISTS
          (SELECT 1 FROM intranet_trein_auditoria au WHERE au.curso_id=%s
           AND au.entidade='AULA' AND au.entidade_id=a.id AND au.acao='EXCLUSAO_LOGICA')""",(p['id'],curso_id,curso_id))
        resumo=cur.fetchone()
        config=_config_ead(cur,curso_id)
        finalizado=resumo['total']>0 and resumo['total']==resumo['concluidos']
        if finalizado and not config['exigir_prova']:
            cur.execute("""UPDATE intranet_trein_participantes SET status='CONCLUIDO',
              concluido_em=COALESCE(concluido_em,NOW()),atualizado_em=NOW()
              WHERE id=%s AND status NOT IN ('CONCLUIDO','CANCELADO')""",(p['id'],))
        elif p['status']=='PENDENTE':
            cur.execute("""UPDATE intranet_trein_participantes SET status='EM_ANDAMENTO',
              iniciado_em=COALESCE(iniciado_em,NOW()),atualizado_em=NOW() WHERE id=%s""",(p['id'],))
        return {'salvo':True,'concluido':concluir,'percentual_assistido':pct,'aulas_concluidas':resumo['concluidos'],
                'total_aulas':resumo['total'],'segundos_assistidos':segundos,'curso_concluido':bool(finalizado and not config['exigir_prova']),
                'prova_pendente':bool(finalizado and config['exigir_prova'])}




# Prova EAD do funcionário: respostas e notas registradas na auditoria existente.
class RespostasProvaEAD(BaseModel):
    respostas: dict[str, int]


def _prova_participante(cur, curso_id, usuario_id, bloquear=False):
    cur.execute("""SELECT p.id,p.status,p.concluido_em FROM intranet_trein_participantes p
      JOIN intranet_trein_cursos c ON c.id=p.curso_id
      WHERE p.curso_id=%s AND p.usuario_id=%s AND c.modalidade='EAD'
      AND c.status='PUBLICADO' ORDER BY p.id DESC LIMIT 1""" +
      (' FOR UPDATE OF p' if bloquear else ''), (curso_id,usuario_id))
    p=cur.fetchone()
    if not p: raise HTTPException(404,'Inscrição não encontrada')
    config=_config_ead(cur,curso_id)
    if not config['exigir_prova']: raise HTTPException(409,'Este curso não exige prova')
    cur.execute("""SELECT COUNT(*) AS total, COUNT(pr.concluido_em) AS concluidos
      FROM intranet_trein_materiais m
      JOIN intranet_trein_aulas a ON a.id=m.aula_id
      JOIN intranet_trein_modulos mo ON mo.id=a.modulo_id
      LEFT JOIN intranet_trein_progresso pr ON pr.material_id=m.id AND pr.participante_id=%s
      WHERE mo.curso_id=%s AND m.tipo='VIDEO' AND NOT EXISTS
      (SELECT 1 FROM intranet_trein_auditoria au WHERE au.curso_id=%s
      AND au.entidade='AULA' AND au.entidade_id=a.id AND au.acao='EXCLUSAO_LOGICA')""",
      (p['id'],curso_id,curso_id))
    r=cur.fetchone()
    pronto=bool(r['total'] and r['total']==r['concluidos'])
    cur.execute("""SELECT detalhes FROM intranet_trein_auditoria
      WHERE curso_id=%s AND usuario_id=%s AND entidade='PARTICIPANTE'
      AND entidade_id=%s AND acao='PROVA_EAD_RESPONDIDA' ORDER BY id""",
      (curso_id,usuario_id,p['id']))
    tentativas=[x['detalhes'] for x in cur.fetchall()]
    aprovado=any(t.get('aprovado') for t in tentativas)
    return p,config,pronto,tentativas,aprovado


@router_treinamentos.get('/meus/{curso_id}/prova')
def consultar_prova_ead(curso_id:int, authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn,conn.cursor(row_factory=dict_row) as cur:
        p,config,pronto,tentativas,aprovado=_prova_participante(cur,curso_id,u[0])
        cur.execute("""SELECT q.id,q.enunciado FROM intranet_trein_questoes q
          JOIN intranet_trein_avaliacao_questoes aq ON aq.questao_id=q.id
          JOIN intranet_trein_avaliacoes av ON av.id=aq.avaliacao_id
          WHERE av.curso_id=%s AND q.ativa=TRUE ORDER BY q.id""",(curso_id,))
        questoes=cur.fetchall()
        for q in questoes:
            cur.execute('SELECT id,texto FROM intranet_trein_alternativas WHERE questao_id=%s ORDER BY ordem,id',(q['id'],))
            q['alternativas']=cur.fetchall()
        return {'liberada':pronto,'aprovado':aprovado,'nota_minima':config['nota_minima'],
          'tentativas_usadas':len(tentativas),'tentativas_maximas':config['tentativas_maximas'],
          'ultima_nota':tentativas[-1].get('nota') if tentativas else None,
          'questoes':questoes if pronto and not aprovado and len(tentativas)<config['tentativas_maximas'] else []}


@router_treinamentos.post('/meus/{curso_id}/prova')
def responder_prova_ead(curso_id:int,dados:RespostasProvaEAD,authorization:Optional[str]=Header(None)):
    import json
    u=usuario_atual(authorization)
    with _conectar() as conn,conn.cursor(row_factory=dict_row) as cur:
        p,config,pronto,tentativas,aprovado=_prova_participante(cur,curso_id,u[0],True)
        if not pronto: raise HTTPException(409,'Conclua todas as aulas antes da prova')
        if aprovado or p['status']=='CONCLUIDO': raise HTTPException(409,'Prova já aprovada')
        if len(tentativas)>=config['tentativas_maximas']:
            raise HTTPException(409,'Limite de tentativas atingido')
        cur.execute("""SELECT q.id,al.id AS alternativa_id,al.correta
          FROM intranet_trein_questoes q
          JOIN intranet_trein_avaliacao_questoes aq ON aq.questao_id=q.id
          JOIN intranet_trein_avaliacoes av ON av.id=aq.avaliacao_id
          JOIN intranet_trein_alternativas al ON al.questao_id=q.id
          WHERE av.curso_id=%s AND q.ativa=TRUE ORDER BY q.id,al.ordem""",(curso_id,))
        rows=cur.fetchall(); corretas={};validas={}
        for r in rows:
            validas.setdefault(r['id'],set()).add(r['alternativa_id'])
            if r['correta']: corretas[r['id']]=r['alternativa_id']
        if not validas or set(map(str,validas))!=set(dados.respostas):
            raise HTTPException(422,'Responda todas as questões')
        if any(dados.respostas[str(q)] not in ids for q,ids in validas.items()):
            raise HTTPException(422,'Alternativa inválida')
        acertos=sum(dados.respostas[str(q)]==corretas.get(q) for q in validas)
        nota=round(100*acertos/len(validas),2)
        passou=nota>=config['nota_minima']
        detalhes={'nota':nota,'aprovado':passou,'acertos':acertos,'total':len(validas),
                  'tentativa':len(tentativas)+1,'respostas':dados.respostas}
        cur.execute("""INSERT INTO intranet_trein_auditoria
          (usuario_id,curso_id,entidade,entidade_id,acao,detalhes)
          VALUES (%s,%s,'PARTICIPANTE',%s,'PROVA_EAD_RESPONDIDA',%s::jsonb)""",
          (u[0],curso_id,p['id'],json.dumps(detalhes)))
        if passou:
            cur.execute("""UPDATE intranet_trein_participantes SET status='CONCLUIDO',
              concluido_em=COALESCE(concluido_em,NOW()),atualizado_em=NOW()
              WHERE id=%s""",(p['id'],))
        return {'nota':nota,'aprovado':passou,'nota_minima':config['nota_minima'],
          'tentativas_restantes':config['tentativas_maximas']-len(tentativas)-1,
          'curso_concluido':passou}

# Certificados EAD: emissão idempotente e download apenas pelo participante.
@router_treinamentos.get('/meus/{curso_id}/certificado')
def baixar_certificado(curso_id: int, authorization: Optional[str] = Header(None)):
    from io import BytesIO
    from uuid import uuid4
    from fastapi.responses import StreamingResponse
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from datetime import datetime

    u = usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""SELECT p.id AS participante_id,p.status,p.concluido_em,
                 c.titulo,c.modalidade,c.carga_horaria_min,c.certificado_liberacao,
                 us.nome AS nome_participante
            FROM intranet_trein_participantes p
            JOIN intranet_trein_cursos c ON c.id=p.curso_id
            JOIN intranet_usuarios us ON us.id=p.usuario_id
            WHERE p.curso_id=%s AND p.usuario_id=%s
            ORDER BY p.id DESC LIMIT 1 FOR UPDATE OF p""",(curso_id,u[0]))
        p=cur.fetchone()
        if not p: raise HTTPException(404,'Inscrição não encontrada')
        if p['status']!='CONCLUIDO' or p['concluido_em'] is None:
            raise HTTPException(409,'O treinamento ainda não foi concluído')
        if p['modalidade']=='EAD' and p['certificado_liberacao']!='AUTOMATICA':
            raise HTTPException(409,'Certificado depende de liberação administrativa')
        cur.execute('SELECT * FROM intranet_trein_certificados WHERE participante_id=%s',
                    (p['participante_id'],))
        cert=cur.fetchone()
        if cert and cert['revogado_em'] is not None:
            raise HTTPException(403,'Certificado revogado')
        if cert and cert['liberado_em'] is None:
            raise HTTPException(403,'Certificado ainda não liberado')
        if not cert and p['modalidade']=='PRESENCIAL':
            raise HTTPException(409,'Certificado presencial ainda não liberado pelo administrador')
        if not cert:
            codigo='MEGAVIG-'+uuid4().hex.upper()[:20]
            cur.execute("""INSERT INTO intranet_trein_certificados
                (participante_id,codigo_validacao,arquivo_pdf_relativo,liberado_em,
                 nome_participante_snapshot,titulo_curso_snapshot,modalidade_snapshot,
                 carga_horaria_min_snapshot,data_conclusao_snapshot)
                VALUES (%s,%s,%s,NOW(),%s,%s,%s,%s,%s) RETURNING *""",
                (p['participante_id'],codigo,'gerado-sob-demanda/'+codigo+'.pdf',
                 p['nome_participante'],p['titulo'],p['modalidade'],
                 p['carga_horaria_min'],p['concluido_em'].date()))
            cert=cur.fetchone()
        nome=cert['nome_participante_snapshot']
        titulo=cert['titulo_curso_snapshot']
        minutos=cert['carga_horaria_min_snapshot']
        data=cert['data_conclusao_snapshot'].strftime('%d/%m/%Y')
        codigo=cert['codigo_validacao']
        modalidade=p['modalidade']
        detalhes_presencial=None
        if modalidade=='PRESENCIAL':
            cur.execute('''SELECT a.data_curso,a.inicio,a.fim,a.instrutor_nome,a.local
              FROM intranet_trein_participantes part
              JOIN intranet_trein_agendamentos a ON a.id=part.agendamento_id
              WHERE part.id=%s AND a.status='ENCERRADO' ''',(p['participante_id'],))
            detalhes_presencial=cur.fetchone()
            if not detalhes_presencial: raise HTTPException(409,'Turma presencial não encerrada')
        # Conteudo programatico oficial do curso, sem duplicar aulas por material.
        # A exclusao logica segue a mesma regra da consulta de aulas.
        cur.execute('''SELECT mo.titulo AS modulo_titulo, mo.descricao AS modulo_descricao,
                 a.titulo AS aula_titulo, a.descricao AS aula_descricao
            FROM intranet_trein_modulos mo
            JOIN intranet_trein_aulas a ON a.modulo_id=mo.id
            WHERE mo.curso_id=%s AND NOT EXISTS
              (SELECT 1 FROM intranet_trein_auditoria au
               WHERE au.curso_id=%s AND au.entidade='AULA'
                 AND au.entidade_id=a.id AND au.acao='EXCLUSAO_LOGICA')
            ORDER BY mo.ordem,mo.id,a.ordem,a.id''',(curso_id,curso_id))
        conteudo_programatico=cur.fetchall()

    empresa_certificado = _nome_empresa_certificado()
    titulo_institucional = 'UNIVERSIDADE CORPORATIVA - ' + empresa_certificado
    buf=BytesIO()
    page=canvas.Canvas(buf,pagesize=landscape(A4))
    w,h=landscape(A4)
    navy=HexColor('#15334F'); blue=HexColor('#17649B')
    gold=HexColor('#C39A52'); muted=HexColor('#65788A')
    paper=HexColor('#FCFDFE')
    page.setFillColor(paper); page.rect(0,0,w,h,fill=1,stroke=0)
    # Faixa institucional e acabamento discreto.
    page.setFillColor(navy); page.rect(0,h-13,w,13,fill=1,stroke=0)
    page.setFillColor(gold); page.rect(0,h-17,w,4,fill=1,stroke=0)
    page.setStrokeColor(HexColor('#D9E2E9')); page.setLineWidth(1)
    page.roundRect(25,25,w-50,h-65,12,fill=0,stroke=1)
    page.setStrokeColor(gold); page.setLineWidth(1.4)
    page.line(55,h-48,150,h-48); page.line(w-150,h-48,w-55,h-48)
    # Selo institucional vetorial: não depende de imagem externa.
    cx=w/2
    page.setStrokeColor(gold);page.setLineWidth(1.4)
    page.circle(cx,h-69,17,fill=0,stroke=1)
    page.setFillColor(blue);page.setFont('Helvetica-Bold',12)
    page.drawCentredString(cx,h-73,'C')
    page.setFillColor(navy);page.setFont('Helvetica-Bold',12)
    page.setFont('Helvetica-Bold', min(12, 740 / max(stringWidth(titulo_institucional, 'Helvetica-Bold', 12), 1) * 12))
    page.drawCentredString(cx,h-102,titulo_institucional)
    page.setFillColor(muted);page.setFont('Helvetica',9)
    page.drawCentredString(cx,h-119,'DESENVOLVIMENTO E CAPACITACAO PROFISSIONAL')
    page.setFillColor(blue);page.setFont('Helvetica-Bold',31)
    page.drawCentredString(cx,h-170,'CERTIFICADO DE CONCLUSAO')
    page.setStrokeColor(gold);page.setLineWidth(2)
    page.line(cx-58,h-184,cx+58,h-184)
    page.setFillColor(muted);page.setFont('Helvetica',12)
    page.drawCentredString(cx,h-220,'Certificamos que')

    def linhas_ajustadas(texto, tamanho, limite=690, max_linhas=3):
        palavras=str(texto).split(); linhas=[]; linha=''
        for palavra in palavras:
            tentativa=(linha+' '+palavra).strip()
            if linha and stringWidth(tentativa,'Helvetica-Bold',tamanho)>limite:
                linhas.append(linha);linha=palavra
            else: linha=tentativa
        if linha: linhas.append(linha)
        return linhas[:max_linhas]

    def escrever_destaque(texto, y, tamanho=23, limite=700):
        texto=str(texto).strip()
        while tamanho>13 and len(linhas_ajustadas(texto,tamanho,limite))>2:
            tamanho-=1
        linhas=linhas_ajustadas(texto,tamanho,limite)
        page.setFont('Helvetica-Bold',tamanho);page.setFillColor(navy)
        for linha in linhas:
            page.drawCentredString(cx,y,linha);y-=tamanho+6
        return y

    y=escrever_destaque(nome,h-257,24)
    page.setFillColor(muted);page.setFont('Helvetica',12)
    page.drawCentredString(cx,y-4,'concluiu com aproveitamento o treinamento')
    y=escrever_destaque(titulo,y-44,21)
    # Quadro do certificado: data de realizacao (nao data de encerramento),
    # horarios, local e instrutor para PRESENCIAL. EAD permanece inalterado.
    quadro_y=119
    horas=f'{minutos//60}h' + (f' {minutos%60}min' if minutos%60 else '')
    if detalhes_presencial:
        quadro_y=111
        page.setFillColor(HexColor('#EFF4F8'))
        page.roundRect(95,quadro_y,w-190,90,9,fill=1,stroke=0)
        page.setStrokeColor(HexColor('#CCD8E1'));page.setLineWidth(0.7)
        page.line(cx,quadro_y+48,cx,quadro_y+81)
        page.line(110,quadro_y+44,w-110,quadro_y+44)
        page.line(cx,quadro_y+8,cx,quadro_y+37)
        dia=detalhes_presencial['data_curso'].strftime('%d/%m/%Y')
        def hora_texto(valor):
            return str(valor)[:5] if valor is not None else '--:--'
        horario=f"{hora_texto(detalhes_presencial['inicio'])} às {hora_texto(detalhes_presencial['fim'])}"
        def campo_central(rotulo,valor,x,y,limite=320):
            page.setFillColor(muted);page.setFont('Helvetica-Bold',8)
            page.drawCentredString(x,y+15,rotulo)
            tamanho=12
            valor=str(valor or 'Não informado')
            while tamanho>8 and stringWidth(valor,'Helvetica-Bold',tamanho)>limite:
                tamanho-=1
            page.setFillColor(navy);page.setFont('Helvetica-Bold',tamanho)
            page.drawCentredString(x,y-2,valor)
        campo_central('DATA DE REALIZACAO',dia, (95+cx)/2,quadro_y+59)
        campo_central('HORARIO',horario,(cx+w-95)/2,quadro_y+59)
        campo_central('LOCAL',detalhes_presencial['local'],(95+cx)/2,quadro_y+14)
        campo_central('INSTRUTOR',detalhes_presencial['instrutor_nome'],(cx+w-95)/2,quadro_y+14)
        # A carga horaria e informada no rodape, sem confundir realizacao com conclusao.
        page.setFillColor(navy);page.setFont('Helvetica-Bold',9)
        page.drawCentredString(cx,quadro_y-14,'CARGA HORARIA: '+horas)
    else:
        page.setFillColor(HexColor('#EFF4F8'))
        page.roundRect(135,quadro_y,w-270,66,9,fill=1,stroke=0)
        page.setStrokeColor(HexColor('#CCD8E1'));page.setLineWidth(0.7)
        page.line(cx,quadro_y+13,cx,quadro_y+53)
        page.setFillColor(muted);page.setFont('Helvetica-Bold',9)
        page.drawCentredString(275,quadro_y+43,'CARGA HORARIA')
        page.drawCentredString(w-275,quadro_y+43,'DATA DE CONCLUSAO')
        page.setFillColor(navy);page.setFont('Helvetica-Bold',15)
        page.drawCentredString(275,quadro_y+19,horas)
        page.drawCentredString(w-275,quadro_y+19,data)
    # Rodape para autenticidade e segunda via.
    page.setStrokeColor(HexColor('#DFE5EA'));page.line(64,88,w-64,88)
    page.setFillColor(navy);page.setFont('Helvetica-Bold',9)
    page.drawCentredString(cx,73,'EMITIDO PELA UNIVERSIDADE CORPORATIVA - ' + empresa_certificado)
    page.setFillColor(muted);page.setFont('Helvetica',9)
    page.drawCentredString(cx,56,'Codigo de autenticidade: '+codigo)
    page.setFont('Helvetica',8)
    if not detalhes_presencial:
        page.drawCentredString(cx,40,'Documento digital emitido com registro de conclusao no sistema corporativo.')
    # Pagina 2 (e continuacoes, se necessario): conteudo programatico.
    # Preserva integralmente o desenho da primeira pagina e o codigo original.
    from reportlab.platypus import Paragraph
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_LEFT
    from xml.sax.saxutils import escape as escape_xml

    estilo_modulo=ParagraphStyle('cert_modulo',fontName='Helvetica-Bold',fontSize=12,
        leading=16,textColor=navy,alignment=TA_LEFT)
    estilo_aula=ParagraphStyle('cert_aula',fontName='Helvetica-Bold',fontSize=10.5,
        leading=14,textColor=navy,alignment=TA_LEFT)
    estilo_descricao=ParagraphStyle('cert_descricao',fontName='Helvetica',fontSize=9.5,
        leading=13.5,textColor=muted,alignment=TA_LEFT)
    estilo_info=ParagraphStyle('cert_info',fontName='Helvetica',fontSize=10,
        leading=15,textColor=navy,alignment=TA_LEFT)

    def cabecalho_conteudo():
        page.setFillColor(paper);page.rect(0,0,w,h,fill=1,stroke=0)
        page.setFillColor(navy);page.rect(0,h-13,w,13,fill=1,stroke=0)
        page.setFillColor(gold);page.rect(0,h-17,w,4,fill=1,stroke=0)
        page.setStrokeColor(HexColor('#D9E2E9'));page.setLineWidth(1)
        page.roundRect(25,25,w-50,h-65,12,fill=0,stroke=1)
        page.setFillColor(navy);page.setFont('Helvetica-Bold',13)
        page.drawString(55,h-65,titulo_institucional)
        page.setFillColor(blue);page.setFont('Helvetica-Bold',22)
        page.drawString(55,h-108,'CONTEUDO PROGRAMATICO')
        page.setStrokeColor(gold);page.setLineWidth(1.5)
        page.line(55,h-120,w-55,h-120)
        page.setStrokeColor(HexColor('#DFE5EA'));page.line(55,67,w-55,67)
        page.setFillColor(muted);page.setFont('Helvetica',8)
        page.drawString(55,51,'Codigo de autenticidade: '+str(codigo))
        page.drawRightString(w-55,51,'Universidade Corporativa - ' + empresa_certificado + ' - Anexo ao certificado')
        return h-145

    margem=57
    largura=w-2*margem
    y_conteudo=None
    def escrever_paragrafo(texto,estilo,espaco=7):
        nonlocal y_conteudo
        par=Paragraph(escape_xml(str(texto or '')),estilo)
        _,altura=par.wrap(largura, h)
        if y_conteudo-altura < 86:
            page.showPage()
            y_conteudo=cabecalho_conteudo()
        par.drawOn(page,margem,y_conteudo-altura)
        y_conteudo-=altura+espaco

    page.showPage()
    y_conteudo=cabecalho_conteudo()
    escrever_paragrafo('Curso: '+str(titulo),estilo_info,5)
    escrever_paragrafo('Participante: '+str(nome),estilo_info,5)
    escrever_paragrafo('Modalidade: '+str(modalidade)+'   |   Carga horaria: '+horas+
                       '   |   Conclusao: '+data,estilo_info,15)
    if detalhes_presencial:
        dia=detalhes_presencial['data_curso'].strftime('%d/%m/%Y')
        escrever_paragrafo('Realizacao: '+dia+'   |   Horario: '+horario,estilo_info,4)
        escrever_paragrafo('Local: '+str(detalhes_presencial['local'] or 'Nao informado'),estilo_info,4)
        escrever_paragrafo('Instrutor: '+str(detalhes_presencial['instrutor_nome'] or 'Nao informado'),estilo_info,12)
    if not conteudo_programatico:
        escrever_paragrafo('Nenhuma aula cadastrada no conteudo programatico.',estilo_descricao)
    else:
        modulo_anterior=None
        for item in conteudo_programatico:
            modulo=item['modulo_titulo'] or 'Modulo'
            if modulo!=modulo_anterior:
                escrever_paragrafo(modulo,estilo_modulo,5)
                if item['modulo_descricao']:
                    escrever_paragrafo(item['modulo_descricao'],estilo_descricao,9)
                modulo_anterior=modulo
            escrever_paragrafo('Aula: '+str(item['aula_titulo'] or 'Sem titulo'),estilo_aula,3)
            if item['aula_descricao']:
                escrever_paragrafo(item['aula_descricao'],estilo_descricao,10)
            else:
                y_conteudo-=7
    import re
    nome_arquivo_empresa = re.sub(r'[^a-z0-9]+', '-', empresa_certificado.lower()).strip('-') or 'intranet'
    page.setTitle('Certificado - '+titulo)
    page.save();buf.seek(0)
    return StreamingResponse(buf,media_type='application/pdf',headers={
        'Content-Disposition':f'attachment; filename="certificado-{nome_arquivo_empresa}-{curso_id}.pdf"',
        'Cache-Control':'no-store'})


@router_treinamentos.post('/admin/cursos/{curso_id}/participantes/{participante_id}/liberar-certificado')
def liberar_certificado_presencial(curso_id:int,participante_id:int,authorization:Optional[str]=Header(None)):
    from uuid import uuid4
    import json
    u=usuario_atual(authorization); exigir_admin(u)
    with _conectar() as conn,conn.cursor(row_factory=dict_row) as cur:
        cur.execute('''SELECT p.id,p.status,p.concluido_em,c.titulo,c.modalidade,c.carga_horaria_min,
          us.nome,a.status AS status_turma,pr.presente
          FROM intranet_trein_participantes p
          JOIN intranet_trein_cursos c ON c.id=p.curso_id
          JOIN intranet_usuarios us ON us.id=p.usuario_id
          JOIN intranet_trein_agendamentos a ON a.id=p.agendamento_id
          JOIN intranet_trein_presencas pr ON pr.participante_id=p.id
          WHERE p.id=%s AND p.curso_id=%s FOR UPDATE OF p''',(participante_id,curso_id))
        p=cur.fetchone()
        if not p: raise HTTPException(404,'Participante presencial não encontrado')
        if p['modalidade']!='PRESENCIAL' or p['status_turma']!='ENCERRADO' or p['presente'] is not True or p['status']!='CONCLUIDO' or not p['concluido_em']:
            raise HTTPException(409,'Somente participantes presentes e concluídos de turmas encerradas podem receber certificado')
        cur.execute('SELECT * FROM intranet_trein_certificados WHERE participante_id=%s FOR UPDATE',(participante_id,))
        cert=cur.fetchone()
        if cert and cert['revogado_em'] is not None: raise HTTPException(409,'Certificado revogado: revisão administrativa necessária')
        if cert and cert['liberado_em'] is not None:
            return {'participante_id':participante_id,'liberado':True,'ja_liberado':True,'codigo_validacao':cert['codigo_validacao']}
        if cert:
            cur.execute('UPDATE intranet_trein_certificados SET liberado_em=NOW() WHERE participante_id=%s RETURNING codigo_validacao',(participante_id,))
            codigo=cur.fetchone()['codigo_validacao']
        else:
            codigo='MEGAVIG-'+uuid4().hex.upper()[:20]
            cur.execute('''INSERT INTO intranet_trein_certificados
              (participante_id,codigo_validacao,arquivo_pdf_relativo,liberado_em,
               nome_participante_snapshot,titulo_curso_snapshot,modalidade_snapshot,
               carga_horaria_min_snapshot,data_conclusao_snapshot)
              VALUES (%s,%s,%s,NOW(),%s,%s,'PRESENCIAL',%s,%s)''',
              (participante_id,codigo,'gerado-sob-demanda/'+codigo+'.pdf',p['nome'],p['titulo'],p['carga_horaria_min'],p['concluido_em'].date()))
        cur.execute('''INSERT INTO intranet_trein_auditoria(usuario_id,curso_id,entidade,entidade_id,acao,detalhes)
          VALUES (%s,%s,'CERTIFICADO',%s,'LIBERACAO',%s::jsonb)''',
          (u[0],curso_id,participante_id,json.dumps({'codigo_validacao':codigo})))
        return {'participante_id':participante_id,'liberado':True,'codigo_validacao':codigo}


# Presencial: turmas, convocacoes individuais e registro de presenca.
class TurmaPresencial(BaseModel):
    data_curso: str
    inicio: str
    fim: str
    local: str = Field(min_length=2, max_length=250)
    instrutor_nome: str = Field(min_length=2, max_length=200)
    instrutor_empresa: str = Field(default='', max_length=200)

def _curso_presencial(cur, curso_id, exigir_rascunho=False):
    cur.execute("SELECT id,status,modalidade FROM intranet_trein_cursos WHERE id=%s FOR UPDATE",(curso_id,))
    c=cur.fetchone()
    if not c: raise HTTPException(404,'Curso não encontrado')
    if c['modalidade']!='PRESENCIAL': raise HTTPException(409,'Operação exclusiva de curso presencial')
    if c['status']=='DESATIVADO' or (exigir_rascunho and c['status']!='RASCUNHO'):
        raise HTTPException(409,'Curso não permite esta alteração')
    return c

@router_treinamentos.get('/admin/cursos/{curso_id}/turmas')
def listar_turmas(curso_id:int,authorization:Optional[str]=Header(None)):
    exigir_admin(usuario_atual(authorization))
    with _conectar() as conn,conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id FROM intranet_trein_cursos WHERE id=%s AND modalidade='PRESENCIAL'",(curso_id,))
        if not cur.fetchone(): raise HTTPException(404,'Curso presencial não encontrado')
        cur.execute("""SELECT a.*, (SELECT COUNT(*) FROM intranet_trein_participantes p WHERE p.agendamento_id=a.id) AS inscritos,
          (SELECT COUNT(*) FROM intranet_trein_participantes p JOIN intranet_trein_presencas pr ON pr.participante_id=p.id
           WHERE p.agendamento_id=a.id AND pr.presente=TRUE) AS presentes
          FROM intranet_trein_agendamentos a WHERE a.curso_id=%s ORDER BY a.data_curso,a.inicio,a.id""",(curso_id,))
        return cur.fetchall()

@router_treinamentos.post('/admin/cursos/{curso_id}/turmas',status_code=201)
def criar_turma(curso_id:int,dados:TurmaPresencial,authorization:Optional[str]=Header(None)):
    from datetime import date,time
    u=usuario_atual(authorization); exigir_admin(u)
    try:
        d=date.fromisoformat(dados.data_curso)
        i=time.fromisoformat(dados.inicio); f=time.fromisoformat(dados.fim)
    except ValueError: raise HTTPException(422,'Data ou horários inválidos')
    if f<=i: raise HTTPException(422,'Término deve ser posterior ao início')
    with _conectar() as conn,conn.cursor(row_factory=dict_row) as cur:
        _bloquear_concluido(cur,curso_id)
        _curso_presencial(cur,curso_id,True)
        cur.execute("""INSERT INTO intranet_trein_agendamentos
          (curso_id,data_curso,inicio,fim,local,instrutor_nome,instrutor_empresa)
          VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
          (curso_id,d,i,f,dados.local.strip(),dados.instrutor_nome.strip(),dados.instrutor_empresa.strip()))
        tid=cur.fetchone()['id']
        cur.execute("""INSERT INTO intranet_trein_auditoria(usuario_id,curso_id,entidade,entidade_id,acao,detalhes)
          VALUES (%s,%s,'TURMA',%s,'CRIACAO',%s::jsonb)""",(u[0],curso_id,tid,'{}'))
        return {'id':tid,'criado':True}

@router_treinamentos.get('/admin/funcionarios')
def funcionarios_treinamento(authorization:Optional[str]=Header(None)):
    exigir_admin(usuario_atual(authorization))
    with _conectar() as conn,conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id,nome,cargo,centro_custo FROM intranet_usuarios WHERE ativo=TRUE ORDER BY nome,id")
        return cur.fetchall()

class ParticipantesTurma(BaseModel):
    usuario_ids: list[int] = Field(default_factory=list)

@router_treinamentos.get('/admin/cursos/{curso_id}/turmas/{turma_id}/participantes')
def listar_participantes_turma(curso_id:int,turma_id:int,authorization:Optional[str]=Header(None)):
    exigir_admin(usuario_atual(authorization))
    with _conectar() as conn,conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""SELECT p.id AS participante_id,p.usuario_id,u.nome,u.cargo,u.centro_custo,p.status,
          pr.presente FROM intranet_trein_participantes p
          JOIN intranet_usuarios u ON u.id=p.usuario_id
          LEFT JOIN intranet_trein_presencas pr ON pr.participante_id=p.id
          WHERE p.curso_id=%s AND p.agendamento_id=%s ORDER BY u.nome""",(curso_id,turma_id))
        return cur.fetchall()

@router_treinamentos.post('/admin/cursos/{curso_id}/turmas/{turma_id}/participantes')
def incluir_participantes_turma(curso_id:int,turma_id:int,dados:ParticipantesTurma,authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization); exigir_admin(u)
    ids=list(dict.fromkeys(dados.usuario_ids))
    if not ids: raise HTTPException(422,'Selecione funcionários')
    with _conectar() as conn,conn.cursor(row_factory=dict_row) as cur:
        _bloquear_concluido(cur,curso_id)
        _curso_presencial(cur,curso_id,True)
        cur.execute("SELECT id,status FROM intranet_trein_agendamentos WHERE id=%s AND curso_id=%s FOR UPDATE",(turma_id,curso_id))
        turma=cur.fetchone()
        if not turma or turma['status']!='AGENDADO': raise HTTPException(409,'Turma inexistente ou não agendada')
        cur.execute("SELECT id FROM intranet_usuarios WHERE ativo=TRUE AND id=ANY(%s)",(ids,))
        validos={r['id'] for r in cur.fetchall()}
        if len(validos)!=len(ids): raise HTTPException(422,'Há funcionários inexistentes ou inativos')
        incluidos=0
        for uid in ids:
            cur.execute("""INSERT INTO intranet_trein_participantes
              (curso_id,usuario_id,agendamento_id,origem,status,convocado_em,incluido_por)
              VALUES (%s,%s,%s,'RH','PENDENTE',NOW(),%s)
              ON CONFLICT (agendamento_id,usuario_id) WHERE agendamento_id IS NOT NULL DO NOTHING
              RETURNING id""",(curso_id,uid,turma_id,u[0]))
            if cur.fetchone(): incluidos+=1
        return {'incluidos':incluidos,'solicitados':len(ids)}

class PresencaTurma(BaseModel):
    presente: bool

@router_treinamentos.put('/admin/cursos/{curso_id}/turmas/{turma_id}/participantes/{participante_id}/presenca')
def registrar_presenca_turma(curso_id:int,turma_id:int,participante_id:int,dados:PresencaTurma,authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization); exigir_admin(u)
    with _conectar() as conn,conn.cursor(row_factory=dict_row) as cur:
        _curso_presencial(cur,curso_id)
        cur.execute("SELECT status FROM intranet_trein_agendamentos WHERE id=%s AND curso_id=%s FOR UPDATE",(turma_id,curso_id))
        ag=cur.fetchone()
        if not ag: raise HTTPException(404,'Turma não encontrada')
        if ag['status']!='AGENDADO': raise HTTPException(409,'Turma encerrada ou cancelada: presença bloqueada')
        cur.execute("SELECT id FROM intranet_trein_participantes WHERE id=%s AND curso_id=%s AND agendamento_id=%s FOR UPDATE",
                    (participante_id,curso_id,turma_id))
        if not cur.fetchone(): raise HTTPException(404,'Participante não pertence à turma')
        cur.execute("""INSERT INTO intranet_trein_presencas(participante_id,presente,registrado_por)
          VALUES (%s,%s,%s) ON CONFLICT (participante_id) DO UPDATE
          SET presente=EXCLUDED.presente,atualizado_por=EXCLUDED.registrado_por,atualizado_em=NOW()""",
          (participante_id,dados.presente,u[0]))
        return {'participante_id':participante_id,'presente':dados.presente}

@router_treinamentos.post('/admin/cursos/{curso_id}/turmas/{turma_id}/encerrar')
def encerrar_turma_presencial(curso_id:int,turma_id:int,authorization:Optional[str]=Header(None)):
    from datetime import date
    import json
    u=usuario_atual(authorization); exigir_admin(u)
    with _conectar() as conn,conn.cursor(row_factory=dict_row) as cur:
        curso=_curso_presencial(cur,curso_id)
        if curso['status']!='PUBLICADO': raise HTTPException(409,'O curso deve estar publicado')
        cur.execute("SELECT id,data_curso,status FROM intranet_trein_agendamentos WHERE id=%s AND curso_id=%s FOR UPDATE",(turma_id,curso_id))
        turma=cur.fetchone()
        if not turma: raise HTTPException(404,'Turma não encontrada')
        if turma['status']!='AGENDADO': raise HTTPException(409,'Turma já encerrada ou cancelada')
        if turma['data_curso']>date.today(): raise HTTPException(409,'Não é permitido encerrar uma turma antes da data do treinamento')
        cur.execute("""SELECT p.id,p.status,pr.presente FROM intranet_trein_participantes p
          LEFT JOIN intranet_trein_presencas pr ON pr.participante_id=p.id
          WHERE p.curso_id=%s AND p.agendamento_id=%s FOR UPDATE OF p""",(curso_id,turma_id))
        participantes=cur.fetchall()
        if not participantes: raise HTTPException(409,'Turma sem participantes')
        sem_registro=sum(p['presente'] is None for p in participantes)
        if sem_registro: raise HTTPException(409,f'Existem {sem_registro} participantes sem presença registrada. Marque presente ou ausente para todos.')
        if any(p['status']=='CONCLUIDO' for p in participantes): raise HTTPException(409,'Há participantes já concluídos; revise o histórico antes de encerrar')
        presentes=[p['id'] for p in participantes if p['presente'] is True]
        if presentes:
            cur.execute("""UPDATE intranet_trein_participantes SET status='CONCLUIDO',
              concluido_em=NOW(),atualizado_em=NOW() WHERE id=ANY(%s)""",(presentes,))
        cur.execute("""UPDATE intranet_trein_agendamentos SET status='ENCERRADO',
          encerrado_em=NOW(),encerrado_por=%s WHERE id=%s""",(u[0],turma_id))
        detalhes=json.dumps({'presentes_concluidos':len(presentes),'ausentes_pendentes':len(participantes)-len(presentes)})
        cur.execute("""INSERT INTO intranet_trein_auditoria(usuario_id,curso_id,entidade,entidade_id,acao,detalhes)
          VALUES (%s,%s,'TURMA',%s,'ENCERRAMENTO',%s::jsonb)""",(u[0],curso_id,turma_id,detalhes))
        return {'turma_id':turma_id,'status':'ENCERRADO','concluidos':len(presentes),
                'ausentes_pendentes':len(participantes)-len(presentes)}

@router_treinamentos.post('/admin/cursos/{curso_id}/publicar-presencial')
def publicar_presencial(curso_id:int,authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization); exigir_admin(u)
    with _conectar() as conn,conn.cursor(row_factory=dict_row) as cur:
        _bloquear_concluido(cur,curso_id)
        _curso_presencial(cur,curso_id,True)
        cur.execute("SELECT COUNT(*) AS total FROM intranet_trein_agendamentos WHERE curso_id=%s AND status='AGENDADO'",(curso_id,))
        if cur.fetchone()['total']<1: raise HTTPException(409,'Cadastre pelo menos uma turma')
        cur.execute("SELECT COUNT(*) AS total FROM intranet_trein_participantes WHERE curso_id=%s AND agendamento_id IS NOT NULL",(curso_id,))
        if cur.fetchone()['total']<1: raise HTTPException(409,'Inclua pelo menos um participante nas turmas')
        cur.execute("UPDATE intranet_trein_cursos SET status='PUBLICADO',publicado_em=NOW() WHERE id=%s",(curso_id,))
        cur.execute("""INSERT INTO intranet_trein_auditoria(usuario_id,curso_id,entidade,entidade_id,acao,detalhes)
          VALUES (%s,%s,'CURSO',%s,'PUBLICACAO_PRESENCIAL',%s::jsonb)""",(u[0],curso_id,curso_id,'{}'))
        return {'id':curso_id,'status':'PUBLICADO'}


@router_treinamentos.get('/meus/{curso_id}/turma')
def minha_turma_presencial(curso_id: int, authorization: Optional[str] = Header(None)):
    """Dados da turma apenas para participante autenticado; não expõe outros inscritos."""
    u = usuario_atual(authorization)
    with _conectar() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""SELECT a.data_curso, a.inicio, a.fim, a.local,
                             a.instrutor_nome, a.instrutor_empresa, a.status AS status_turma
                       FROM intranet_trein_participantes p
                       JOIN intranet_trein_cursos c ON c.id=p.curso_id AND c.modalidade='PRESENCIAL'
                       JOIN intranet_trein_agendamentos a ON a.id=p.agendamento_id AND a.curso_id=p.curso_id
                       WHERE p.curso_id=%s AND p.usuario_id=%s
                       ORDER BY a.data_curso DESC, a.id DESC LIMIT 1""", (curso_id, u[0]))
        turma = cur.fetchone()
        if turma is None:
            raise HTTPException(404, 'Turma presencial não encontrada para este funcionário')
        return turma
