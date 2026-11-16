"""Piloto: link temporario para ciencia em publicacao existente, sem login do destinatario."""
import hashlib
import base64
import mimetypes
import re
from pathlib import Path
import html
import secrets
import bcrypt
from fastapi import Form, Cookie
from fastapi.responses import RedirectResponse
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

router = APIRouter()
_conectar = None
_exigir_nivel = None


def configurar(conectar, exigir_nivel):
    global _conectar, _exigir_nivel
    _conectar, _exigir_nivel = conectar, exigir_nivel
    return router


class GerarLink(BaseModel):
    publicacao_id: int = Field(gt=0)
    usuario_id: int = Field(gt=0)



def _expiracao(fim):
    if fim is None:
        raise HTTPException(409, 'Aviso sem data de término')
    dia = fim.date() if isinstance(fim, datetime) else fim
    return datetime.combine(dia, time(23, 59, 59), tzinfo=ZoneInfo('America/Sao_Paulo'))


def _inserir_link(cur, pub_id, uid, fim, substituir=False):
    expira = _expiracao(fim)
    if expira <= datetime.now(timezone.utc):
        raise HTTPException(409, 'Aviso já encerrado')
    cur.execute('''SELECT id,token_caminho FROM intranet_avisos_link_teste
        WHERE publicacao_id=%s AND usuario_id=%s AND revogado_em IS NULL
        AND expira_em>NOW() ORDER BY id DESC LIMIT 1''', (pub_id,str(uid)))
    existente=cur.fetchone()
    if existente and not substituir and existente[1]:
        return existente[1]
    if existente or substituir:
        cur.execute('''UPDATE intranet_avisos_link_teste SET revogado_em=NOW()
            WHERE publicacao_id=%s AND usuario_id=%s AND revogado_em IS NULL''',(pub_id,str(uid)))
    token=secrets.token_urlsafe(32)
    caminho='/teste-aviso/'+token
    cur.execute('''INSERT INTO intranet_avisos_link_teste
        (publicacao_id,usuario_id,token_hash,expira_em,token_caminho)
        VALUES (%s,%s,%s,%s,%s)''',(pub_id,str(uid),_hash(token),expira,caminho))
    return caminho


def _hash(token):
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def _criar_tabela(cur):
    cur.execute('''CREATE TABLE IF NOT EXISTS intranet_avisos_link_teste (
        id BIGSERIAL PRIMARY KEY,
        publicacao_id BIGINT NOT NULL REFERENCES intranet_publicacoes(id),
        usuario_id TEXT NOT NULL,
        token_hash CHAR(64) NOT NULL UNIQUE,
        criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expira_em TIMESTAMPTZ NOT NULL,
        utilizado_em TIMESTAMPTZ,
        revogado_em TIMESTAMPTZ
    )''')
    cur.execute("ALTER TABLE intranet_publicacoes ADD COLUMN IF NOT EXISTS enviar_whatsapp BOOLEAN NOT NULL DEFAULT FALSE")
    cur.execute("ALTER TABLE intranet_publicacoes ADD COLUMN IF NOT EXISTS exigir_senha BOOLEAN NOT NULL DEFAULT FALSE")
    cur.execute("ALTER TABLE intranet_publicacoes ADD COLUMN IF NOT EXISTS exigir_facial BOOLEAN NOT NULL DEFAULT FALSE")
    cur.execute("ALTER TABLE intranet_avisos_link_teste ADD COLUMN IF NOT EXISTS token_caminho TEXT")
    cur.execute("""CREATE TABLE IF NOT EXISTS intranet_avisos_link_autorizacoes (
        id BIGSERIAL PRIMARY KEY, link_id BIGINT NOT NULL REFERENCES intranet_avisos_link_teste(id),
        segredo_hash CHAR(64) NOT NULL UNIQUE, expira_em TIMESTAMPTZ NOT NULL,
        criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW())""")



