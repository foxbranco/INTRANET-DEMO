"""Links individuais restritos para Pesquisas V12. Não executa migrações."""
import hashlib
import html
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
import bcrypt
from fastapi import APIRouter, Header, HTTPException, Form, Cookie, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
from pesquisas_api import _publico_permitido, _enviar_respostas_usuario, Envio

router=APIRouter()
_conectar=None
_autenticar=None

def configurar(conectar, autenticar):
    global _conectar,_autenticar
    _conectar,_autenticar=conectar,autenticar

def _hash(s): return hashlib.sha256(s.encode('utf-8')).hexdigest()

def _admin(auth):
    u=_autenticar(auth)
    if u[5] or u[3]!=3: raise HTTPException(403,'Acesso exclusivo do administrador')
    return u

def _registro(cur,pid):
    cur.execute('SELECT id,titulo,anonima,status,inicio_em,encerramento_em,enviar_whatsapp,exigir_senha FROM pesq_pesquisas WHERE id=%s',(pid,))
    r=cur.fetchone()
    if not r: raise HTTPException(404,'Pesquisa não encontrada')
    return dict(zip(('id','titulo','anonima','status','inicio','fim','whatsapp','senha'),r))

def _elegiveis(cur,pid):
    cur.execute('''SELECT DISTINCT u.id,u.nome FROM intranet_usuarios u WHERE u.ativo=TRUE AND EXISTS(
        SELECT 1 FROM pesq_publico pub WHERE pub.pesquisa_id=%s AND (
        pub.tipo='TODOS' OR (pub.tipo='DEPARTAMENTO' AND UPPER(TRIM(pub.valor))=UPPER(TRIM(u.departamento)))
        OR (pub.tipo='CENTRO_CUSTO' AND UPPER(TRIM(pub.valor))=UPPER(TRIM(u.centro_custo)))
        OR (pub.tipo='CARGO' AND UPPER(TRIM(pub.valor))=UPPER(TRIM(u.cargo))))) ORDER BY u.nome,u.id''',(pid,))
    return cur.fetchall()

class Config(BaseModel):
    enviar_whatsapp:bool

@router.get('/api/pesquisas/{pid}/whatsapp')
def consultar(pid:int,authorization:Optional[str]=Header(None)):
    _admin(authorization)
    with _conectar() as conn,conn.cursor() as cur:
        p=_registro(cur,pid)
        pessoas=_elegiveis(cur,pid)
        rows=[]
        for uid,nome in pessoas:
            cur.execute('''SELECT token_caminho,expira_em FROM intranet_links_acesso WHERE modulo='PESQUISA' AND recurso_id=%s AND usuario_id=%s AND revogado_em IS NULL AND expira_em>NOW() ORDER BY id DESC LIMIT 1''',(pid,uid))
            link=cur.fetchone()
            respondido=False
            if not p['anonima']:
                cur.execute('SELECT EXISTS(SELECT 1 FROM pesq_participacao WHERE pesquisa_id=%s AND usuario_id=%s)',(pid,uid))
                respondido=cur.fetchone()[0]
            rows.append({'usuario_id':uid,'nome':nome,'caminho':link[0] if link else None,'expira_em':link[1] if link else None,'respondido':respondido})
    return {'config':{'enviar_whatsapp':p['whatsapp'],'exigir_senha':True},'anonima':p['anonima'],'destinatarios':rows}

@router.put('/api/pesquisas/{pid}/whatsapp/config')
def config(pid:int,dados:Config,authorization:Optional[str]=Header(None)):
    _admin(authorization)
    with _conectar() as conn,conn.cursor() as cur:
        p=_registro(cur,pid)
        if p['status'] not in ('RASCUNHO','PUBLICADA','AGENDADA'):
            raise HTTPException(409,'Pesquisa encerrada')
        cur.execute('UPDATE pesq_pesquisas SET enviar_whatsapp=%s,exigir_senha=TRUE,atualizado_em=NOW() WHERE id=%s',(dados.enviar_whatsapp,pid))
        if not dados.enviar_whatsapp:
            cur.execute("UPDATE intranet_links_acesso SET revogado_em=NOW() WHERE modulo='PESQUISA' AND recurso_id=%s AND revogado_em IS NULL",(pid,))
    return {'ok':True}

