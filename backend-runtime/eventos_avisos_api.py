"""Eventos e Avisos V12: depende das tabelas em 01_banco.sql."""
from datetime import date, timedelta, datetime, time, timezone
from zoneinfo import ZoneInfo
import secrets
import hashlib
import re
import calendar
from typing import Optional, Literal
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router_eventos_avisos = APIRouter(prefix='/api/eventos-avisos', tags=['Eventos e Avisos'])
_conectar = _autenticar = None

def configurar_eventos_avisos(conectar, autenticar):
    global _conectar, _autenticar
    _conectar, _autenticar = conectar, autenticar

def usuario_atual(authorization, admin=False):
    u = _autenticar(authorization)
    if u[5]:
        raise HTTPException(403, 'Troca de senha obrigatória')
    if admin and u[3] < 3:
        raise HTTPException(403, 'Acesso exclusivo do administrador')
    return u

class Publicacao(BaseModel):
    tipo: Literal['EVENTO','AVISO']
    titulo: str = Field(min_length=1,max_length=250)
    descricao: str = Field(min_length=1)
    inicio: date
    fim: date
    publico_tipo: Literal['TODOS','CARGO','DEPARTAMENTO','USUARIO']
    alvos: list[str] = Field(default_factory=list)
    exige_ciencia: bool = False

def validar(d):
    if d.fim < d.inicio: raise HTTPException(400, 'Fim anterior ao início')
    if d.tipo == 'EVENTO' and d.exige_ciencia: raise HTTPException(400, 'Ciência apenas para avisos')
    if d.publico_tipo != 'TODOS' and not any(str(x).strip() for x in d.alvos):
        raise HTTPException(400, 'Selecione ao menos um destinatário')
    if d.publico_tipo == 'TODOS' and d.alvos: raise HTTPException(400, 'TODOS não recebe alvos')

@router_eventos_avisos.post('/admin',status_code=201)
def criar(d:Publicacao,authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization,True); validar(d)
    with _conectar() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO intranet_publicacoes(tipo,titulo,descricao,inicio,fim,publico_tipo,exige_ciencia,criado_por)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
            (d.tipo,d.titulo.strip(),d.descricao.strip(),d.inicio,d.fim,d.publico_tipo,d.exige_ciencia,str(u[0])))
        pid=cur.fetchone()[0]
        for alvo in set(x.strip() for x in d.alvos if x.strip()):
            cur.execute('INSERT INTO intranet_publicacoes_alvos(publicacao_id,valor) VALUES(%s,%s)',(pid,alvo))
    return {'id':pid,'status':'RASCUNHO'}

@router_eventos_avisos.get('/admin')
def listar_admin(authorization:Optional[str]=Header(None)):
    usuario_atual(authorization,True)
    with _conectar() as conn, conn.cursor() as cur:
        cur.execute('''SELECT p.id,p.tipo,p.titulo,p.inicio,p.fim,p.publico_tipo,p.exige_ciencia,p.status,
          COUNT(d.usuario_id),COUNT(d.visualizado_em),COUNT(d.confirmado_em)
          FROM intranet_publicacoes p LEFT JOIN intranet_publicacoes_destinatarios d ON d.publicacao_id=p.id
          GROUP BY p.id ORDER BY p.id DESC''')
        rows=cur.fetchall()
    return [{'id':r[0],'tipo':r[1],'titulo':r[2],'inicio':r[3],'fim':r[4],
             'publico_tipo':r[5],'exige_ciencia':r[6],'status':r[7],
             'destinatarios':r[8],'visualizados':r[9],'confirmados':r[10],
             'pendentes':r[8]-(r[10] if r[6] else r[9])} for r in rows]

@router_eventos_avisos.get('/admin/{pid}')
def obter_rascunho(pid:int,authorization:Optional[str]=Header(None)):
    usuario_atual(authorization,True)
    with _conectar() as conn,conn.cursor() as cur:
        cur.execute('SELECT tipo,titulo,descricao,inicio,fim,publico_tipo,exige_ciencia,status FROM intranet_publicacoes WHERE id=%s',(pid,))
        r=cur.fetchone()
        if not r: raise HTTPException(404,'Publicação não encontrada')
        if r[7]!='RASCUNHO': raise HTTPException(409,'Somente rascunhos podem ser editados')
        cur.execute('SELECT valor FROM intranet_publicacoes_alvos WHERE publicacao_id=%s ORDER BY valor',(pid,))
        alvos=[x[0] for x in cur.fetchall()]
    return {'id':pid,'tipo':r[0],'titulo':r[1],'descricao':r[2],'inicio':r[3],'fim':r[4],'publico_tipo':r[5],'exige_ciencia':r[6],'status':r[7],'alvos':alvos}