def _marca_empresa():
    """Consulta a marca pública da empresa no cadastro existente, sem exigir login."""
    nome = 'Empresa'
    logo_html = ''
    try:
        with _conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COALESCE(NULLIF(nome_curto,''),nome), logo FROM intranet_empresa_config WHERE id=1")
                registro = cur.fetchone()
        if registro:
            nome = registro[0] or nome
            arquivo = registro[1]
            if arquivo and re.fullmatch(r'[a-f0-9]{32}\.(?:jpg|png|webp)', arquivo):
                caminho = Path('/intranet/documentos_upload/empresa_assets') / arquivo
                if caminho.is_file() and caminho.stat().st_size <= 2 * 1024 * 1024:
                    mime = mimetypes.guess_type(arquivo)[0] or 'image/png'
                    dados = base64.b64encode(caminho.read_bytes()).decode('ascii')
                    logo_html = '<img class="empresa-logo" alt="Logotipo da empresa" src="data:' + mime + ';base64,' + dados + '">'
    except Exception:
        pass  # Marca não pode impedir a abertura ou a confirmação de avisos.
    return '<div class="empresa-marca">' + logo_html + '<div class="empresa-nome">' + html.escape(nome) + '</div></div>'


def _pagina(titulo, corpo, botao='', permitir_fechar=False, destinatario=None):
    nonce = secrets.token_urlsafe(16)
    if permitir_fechar:
        botao = (
            '<button type="button" id="fechar-pagina">Fechar</button>'
            '<p id="orientacao-fechar" style="font-size:14px;color:#586477;text-align:center">'
            'Se a janela não fechar, você já pode voltar ao WhatsApp.</p>'
            '<script nonce="' + nonce + '">'
            'document.getElementById("fechar-pagina").addEventListener("click",function(){'
            'window.close();'
            'document.getElementById("orientacao-fechar").textContent='
            '"Confirmação concluída. Você já pode fechar esta janela e voltar ao WhatsApp.";'
            '});</script>'
        )
    saudacao = ('<p class="saudacao">Olá, <strong>' + html.escape(destinatario) + '</strong>!</p>') if destinatario else ''
    return HTMLResponse('''<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Confirmação de leitura</title>
<style>body{font-family:system-ui,Arial;background:#f3f5f9;margin:0;padding:24px;color:#172b4d}
main{max-width:460px;margin:8vh auto;background:white;border-radius:16px;padding:28px;
box-shadow:0 8px 30px #172b4d18}h1{font-size:23px}p{line-height:1.6;white-space:pre-wrap}
.empresa-marca{text-align:center;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid #e4eaf0}.empresa-logo{display:block;max-width:175px;max-height:95px;width:auto;height:auto;object-fit:contain;margin:0 auto 9px}.empresa-nome{font-size:17px;font-weight:800;color:#153e66;overflow-wrap:anywhere}.saudacao{font-size:16px;color:#243f5c}button{width:100%;background:#17663b;color:white;border:0;border-radius:10px;padding:16px;
font-size:17px;font-weight:700;cursor:pointer}</style></head><body><main>''' + _marca_empresa() + '<h1>' + html.escape(titulo) +
'</h1>' + saudacao + '<p>' + html.escape(corpo) + '</p>' + botao + '</main></body></html>',
        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                 'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'; img-src data:; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})


@router.get('/api/teste-aviso/configuracoes')
def listar_configuracoes(authorization: Optional[str] = Header(None)):
    _exigir_nivel(authorization, 3)
    with _conectar() as conn:
        with conn.cursor() as cur:
            _criar_tabela(cur)
            cur.execute('SELECT id FROM intranet_publicacoes WHERE enviar_whatsapp=TRUE')
            ids=[r[0] for r in cur.fetchall()]
    return {'ids':ids}

@router.get('/api/teste-aviso/publicacao/{publicacao_id}/destinatarios')
def listar_destinatarios(publicacao_id: int, authorization: Optional[str] = Header(None)):
    _exigir_nivel(authorization, 3)
    with _conectar() as conn:
        with conn.cursor() as cur:
            _criar_tabela(cur)
            cur.execute("""SELECT d.usuario_id,u.nome,d.visualizado_em,d.confirmado_em,
                   l.token_caminho,l.expira_em,d.visualizado_via,d.confirmado_via
                   FROM intranet_publicacoes_destinatarios d
                   JOIN intranet_usuarios u ON u.id::text=d.usuario_id
                   LEFT JOIN LATERAL (
                      SELECT token_caminho,expira_em FROM intranet_avisos_link_teste
                      WHERE publicacao_id=d.publicacao_id AND usuario_id=d.usuario_id
                        AND revogado_em IS NULL
                      ORDER BY id DESC LIMIT 1
                   ) l ON TRUE
                   WHERE d.publicacao_id=%s
                   ORDER BY u.nome""", (publicacao_id,))
            rows=cur.fetchall()
    return [dict(usuario_id=r[0],nome=r[1],visualizado_em=r[2],confirmado_em=r[3],
                 caminho=r[4],expira_em=r[5],visualizado_via=r[6],confirmado_via=r[7]) for r in rows]