@router.post('/api/pesquisas/{pid}/whatsapp/gerar')
def gerar(pid:int,authorization:Optional[str]=Header(None)):
    _admin(authorization)
    with _conectar() as conn,conn.cursor() as cur:
        cur.execute('SELECT id FROM pesq_pesquisas WHERE id=%s FOR UPDATE',(pid,))
        p=_registro(cur,pid)
        if not p['whatsapp'] or not p['senha'] or p['status'] not in ('PUBLICADA','AGENDADA'):
            raise HTTPException(409,'Pesquisa não habilitada para links')
        agora=datetime.now(timezone.utc)
        if p['fim'] and p['fim']<=agora: raise HTTPException(409,'Pesquisa encerrada')
        fim=p['fim'] or max(agora,p['inicio'] or agora)+timedelta(days=30)
        gerados=0
        for uid,nome in _elegiveis(cur,pid):
            cur.execute('SELECT 1 FROM pesq_participacao WHERE pesquisa_id=%s AND usuario_id=%s',(pid,uid))
            if cur.fetchone(): continue
            cur.execute("SELECT 1 FROM intranet_links_acesso WHERE modulo='PESQUISA' AND recurso_id=%s AND usuario_id=%s AND revogado_em IS NULL AND expira_em>NOW() LIMIT 1",(pid,uid))
            if cur.fetchone():continue
            token=secrets.token_urlsafe(32)
            cur.execute('''INSERT INTO intranet_links_acesso(modulo,recurso_id,usuario_id,token_hash,token_caminho,expira_em)
                VALUES('PESQUISA',%s,%s,%s,%s,%s)''',(pid,uid,_hash(token),'/pesquisa-link/'+token,fim))
            gerados+=1
    return {'ok':True,'gerados':gerados}

@router.post('/api/pesquisas/{pid}/whatsapp/gerar/{uid}')
def gerar_individual(pid:int,uid:int,authorization:Optional[str]=Header(None)):
    _admin(authorization)
    with _conectar() as conn,conn.cursor() as cur:
        cur.execute('SELECT id FROM pesq_pesquisas WHERE id=%s FOR UPDATE',(pid,))
        p=_registro(cur,pid)
        if not p['whatsapp'] or not p['senha'] or p['status'] not in ('PUBLICADA','AGENDADA'):
            raise HTTPException(409,'Pesquisa não habilitada para links')
        agora=datetime.now(timezone.utc)
        if p['fim'] and p['fim']<=agora:raise HTTPException(409,'Pesquisa encerrada')
        if not _publico_permitido(cur,pid,uid):raise HTTPException(403,'Funcionário fora do público')
        cur.execute('SELECT 1 FROM pesq_participacao WHERE pesquisa_id=%s AND usuario_id=%s',(pid,uid))
        if cur.fetchone():raise HTTPException(409,'Pesquisa já respondida')
        cur.execute("UPDATE intranet_links_acesso SET revogado_em=NOW() WHERE modulo='PESQUISA' AND recurso_id=%s AND usuario_id=%s AND revogado_em IS NULL",(pid,uid))
        token=secrets.token_urlsafe(32)
        fim=p['fim'] or max(agora,p['inicio'] or agora)+timedelta(days=30)
        cur.execute("INSERT INTO intranet_links_acesso(modulo,recurso_id,usuario_id,token_hash,token_caminho,expira_em) VALUES('PESQUISA',%s,%s,%s,%s,%s)",(pid,uid,_hash(token),'/pesquisa-link/'+token,fim))
    return {'ok':True}