@router_eventos_avisos.put('/admin/{pid}')
def atualizar_rascunho(pid:int,d:Publicacao,authorization:Optional[str]=Header(None)):
    usuario_atual(authorization,True);validar(d)
    with _conectar() as conn,conn.cursor() as cur:
        cur.execute('SELECT status FROM intranet_publicacoes WHERE id=%s FOR UPDATE',(pid,))
        r=cur.fetchone()
        if not r: raise HTTPException(404,'Publicação não encontrada')
        if r[0]!='RASCUNHO': raise HTTPException(409,'Publicações processadas não podem ser editadas')
        cur.execute("UPDATE intranet_publicacoes SET tipo=%s,titulo=%s,descricao=%s,inicio=%s,fim=%s,publico_tipo=%s,exige_ciencia=%s,atualizado_em=now() WHERE id=%s",
            (d.tipo,d.titulo.strip(),d.descricao.strip(),d.inicio,d.fim,d.publico_tipo,d.exige_ciencia,pid))
        cur.execute('DELETE FROM intranet_publicacoes_alvos WHERE publicacao_id=%s',(pid,))
        for alvo in set(x.strip() for x in d.alvos if x.strip()):
            cur.execute('INSERT INTO intranet_publicacoes_alvos(publicacao_id,valor) VALUES(%s,%s)',(pid,alvo))
    return {'id':pid,'status':'RASCUNHO'}

@router_eventos_avisos.post('/admin/{pid}/publicar')
def publicar(pid:int,authorization:Optional[str]=Header(None)):
    usuario_atual(authorization,True)
    with _conectar() as conn,conn.cursor() as cur:
        cur.execute('''SELECT tipo,publico_tipo,status FROM intranet_publicacoes WHERE id=%s FOR UPDATE''',(pid,))
        r=cur.fetchone()
        if not r: raise HTTPException(404,'Publicação não encontrada')
        tipo,publico,status=r
        if tipo=='EVENTO':
            cur.execute('UPDATE intranet_publicacoes SET exige_ciencia=FALSE WHERE id=%s',(pid,))
        if status!='RASCUNHO': raise HTTPException(409,'Publicação já processada')
        cur.execute('SELECT valor FROM intranet_publicacoes_alvos WHERE publicacao_id=%s',(pid,))
        alvos=[x[0] for x in cur.fetchall()]
        if publico!='TODOS' and not alvos: raise HTTPException(400,'Público sem seleção')
        filtro='TRUE'; params=[]
        if publico=='CARGO': filtro='u.cargo = ANY(%s)';params=[alvos]
        elif publico=='DEPARTAMENTO': filtro='u.departamento = ANY(%s)';params=[alvos]
        elif publico=='USUARIO': filtro='u.id::text = ANY(%s)';params=[alvos]
        cur.execute(f'''INSERT INTO intranet_publicacoes_destinatarios(publicacao_id,usuario_id)
          SELECT %s,u.id::text FROM intranet_usuarios u
          WHERE u.ativo=TRUE AND u.data_demissao IS NULL AND {filtro}
          ON CONFLICT DO NOTHING''',[pid]+params)
        cur.execute("UPDATE intranet_publicacoes SET status='PUBLICADA',publicado_em=now(),atualizado_em=now() WHERE id=%s",(pid,))
        # Gera links individuais na mesma transação da publicação.
        cur.execute("SELECT enviar_whatsapp,fim FROM intranet_publicacoes WHERE id=%s",(pid,))
        wa_habilitado,fim_aviso=cur.fetchone()
        if tipo=='AVISO' and wa_habilitado:
            expira=datetime.combine(fim_aviso,time(23,59,59),tzinfo=ZoneInfo('America/Sao_Paulo'))
            if expira <= datetime.now(timezone.utc):
                raise HTTPException(409,'Aviso já encerrado: não é possível gerar links')
            cur.execute("""SELECT usuario_id FROM intranet_publicacoes_destinatarios
                WHERE publicacao_id=%s AND confirmado_em IS NULL""",(pid,))
            for (uid,) in cur.fetchall():
                token=secrets.token_urlsafe(32)
                cur.execute("""INSERT INTO intranet_avisos_link_teste
                    (publicacao_id,usuario_id,token_hash,expira_em,token_caminho)
                    VALUES(%s,%s,%s,%s,%s)""",
                    (pid,uid,hashlib.sha256(token.encode('ascii')).hexdigest(),expira,'/teste-aviso/'+token))
        cur.execute('SELECT COUNT(*) FROM intranet_publicacoes_destinatarios WHERE publicacao_id=%s',(pid,))
        total=cur.fetchone()[0]
    return {'id':pid,'status':'PUBLICADA','destinatarios':total}