class ConfigWhatsApp(BaseModel):
    enviar_whatsapp: bool = False
    exigir_senha: bool = True
    exigir_facial: bool = False

@router.put('/api/teste-aviso/publicacao/{publicacao_id}/config')
def definir_config(publicacao_id: int, dados: ConfigWhatsApp, authorization: Optional[str] = Header(None)):
    _exigir_nivel(authorization, 3)
    with _conectar() as conn:
        with conn.cursor() as cur:
            _criar_tabela(cur)
            cur.execute("""UPDATE intranet_publicacoes SET enviar_whatsapp=%s, exigir_senha=%s, exigir_facial=FALSE,
                    exige_ciencia=CASE WHEN %s THEN TRUE ELSE exige_ciencia END
                    WHERE id=%s AND tipo='AVISO' AND status='RASCUNHO' RETURNING id""",
                    (dados.enviar_whatsapp,dados.exigir_senha if dados.enviar_whatsapp else False,dados.enviar_whatsapp,publicacao_id))
            if not cur.fetchone(): raise HTTPException(409,'Somente rascunhos de avisos podem ser configurados')
    return {'ok':True}

@router.get('/api/teste-aviso/publicacao/{publicacao_id}/config')
def consultar_config(publicacao_id: int, authorization: Optional[str] = Header(None)):
    _exigir_nivel(authorization, 3)
    with _conectar() as conn:
        with conn.cursor() as cur:
            _criar_tabela(cur)
            cur.execute('SELECT enviar_whatsapp,exigir_senha,exigir_facial FROM intranet_publicacoes WHERE id=%s',(publicacao_id,))
            row=cur.fetchone()
    if not row: raise HTTPException(404,'Publicação não encontrada')
    return {'enviar_whatsapp':row[0], 'exigir_senha':row[1], 'exigir_facial':False}

@router.post('/api/teste-aviso/publicacao/{publicacao_id}/gerar-todos')
def gerar_todos(publicacao_id: int, authorization: Optional[str] = Header(None)):
    _exigir_nivel(authorization, 3)
    with _conectar() as conn:
        with conn.cursor() as cur:
            _criar_tabela(cur)
            cur.execute('''SELECT fim FROM intranet_publicacoes WHERE id=%s
                AND tipo='AVISO' AND status='PUBLICADA' AND exige_ciencia=TRUE
                AND enviar_whatsapp=TRUE FOR UPDATE''',(publicacao_id,))
            r=cur.fetchone()
            if not r: raise HTTPException(409,'Aviso WhatsApp publicado não encontrado')
            cur.execute('''SELECT d.usuario_id FROM intranet_publicacoes_destinatarios d
                JOIN intranet_usuarios u ON u.id::text=d.usuario_id
                WHERE d.publicacao_id=%s AND d.confirmado_em IS NULL AND u.ativo=TRUE''',(publicacao_id,))
            ids=[x[0] for x in cur.fetchall()]
            for uid in ids: _inserir_link(cur,publicacao_id,uid,r[0])
    return {'ok':True,'destinatarios':len(ids)}


@router.post('/api/teste-aviso/gerar')
def gerar_link(dados: GerarLink, authorization: Optional[str] = Header(None)):
    _exigir_nivel(authorization, 3)
    with _conectar() as conn:
        with conn.cursor() as cur:
            _criar_tabela(cur)
            cur.execute('''SELECT p.titulo,u.nome,d.confirmado_em,p.fim
                FROM intranet_publicacoes p
                JOIN intranet_publicacoes_destinatarios d ON d.publicacao_id=p.id
                JOIN intranet_usuarios u ON u.id::text=d.usuario_id
                WHERE p.id=%s AND d.usuario_id=%s AND u.ativo=TRUE
                AND p.tipo='AVISO' AND p.enviar_whatsapp=TRUE
                AND p.status='PUBLICADA' AND p.exige_ciencia=TRUE''',
                (dados.publicacao_id,str(dados.usuario_id)))
            r=cur.fetchone()
            if not r: raise HTTPException(404,'Destinatário ou aviso não encontrado')
            titulo,nome,confirmado,fim=r
            if confirmado: raise HTTPException(409,'Destinatário já confirmou')
            caminho=_inserir_link(cur,dados.publicacao_id,dados.usuario_id,fim,substituir=True)
    return {'publicacao_id':dados.publicacao_id,'usuario_id':dados.usuario_id,
            'usuario':nome,'titulo':titulo,'caminho':caminho}