CSS='''<style>body{background:#f3f6fb;color:#17314d;font:15px system-ui,Arial;margin:0;padding:20px}main{max-width:680px;background:#fff;border-radius:14px;margin:5vh auto;padding:26px;box-shadow:0 6px 24px #17314d15}h1{font-size:22px}input,textarea,button{font:inherit;padding:11px;border-radius:8px;border:1px solid #ccd7e4;box-sizing:border-box}input[type=password],textarea{width:100%;margin:7px 0 14px}button{background:#17663b;color:#fff;border:0;cursor:pointer}label{display:block;margin:9px 0}fieldset{border:1px solid #e1e7ef;border-radius:9px;margin:16px 0;padding:14px}small{color:#58718d}.erro{color:#b42318}</style>'''

def _pagina(titulo,corpo):
    return HTMLResponse('<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>Pesquisa</title>'+CSS+'</head><body><main><h1>'+html.escape(titulo)+'</h1>'+corpo+'</main></body></html>',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})

def _validar_token(token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,100}',token):raise HTTPException(404,'Link inválido')

def _link(cur,token,lock=False):
    cur.execute('''SELECT l.id,l.recurso_id,l.usuario_id,u.nome,u.cpf,u.senha_hash,u.trocar_senha,p.titulo,p.descricao
        FROM intranet_links_acesso l JOIN intranet_usuarios u ON u.id=l.usuario_id AND u.ativo=TRUE
        JOIN pesq_pesquisas p ON p.id=l.recurso_id
        WHERE l.modulo='PESQUISA' AND l.token_hash=%s AND l.revogado_em IS NULL AND l.respondido_em IS NULL AND l.expira_em>NOW()
        AND p.enviar_whatsapp=TRUE AND p.exigir_senha=TRUE AND p.status='PUBLICADA'
        AND (p.inicio_em IS NULL OR p.inicio_em<=NOW()) AND (p.encerramento_em IS NULL OR p.encerramento_em>NOW())'''+(' FOR UPDATE OF l' if lock else ''),(_hash(token),))
    r=cur.fetchone()
    if not r:return None
    if not _publico_permitido(cur,r[1],r[2]):return None
    cur.execute('SELECT 1 FROM pesq_participacao WHERE pesquisa_id=%s AND usuario_id=%s',(r[1],r[2]))
    return None if cur.fetchone() else r

def _autorizado(cur,link_id,cookie):
    if not cookie or len(cookie)>200:return False
    cur.execute('''SELECT 1 FROM intranet_links_autorizacoes WHERE link_id=%s AND segredo_hash=%s AND revogado_em IS NULL AND expira_em>NOW()''',(link_id,_hash(cookie)))
    return cur.fetchone() is not None

def _senha_form(token,nome,erro=''):
    caminho='/pesquisa-link/'+html.escape(token,quote=True)
    return _pagina('Acesso à pesquisa','<p>Olá, <strong>'+html.escape(nome)+'</strong>. Informe sua senha da Intranet.</p>'+('<p class="erro">'+html.escape(erro)+'</p>' if erro else '')+'<form method="post" action="'+caminho+'/entrar"><input type="password" name="senha" required maxlength="256" autocomplete="current-password"><button>Entrar</button></form>')

def _troca_form(token,nome,erro=''):
    caminho='/pesquisa-link/'+html.escape(token,quote=True)
    return _pagina('Troca obrigatória de senha','<p>Olá, '+html.escape(nome)+'. Defina uma senha diferente do CPF.</p>'+('<p class="erro">'+html.escape(erro)+'</p>' if erro else '')+'<form method="post" action="'+caminho+'/trocar-senha"><label>Senha atual<input type="password" name="senha_atual" required maxlength="256"></label><label>Nova senha<input type="password" name="nova_senha" minlength="8" maxlength="256" required></label><button>Alterar senha</button></form>')

def _conceder(cur,link_id,token):
    segredo=secrets.token_urlsafe(32)
    cur.execute("UPDATE intranet_links_autorizacoes SET revogado_em=NOW() WHERE link_id=%s AND revogado_em IS NULL",(link_id,))
    cur.execute("INSERT INTO intranet_links_autorizacoes(link_id,segredo_hash,expira_em) VALUES(%s,%s,NOW()+INTERVAL '15 minutes')",(link_id,_hash(segredo)))
    resposta=RedirectResponse('/pesquisa-link/'+token,status_code=303)
    resposta.set_cookie('acesso_pesquisa',segredo,httponly=True,secure=True,samesite='lax',max_age=900,path='/pesquisa-link/'+token)
    return resposta

