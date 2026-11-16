"""Megavig Pulse V2: catalogo e rascunhos persistidos; SEM disparos automáticos.
Integrar no app principal: app.include_router(router_pulse); configurar_pulse(conectar, autenticar).
"""
from typing import Optional
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

router_pulse = APIRouter(prefix='/api/pulse', tags=['Megavig Pulse'])
_conectar = _autenticar = None

def configurar_pulse(conectar, autenticar):
    global _conectar, _autenticar
    _conectar, _autenticar = conectar, autenticar

def usuario_atual(authorization):
    if not _autenticar or not _conectar: raise HTTPException(503,'Pulse não configurado')
    u=_autenticar(authorization)
    if not u: raise HTTPException(401,'Sessão inválida')
    if u[5]: raise HTTPException(403,'Troca de senha obrigatória')
    if int(u[3]) not in (2,3): raise HTTPException(403,'Pulse restrito aos níveis 2 e 3')
    return u

def dashboards_autorizados(cur, uid):
    cur.execute('''SELECT DISTINCT d.id,d.nome FROM intranet_dash_vinculos v
      JOIN intranet_dash_caixinhas c ON c.id=v.caixinha_id AND c.ativo=TRUE
      JOIN intranet_dash_dashboards d ON d.id=v.dashboard_id AND d.ativo=TRUE
      JOIN intranet_usuarios u ON u.id=%s AND u.ativo=TRUE
      WHERE EXISTS(SELECT 1 FROM intranet_dash_permissoes_vinculos p WHERE p.vinculo_id=v.id AND
        ((p.modalidade='USUARIO' AND p.usuario_id=u.id) OR
         (p.modalidade='CARGO' AND p.cargo=u.cargo) OR
         (p.modalidade='DEPARTAMENTO' AND p.departamento=u.departamento)))
      ORDER BY d.nome,d.id''',(uid,))
    return [{'codigo':'DASHBOARD:'+str(i),'tipo':'DASHBOARD','nome':nome} for i,nome in cur.fetchall()]

def catalogo_para(cur,u):
    # Fase 1: só dashboards com autorização comprovada. Outros módulos serão
    # registrados no catálogo quando as respectivas verificações de escopo existirem.
    return dashboards_autorizados(cur,int(u[0]))