def _autorizado(cur, token, cookie):
    if not cookie or len(cookie) > 200:
        return False
    cur.execute("""SELECT 1 FROM intranet_avisos_link_autorizacoes a
        JOIN intranet_avisos_link_teste l ON l.id=a.link_id
        WHERE l.token_hash=%s AND a.segredo_hash=%s AND a.expira_em>NOW()
        AND l.revogado_em IS NULL AND l.utilizado_em IS NULL""", (_hash(token), _hash(cookie)))
    return cur.fetchone() is not None


def _form_senha(token, nome, erro=''):
    caminho='/teste-aviso/'+html.escape(token, quote=True)
    conteudo=('Informe somente sua senha da Intranet para visualizar este aviso.'
              + ('\n'+erro if erro else ''))
    botao='<form method="post" action="'+caminho+'/entrar"><input type="password" name="senha" required autocomplete="current-password" placeholder="Senha da Intranet" maxlength="256" style="width:100%;padding:13px;margin-bottom:12px;box-sizing:border-box"><button type="submit">Acessar aviso</button></form>'
    return _pagina('Acesso ao aviso', conteudo, botao, destinatario=nome)


@router.post('/teste-aviso/{token}/entrar', include_in_schema=False)
def entrar_aviso(token: str, senha: str = Form(...)):
    if not (32 <= len(token) <= 100) or len(senha) > 256:
        return _pagina('Link inválido', 'Este link não está disponível.')
    with _conectar() as conn, conn.cursor() as cur:
        _criar_tabela(cur)
        cur.execute("""SELECT l.id,u.nome,u.senha_hash,u.trocar_senha,u.cpf,p.exigir_senha,p.exigir_facial
            FROM intranet_avisos_link_teste l
            JOIN intranet_publicacoes p ON p.id=l.publicacao_id
            JOIN intranet_usuarios u ON u.id::text=l.usuario_id AND u.ativo=TRUE
            JOIN intranet_publicacoes_destinatarios d ON d.publicacao_id=l.publicacao_id AND d.usuario_id=l.usuario_id
            WHERE l.token_hash=%s AND l.revogado_em IS NULL AND l.utilizado_em IS NULL
            AND l.expira_em>NOW() AND p.status='PUBLICADA' AND p.exige_ciencia=TRUE
            AND p.fim>=CURRENT_DATE AND d.confirmado_em IS NULL""", (_hash(token),))
        r=cur.fetchone()
        if not r: return _pagina('Link indisponível','Este link expirou ou não está disponível.')
        link_id,nome,senha_hash,trocar,cpf,exige,facial=r
        if facial: return _pagina('Reconhecimento facial indisponível', 'Este aviso exige validação facial, ainda não homologada. O acesso está bloqueado por segurança.')
        if not exige: return RedirectResponse('/teste-aviso/'+token,status_code=303)
        if not senha_hash or not bcrypt.checkpw(senha.encode(), senha_hash.encode()):
            return _form_senha(token,nome,'Senha incorreta. Tente novamente.')
        if trocar:
            return _form_troca(token,nome)
        segredo=secrets.token_urlsafe(32)
        cur.execute("INSERT INTO intranet_avisos_link_autorizacoes(link_id,segredo_hash,expira_em) VALUES(%s,%s,NOW()+INTERVAL '15 minutes')",(link_id,_hash(segredo)))
    resposta=RedirectResponse('/teste-aviso/'+token,status_code=303)
    resposta.set_cookie('acesso_aviso',segredo,max_age=900,httponly=True,secure=True,samesite='lax',path='/teste-aviso/'+token)
    return resposta