@router.post('/pesquisa-link/{token}/entrar',include_in_schema=False)
def entrar(token:str,senha:str=Form(...)):
    _validar_token(token)
    if len(senha)>256:raise HTTPException(400,'Senha inválida')
    with _conectar() as conn,conn.cursor() as cur:
        r=_link(cur,token,True)
        if not r:return _pagina('Link indisponível','<p>Link expirado, utilizado ou pesquisa indisponível.</p>')
        if not r[5] or not bcrypt.checkpw(senha.encode(),r[5].encode()):return _senha_form(token,r[3],'Senha incorreta.')
        if r[6]:return _troca_form(token,r[3])
        return _conceder(cur,r[0],token)

@router.post('/pesquisa-link/{token}/trocar-senha',include_in_schema=False)
def trocar(token:str,senha_atual:str=Form(...),nova_senha:str=Form(...)):
    _validar_token(token)
    if len(senha_atual)>256 or len(nova_senha)>256:raise HTTPException(400,'Dados inválidos')
    with _conectar() as conn,conn.cursor() as cur:
        r=_link(cur,token,True)
        if not r:return _pagina('Link indisponível','<p>Link indisponível.</p>')
        if not r[6]:return _senha_form(token,r[3],'Senha já alterada. Entre novamente.')
        cur.execute('SELECT senha_hash FROM intranet_usuarios WHERE id=%s FOR UPDATE',(r[2],))
        atual=cur.fetchone()[0]
        if not atual or not bcrypt.checkpw(senha_atual.encode(),atual.encode()):return _troca_form(token,r[3],'Senha atual incorreta.')
        if len(nova_senha)<8 or nova_senha==r[4]:return _troca_form(token,r[3],'A nova senha precisa ter 8 caracteres ou mais e não pode ser o CPF.')
        novo=bcrypt.hashpw(nova_senha.encode(),bcrypt.gensalt(rounds=12)).decode()
        cur.execute('UPDATE intranet_usuarios SET senha_hash=%s,trocar_senha=FALSE,atualizado_em=NOW() WHERE id=%s',(novo,r[2]))
        cur.execute('UPDATE intranet_sessoes SET revogado_em=NOW() WHERE usuario_id=%s AND revogado_em IS NULL',(r[2],))
        return _conceder(cur,r[0],token)