@router_eventos_avisos.post('/admin/{pid}/cancelar')
def cancelar(pid:int,authorization:Optional[str]=Header(None)):
    usuario_atual(authorization,True)
    with _conectar() as conn,conn.cursor() as cur:
        cur.execute("UPDATE intranet_publicacoes SET status='CANCELADA',atualizado_em=now() WHERE id=%s AND status<>'CANCELADA' RETURNING id",(pid,))
        if not cur.fetchone(): raise HTTPException(404,'Publicação não encontrada ou cancelada')
    return {'id':pid,'status':'CANCELADA'}

@router_eventos_avisos.get('/meus')
def meus(authorization:Optional[str]=Header(None),mes:Optional[str]=None):
    u=usuario_atual(authorization)
    if mes is not None and not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',mes):
        raise HTTPException(400,'Mês inválido: use AAAA-MM')
    ano,numero_mes=map(int,mes.split('-')) if mes else (date.today().year,date.today().month)
    primeiro=date(ano,numero_mes,1)
    ultimo=date(ano,numero_mes,calendar.monthrange(ano,numero_mes)[1])
    with _conectar() as conn,conn.cursor() as cur:
        cur.execute('''SELECT p.id,p.tipo,p.titulo,p.descricao,p.inicio,p.fim,p.exige_ciencia,
          d.visualizado_em,d.confirmado_em
          FROM intranet_publicacoes p JOIN intranet_publicacoes_destinatarios d ON d.publicacao_id=p.id
          WHERE d.usuario_id=%s AND p.status='PUBLICADA'
            AND ((p.tipo='AVISO' AND p.inicio<=CURRENT_DATE AND p.fim>=CURRENT_DATE)
                 OR (p.tipo='EVENTO' AND p.inicio<=%s AND p.fim>=%s))
          ORDER BY p.inicio,p.id''',(str(u[0]),ultimo,primeiro))
        rows=cur.fetchall()
    return [{'id':r[0],'tipo':r[1],'titulo':r[2],'descricao':r[3],
             'inicio':r[4],'fim':r[5],'exige_ciencia':r[6],
             'visualizado_em':r[7],'confirmado_em':r[8],
             'pendente':r[1]=='AVISO' and (r[8] is None if r[6] else r[7] is None)} for r in rows]

@router_eventos_avisos.post('/{pid}/visualizar')
def visualizar(pid:int,authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn,conn.cursor() as cur:
        cur.execute('''UPDATE intranet_publicacoes_destinatarios d SET visualizado_via=CASE WHEN d.visualizado_em IS NULL THEN 'INTRANET' ELSE d.visualizado_via END, visualizado_em=COALESCE(d.visualizado_em,now())
          FROM intranet_publicacoes p WHERE p.id=d.publicacao_id AND p.id=%s AND d.usuario_id=%s
          AND p.status='PUBLICADA' AND p.inicio<=CURRENT_DATE AND p.fim>=CURRENT_DATE
          RETURNING p.exige_ciencia,d.confirmado_em''',(pid,str(u[0])))
        r=cur.fetchone()
        if not r: raise HTTPException(404,'Publicação indisponível')
    return {'visualizado':True,'pendente':r[0] and r[1] is None}

@router_eventos_avisos.post('/{pid}/confirmar')
def confirmar(pid:int,authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn,conn.cursor() as cur:
        cur.execute('''UPDATE intranet_publicacoes_destinatarios d
          SET visualizado_via=CASE WHEN d.visualizado_em IS NULL THEN 'INTRANET' ELSE d.visualizado_via END, visualizado_em=COALESCE(d.visualizado_em,now()), confirmado_via=CASE WHEN d.confirmado_em IS NULL THEN 'INTRANET' ELSE d.confirmado_via END, confirmado_em=COALESCE(d.confirmado_em,now())
          FROM intranet_publicacoes p WHERE p.id=d.publicacao_id AND p.id=%s AND d.usuario_id=%s
          AND p.tipo='AVISO' AND p.exige_ciencia=TRUE AND p.status='PUBLICADA'
          AND p.inicio<=CURRENT_DATE AND p.fim>=CURRENT_DATE RETURNING d.confirmado_em''',(pid,str(u[0])))
        r=cur.fetchone()
        if not r: raise HTTPException(404,'Aviso indisponível para confirmação')
    return {'confirmado':True,'confirmado_em':r[0],'pendente':False}