def _form_troca(token,nome,erro=''):
    caminho='/teste-aviso/'+html.escape(token,quote=True)
    botao='<form method="post" action="'+caminho+'/trocar-senha"><input type="password" name="senha_atual" required placeholder="Senha atual" autocomplete="current-password" style="width:100%;padding:12px;box-sizing:border-box"><input type="password" name="nova_senha" required minlength="8" maxlength="256" placeholder="Nova senha (mínimo 8 caracteres)" autocomplete="new-password" style="width:100%;padding:12px;margin:10px 0;box-sizing:border-box"><button type="submit">Alterar senha e acessar</button></form>'
    return _pagina('Troca de senha obrigatória','Cadastre uma nova senha para continuar.'+('\n'+erro if erro else ''),botao,destinatario=nome)


@router.post('/teste-aviso/{token}/trocar-senha',include_in_schema=False)
def trocar_senha_aviso(token:str, senha_atual:str=Form(...), nova_senha:str=Form(...)):
    if not (32<=len(token)<=100) or len(senha_atual)>256 or len(nova_senha)>256:
        return _pagina('Link inválido','Link indisponível.')
    with _conectar() as conn,conn.cursor() as cur:
        _criar_tabela(cur)
        cur.execute("""SELECT l.id,u.id,u.nome,u.cpf,u.senha_hash,u.trocar_senha
          FROM intranet_avisos_link_teste l JOIN intranet_publicacoes p ON p.id=l.publicacao_id
          JOIN intranet_usuarios u ON u.id::text=l.usuario_id AND u.ativo=TRUE
          JOIN intranet_publicacoes_destinatarios d ON d.publicacao_id=l.publicacao_id AND d.usuario_id=l.usuario_id
          WHERE l.token_hash=%s AND l.revogado_em IS NULL AND l.utilizado_em IS NULL AND l.expira_em>NOW()
          AND p.status='PUBLICADA' AND p.exige_ciencia=TRUE AND p.exigir_senha=TRUE
          AND p.fim>=CURRENT_DATE AND d.confirmado_em IS NULL AND p.exigir_facial=FALSE FOR UPDATE OF u""",(_hash(token),))
        r=cur.fetchone()
        if not r: return _pagina('Link indisponível','Link expirado ou já utilizado.')
        link_id,uid,nome,cpf,senha_hash,trocar=r
        if not trocar: return _pagina('Senha já alterada','Volte ao link e informe sua senha.')
        if not senha_hash or not bcrypt.checkpw(senha_atual.encode(),senha_hash.encode()):
            return _form_troca(token,nome,'Senha atual incorreta.')
        if len(nova_senha)<8 or nova_senha==cpf:
            return _form_troca(token,nome,'Nova senha deve ter ao menos 8 caracteres e não pode ser o CPF.')
        novo_hash=bcrypt.hashpw(nova_senha.encode(),bcrypt.gensalt(rounds=12)).decode()
        cur.execute('UPDATE intranet_usuarios SET senha_hash=%s,trocar_senha=FALSE,atualizado_em=NOW() WHERE id=%s',(novo_hash,uid))
        cur.execute('UPDATE intranet_sessoes SET revogado_em=NOW() WHERE usuario_id=%s AND revogado_em IS NULL',(uid,))
        segredo=secrets.token_urlsafe(32)
        cur.execute("INSERT INTO intranet_avisos_link_autorizacoes(link_id,segredo_hash,expira_em) VALUES(%s,%s,NOW()+INTERVAL '15 minutes')",(link_id,_hash(segredo)))
    resposta=RedirectResponse('/teste-aviso/'+token,status_code=303)
    resposta.set_cookie('acesso_aviso',segredo,max_age=900,httponly=True,secure=True,samesite='lax',path='/teste-aviso/'+token)
    return resposta