@router.get('/pesquisa-link/{token}',include_in_schema=False)
def abrir(token:str,acesso_pesquisa:Optional[str]=Cookie(None)):
    _validar_token(token)
    with _conectar() as conn,conn.cursor() as cur:
        r=_link(cur,token)
        if not r:return _pagina('Link indisponível','<p>Esta pesquisa não está disponível ou já foi respondida.</p>')
        if not _autorizado(cur,r[0],acesso_pesquisa):return _senha_form(token,r[3])
        cur.execute('SELECT id,enunciado,tipo,obrigatoria FROM pesq_perguntas WHERE pesquisa_id=%s ORDER BY ordem,id',(r[1],))
        perguntas=[]
        for qid,enunciado,tipo,obrigatoria in cur.fetchall():
            cur.execute('SELECT id,texto FROM pesq_alternativas WHERE pergunta_id=%s ORDER BY ordem',(qid,))
            perguntas.append({'id':qid,'enunciado':enunciado,'tipo':tipo,'obrigatoria':obrigatoria,'alternativas':[{'id':a,'texto':t} for a,t in cur.fetchall()]})
    if not perguntas:return _pagina('Pesquisa indisponível','<p>Não há perguntas.</p>')
    corpo='<p>Olá, <strong>'+html.escape(r[3])+'</strong>.</p><p>'+html.escape(r[8] or '')+'</p><form id="pesquisa"><div id="perguntas"></div><button type="submit">Enviar respostas</button><p id="estado" role="status"></p></form>'
    script='''<script>const perguntas=__DADOS__;const token=__TOKEN__;const raiz=document.getElementById('perguntas');const e=s=>{const x=document.createElement('span');x.textContent=s;return x.innerHTML};
    raiz.innerHTML=perguntas.map((p,i)=>{const n='p'+p.id;let c='';if(p.tipo==='TEXTO')c=`<textarea name="${n}" maxlength="5000"></textarea>`;
    else if(p.tipo==='NOTA_1_5'||p.tipo==='NOTA_0_10'){let a=p.tipo==='NOTA_1_5'?1:0;c=Array.from({length:a===0?11:5},(_,j)=>j+a).map(v=>`<label><input type="radio" name="${n}" value="${v}"> ${v}</label>`).join('');}
    else if(p.tipo==='SIM_NAO')c=`<label><input type="radio" name="${n}" value="true"> Sim</label><label><input type="radio" name="${n}" value="false"> Não</label>`;
    else c=p.alternativas.map(a=>`<label><input type="${p.tipo==='MULTIPLA_ESCOLHA'?'checkbox':'radio'}" name="${n}" value="${a.id}"> ${e(a.texto)}</label>`).join('');
    return `<fieldset><legend>${i+1}. ${e(p.enunciado)} ${p.obrigatoria?'*':''}</legend>${c}</fieldset>`;}).join('');
    document.getElementById('pesquisa').addEventListener('submit',async ev=>{ev.preventDefault();const b=ev.target.querySelector('button');b.disabled=true;const estado=document.getElementById('estado');try{const respostas=[];for(const p of perguntas){const n='p'+p.id;const marcados=[...document.querySelectorAll('[name="'+n+'"]:checked')].map(x=>x.value);const texto=document.querySelector('textarea[name="'+n+'"]');if(!marcados.length&&!texto?.value){if(p.obrigatoria)throw Error('Responda todas as perguntas obrigatórias.');continue;}let valor;if(p.tipo==='TEXTO')valor=texto.value;else if(p.tipo==='MULTIPLA_ESCOLHA')valor=marcados.map(Number);else if(p.tipo==='SIM_NAO')valor=marcados[0]==='true';else valor=Number(marcados[0]);respostas.push({pergunta_id:p.id,valor});}const resp=await fetch(location.pathname+'/enviar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({respostas})});const data=await resp.json();if(!resp.ok)throw Error(data.detail||'Erro ao enviar');document.querySelector('main').innerHTML='<h1>Obrigado!</h1><p>Suas respostas foram registradas. O acesso foi encerrado. Você já pode fechar esta janela.</p>';}catch(err){estado.textContent=err.message;b.disabled=false;}});</script>'''
    script=script.replace('__DADOS__',json.dumps(perguntas,ensure_ascii=False).replace('<','\\u003c')).replace('__TOKEN__',json.dumps(token))
    return _pagina(r[7],corpo+script)

@router.post('/pesquisa-link/{token}/enviar',include_in_schema=False)
def enviar(token:str,dados:Envio,acesso_pesquisa:Optional[str]=Cookie(None)):
    _validar_token(token)
    with _conectar() as conn,conn.cursor() as cur:
        r=_link(cur,token,True)
        if not r:raise HTTPException(409,'Link indisponível ou já utilizado')
        if not _autorizado(cur,r[0],acesso_pesquisa):raise HTTPException(401,'Autenticação necessária')
        # A rotina existente valida respostas e registra participação, inclusive anônimas.
        # A trava do link permanece ativa durante a operação para evitar dois envios simultâneos.
        _enviar_respostas_usuario(r[1],dados,r[2])
        cur.execute('UPDATE intranet_links_acesso SET respondido_em=NOW(),revogado_em=NOW() WHERE id=%s',(r[0],))
        cur.execute('UPDATE intranet_links_autorizacoes SET revogado_em=NOW() WHERE link_id=%s AND revogado_em IS NULL',(r[0],))
    resposta=JSONResponse({'ok':True,'mensagem':'Obrigado! Respostas registradas.'})
    resposta.delete_cookie('acesso_pesquisa',path='/pesquisa-link/'+token)
    return resposta