@router_pulse.get('/catalogo')
def catalogo(authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur: itens=catalogo_para(cur,u)
    return {'itens':itens,'outros_modulos':'pendentes de integração de permissões'}

@router_pulse.get('/automacoes')
def listar(authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur:
            catalogo_disponivel=catalogo_para(cur,u)
            permitidos={x['codigo'] for x in catalogo_disponivel}
            nomes={x['codigo']:x['nome'] for x in catalogo_disponivel}
            cur.execute('SELECT id,codigo,frequencia,horario,canal,status FROM pulse_automacoes WHERE usuario_id=%s ORDER BY id DESC',(u[0],))
            linhas=cur.fetchall()
    return {'itens':[{'id':r[0],'codigo':r[1],'nome':nomes.get(r[1],r[1]),'tipo':r[1].split(':',1)[0],'frequencia':r[2], 'horario':str(r[3])[:5] if r[3] else None,'canal':r[4],'status':r[5]} for r in linhas if r[1] in permitidos]}

class AutomacaoNova(BaseModel):
    codigo:str
    frequencia:str
    horario:str
    canal:str

@router_pulse.post('/automacoes',status_code=201)
def criar(d:AutomacaoNova,authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    if d.frequencia not in ('DIARIO','UTEIS','SEMANAL','MENSAL','EVENTO'):raise HTTPException(422,'Frequência inválida')
    if d.canal not in ('EMAIL','INTRANET'):raise HTTPException(422,'Canal inválido')
    import re
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',d.horario):raise HTTPException(422,'Horário inválido')
    with _conectar() as conn:
        with conn.cursor() as cur:
            permitido={x['codigo']:x for x in catalogo_para(cur,u)}
            if d.codigo not in permitido:raise HTTPException(403,'Relatório não autorizado')
            # Códigos dinâmicos de dashboard são registrados no catálogo antes do vínculo.
            x=permitido[d.codigo]
            cur.execute('INSERT INTO pulse_catalogo(codigo,tipo,nome) VALUES(%s,%s,%s) ON CONFLICT(codigo) DO UPDATE SET nome=EXCLUDED.nome',(d.codigo,x['tipo'],x['nome']))
            cur.execute('''INSERT INTO pulse_automacoes(usuario_id,codigo,frequencia,horario,canal,status)
                VALUES(%s,%s,%s,%s,%s,'RASCUNHO') RETURNING id''',(u[0],d.codigo,d.frequencia,None if d.frequencia=='EVENTO' else d.horario,d.canal))
            novo=cur.fetchone()[0]
        conn.commit()
    return {'id':novo,'status':'RASCUNHO','mensagem':'Configuração salva; envios automáticos ainda não ativados'}

@router_pulse.put('/automacoes/{id}')
def editar(id:int,d:AutomacaoNova,authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    import re
    if d.frequencia not in ('DIARIO','UTEIS','SEMANAL','MENSAL','EVENTO'):
        raise HTTPException(422,'Frequência inválida')
    if d.canal not in ('EMAIL','INTRANET'):
        raise HTTPException(422,'Canal inválido')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',d.horario):
        raise HTTPException(422,'Horário inválido')
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM pulse_automacoes WHERE id=%s AND usuario_id=%s FOR UPDATE',(id,u[0]))
            anterior=cur.fetchone()
            if not anterior: raise HTTPException(404,'Agendamento não encontrado')
            permitido={x['codigo']:x for x in catalogo_para(cur,u)}
            if d.codigo not in permitido: raise HTTPException(403,'Relatório não autorizado')
            x=permitido[d.codigo]
            cur.execute('INSERT INTO pulse_catalogo(codigo,tipo,nome) VALUES(%s,%s,%s) ON CONFLICT(codigo) DO UPDATE SET nome=EXCLUDED.nome',(d.codigo,x['tipo'],x['nome']))
            # A edição pausa a automação: o usuário deve revisar e reativar.
            cur.execute("""UPDATE pulse_automacoes SET codigo=%s,frequencia=%s,horario=%s,canal=%s,
                status='PAUSADA',atualizado_em=now() WHERE id=%s AND usuario_id=%s""",
                (d.codigo,d.frequencia,None if d.frequencia=='EVENTO' else d.horario,d.canal,id,u[0]))
    return {'id':id,'status':'PAUSADA','mensagem':'Alterações salvas. Clique em Ativar para retomar os envios.'}

@router_pulse.delete('/automacoes/{id}',status_code=200)
def excluir(id:int,authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM pulse_automacoes WHERE id=%s AND usuario_id=%s AND status=%s AND NOT EXISTS(SELECT 1 FROM pulse_execucoes e WHERE e.automacao_id=pulse_automacoes.id) RETURNING id',(id,u[0],'RASCUNHO'))
            if not cur.fetchone():raise HTTPException(404,'Rascunho não encontrado')
        conn.commit()
    return {'ok':True}


@router_pulse.post('/automacoes/{id}/ativar')
def ativar(id:int, authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT codigo,frequencia,canal,status FROM pulse_automacoes WHERE id=%s AND usuario_id=%s FOR UPDATE",(id,u[0]))
            r=cur.fetchone()
            if not r: raise HTTPException(404,'Agendamento não encontrado')
            codigo,frequencia,canal,status=r
            if status not in ('RASCUNHO','PAUSADA'): raise HTTPException(409,'Agendamento já ativo')
            if canal!='EMAIL' or frequencia=='EVENTO': raise HTTPException(422,'Ativação disponível apenas para e-mail com horário')
            permitido={x['codigo'] for x in catalogo_para(cur,u)}
            if codigo not in permitido: raise HTTPException(403,'Dashboard não autorizado')
            from pulse_agendador import rota_dashboard, validar_dashboard
            rota=rota_dashboard(cur,codigo)
            try:
                validar_dashboard(rota)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            cur.execute("SELECT email FROM intranet_usuarios WHERE id=%s AND ativo=TRUE",(u[0],))
            email=cur.fetchone()
            if not email or not email[0] or '@' not in email[0]: raise HTTPException(422,'Cadastre seu e-mail no perfil')
            cur.execute("UPDATE pulse_automacoes SET status='ATIVA',atualizado_em=now() WHERE id=%s",(id,))
    return {'status':'ATIVA','mensagem':'Agendamento ativado'}

@router_pulse.post('/automacoes/{id}/pausar')
def pausar(id:int,authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE pulse_automacoes SET status='PAUSADA',atualizado_em=now() WHERE id=%s AND usuario_id=%s AND status='ATIVA' RETURNING id",(id,u[0]))
            if not cur.fetchone(): raise HTTPException(404,'Agendamento ativo não encontrado')
    return {'status':'PAUSADA'}

@router_pulse.get('/execucoes')
def historico(authorization:Optional[str]=Header(None)):
    u=usuario_atual(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT e.id,e.automacao_id,e.status,e.iniciada_em,e.concluida_em,e.erro
              FROM pulse_execucoes e JOIN pulse_automacoes a ON a.id=e.automacao_id
              WHERE a.usuario_id=%s ORDER BY e.id DESC LIMIT 30""",(u[0],))
            rows=cur.fetchall()
    return {'itens':[{'id':r[0],'automacao_id':r[1],'status':r[2], 'inicio':r[3], 'fim':r[4], 'erro':r[5]} for r in rows]}