@router.get('/teste-aviso/{token}', include_in_schema=False)
def abrir_aviso(token: str, acesso_aviso: Optional[str] = Cookie(None)):
    if not (32 <= len(token) <= 100):
        return _pagina('Link inválido', 'Este link não está disponível.')
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute('''SELECT l.publicacao_id,l.usuario_id,l.expira_em,l.utilizado_em,l.revogado_em,
                       p.titulo,p.descricao,p.status,p.exige_ciencia,d.confirmado_em,p.fim,u.nome,p.exigir_senha,p.exigir_facial
                FROM intranet_avisos_link_teste l
                JOIN intranet_publicacoes p ON p.id=l.publicacao_id
                JOIN intranet_publicacoes_destinatarios d
                  ON d.publicacao_id=l.publicacao_id AND d.usuario_id=l.usuario_id
                JOIN intranet_usuarios u ON u.id::text=l.usuario_id AND u.ativo=TRUE
                WHERE l.token_hash=%s''', (_hash(token),))
            r=cur.fetchone()
            if not r:
                return _pagina('Link inválido', 'Este link não está disponível.')
            pub_id,uid,expira,utilizado,revogado,titulo,descricao,status,exige,confirmado,fim,nome,exigir_senha,exigir_facial=r
            if exigir_facial: return _pagina('Reconhecimento facial indisponível', 'Este aviso exige validação facial, ainda não homologada. O acesso está bloqueado por segurança.', destinatario=nome)
            if utilizado or confirmado:
                return _pagina('Aviso já confirmado', 'A confirmação já foi registrada.', permitir_fechar=True, destinatario=nome)
            if revogado or min(expira,_expiracao(fim)) <= datetime.now(timezone.utc) or status!='PUBLICADA' or not exige:
                return _pagina('Link indisponível', 'Este link expirou ou o aviso não está mais disponível.', destinatario=nome)
            if exigir_senha and not _autorizado(cur, token, acesso_aviso):
                return _form_senha(token, nome)
            # Abertura registra visualizacao. Visualizadores automaticos de links podem acessar esta rota.
            cur.execute('''UPDATE intranet_publicacoes_destinatarios
                SET visualizado_em=COALESCE(visualizado_em,NOW()),
                    visualizado_via=CASE WHEN visualizado_em IS NULL THEN 'WHATSAPP' ELSE visualizado_via END
                WHERE publicacao_id=%s AND usuario_id=%s''', (pub_id,uid))
    botao='<form method="post" action="/teste-aviso/'+html.escape(token,quote=True)+'/confirmar"><button type="submit">Li e estou ciente</button></form>'
    return _pagina(titulo, descricao or '', botao, destinatario=nome)


@router.post('/teste-aviso/{token}/confirmar', include_in_schema=False)
def confirmar_aviso(token: str, acesso_aviso: Optional[str] = Cookie(None)):
    if not (32 <= len(token) <= 100):
        return _pagina('Link inválido', 'Este link não está disponível.')
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute('''SELECT l.id,l.publicacao_id,l.usuario_id,u.nome,p.exigir_senha,p.exigir_facial
                FROM intranet_avisos_link_teste l
                JOIN intranet_publicacoes p ON p.id=l.publicacao_id
                JOIN intranet_publicacoes_destinatarios d
                  ON d.publicacao_id=l.publicacao_id AND d.usuario_id=l.usuario_id
                JOIN intranet_usuarios u ON u.id::text=l.usuario_id AND u.ativo=TRUE
                WHERE l.token_hash=%s AND l.utilizado_em IS NULL AND l.revogado_em IS NULL
                  AND l.expira_em>NOW() AND (p.fim + TIME '23:59:59') AT TIME ZONE 'America/Sao_Paulo'>NOW() AND p.status='PUBLICADA' AND p.exige_ciencia=TRUE
                  AND d.confirmado_em IS NULL FOR UPDATE OF l''', (_hash(token),))
            r=cur.fetchone()
            if not r:
                return _pagina('Não foi possível confirmar', 'Link expirado, inválido ou já utilizado.')
            link_id,pub_id,uid,nome,exigir_senha,exigir_facial=r
            if exigir_facial: return _pagina('Reconhecimento facial indisponível', 'Confirmação bloqueada: a validação facial ainda não está homologada.')
            if exigir_senha and not _autorizado(cur, token, acesso_aviso):
                return _pagina('Senha necessária', 'Abra o link novamente e informe sua senha antes de confirmar.')
            cur.execute('''UPDATE intranet_publicacoes_destinatarios
                SET visualizado_em=COALESCE(visualizado_em,NOW()),
                     visualizado_via=CASE WHEN visualizado_em IS NULL THEN 'WHATSAPP' ELSE visualizado_via END,
                     confirmado_em=NOW(), confirmado_via='WHATSAPP'
                WHERE publicacao_id=%s AND usuario_id=%s AND confirmado_em IS NULL
                RETURNING confirmado_em''', (pub_id,uid))
            if cur.fetchone() is None:
                return _pagina('Aviso já confirmado', 'A confirmação já foi registrada.', permitir_fechar=True)
            cur.execute('UPDATE intranet_avisos_link_teste SET utilizado_em=NOW() WHERE id=%s', (link_id,))
            cur.execute('DELETE FROM intranet_avisos_link_autorizacoes WHERE link_id=%s',(link_id,))
    return _pagina('Confirmação registrada!', 'Leitura confirmada com sucesso!', permitir_fechar=True, destinatario=nome)


@router.get('/api/teste-aviso/{link_id}/status')
def status_link(link_id: int, authorization: Optional[str] = Header(None)):
    _exigir_nivel(authorization, 3)
    with _conectar() as conn:
        with conn.cursor() as cur:
            _criar_tabela(cur)
            cur.execute('''SELECT l.id,l.publicacao_id,l.usuario_id,u.nome,p.titulo,l.criado_em,
                       l.expira_em,l.utilizado_em,l.revogado_em,d.visualizado_em,d.confirmado_em,d.visualizado_via,d.confirmado_via
                FROM intranet_avisos_link_teste l
                JOIN intranet_usuarios u ON u.id::text=l.usuario_id
                JOIN intranet_publicacoes p ON p.id=l.publicacao_id
                JOIN intranet_publicacoes_destinatarios d
                  ON d.publicacao_id=l.publicacao_id AND d.usuario_id=l.usuario_id
                WHERE l.id=%s''', (link_id,))
            r=cur.fetchone()
    if not r:
        raise HTTPException(404,'Link não encontrado')
    return dict(zip(('id','publicacao_id','usuario_id','usuario','titulo','criado_em',
                     'expira_em','utilizado_em','revogado_em','visualizado_em','confirmado_em','visualizado_via','confirmado_via'),r))


@router.get('/teste-camera-facial', include_in_schema=False)
def teste_camera_facial():
    """Diagnostico local da camera: nao captura, transmite nem armazena biometria."""
    pagina = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Teste da camera facial</title>
<style>body{font-family:system-ui,Arial;background:#f3f5f9;color:#172b4d;padding:18px}
main{max-width:440px;margin:auto;background:white;padding:22px;border-radius:14px}
video{width:100%;background:#111;border-radius:10px;transform:scaleX(-1)}
button{padding:12px;margin:6px 4px 6px 0;border:0;border-radius:8px;background:#17663b;color:white}
small{display:block;margin-top:12px;color:#586477}</style></head><body><main>
<h2>Teste da camera do celular</h2><p>Este teste apenas verifica a abertura da camera frontal.
Nao realiza reconhecimento facial ou prova de vida.</p>
<video id="video" autoplay playsinline muted></video>
<p id="estado" role="status">Camera desligada.</p>
<button id="ligar" type="button">Abrir camera</button>
<button id="parar" type="button">Desligar camera</button>
<small>Nenhuma imagem e enviada ou armazenada. Use HTTPS no celular.</small>
</main><script>
'use strict';let fluxo=null;
const video=document.getElementById('video'), estado=document.getElementById('estado');
function parar(){if(fluxo){fluxo.getTracks().forEach(t=>t.stop());fluxo=null;}video.srcObject=null;estado.textContent='Camera desligada.';}
document.getElementById('ligar').addEventListener('click',async()=>{
  parar();if(!navigator.mediaDevices?.getUserMedia){estado.textContent='Camera indisponivel: abra em HTTPS.';return;}
  try{fluxo=await navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:'user'}});
    video.srcObject=fluxo;estado.textContent='Camera frontal aberta. Teste concluido.';
  }catch(e){estado.textContent='Nao foi possivel abrir a camera: '+e.name;}
});
document.getElementById('parar').addEventListener('click',parar);
window.addEventListener('pagehide',parar);
</script></body></html>"""
    return HTMLResponse(pagina, headers={
        'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer',
        'X-Content-Type-Options':'nosniff',
        'Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; media-src blob:; base-uri 'none'; frame-ancestors 'none'",
        'Permissions-Policy':'camera=(self), microphone=()'})
