"""Megavig Pulse: executor em thread no processo FastAPI, sem dependências novas.
Cada slot possui chave única no PostgreSQL, impedindo envio duplicado em reinícios.
Execuções com erro não são reenviadas automaticamente para evitar duplicação SMTP.
"""
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ=ZoneInfo('America/Sao_Paulo')
_stop=threading.Event()
_thread=None
_conectar=None


def rota_dashboard(cur,codigo):
    if not codigo.startswith('DASHBOARD:') or not codigo[10:].isdigit():
        raise ValueError('Código de dashboard inválido')
    cur.execute('SELECT rota_interna FROM intranet_dash_dashboards WHERE id=%s AND ativo=TRUE',(int(codigo[10:]),))
    row=cur.fetchone()
    if not row: raise ValueError('Dashboard indisponível')
    return row[0]


def validar_dashboard_360(rota):
    import re
    if not re.fullmatch(r'/api/dashboards/visualizar/[a-f0-9]{32}',rota or ''):
        raise ValueError('Rota do dashboard inválida')
    codigo=rota.rsplit('/',1)[-1]
    arquivo=Path('/intranet/documentos_upload/dashboards-importados')/(codigo+'.html')
    if not arquivo.is_file() or '<!-- MEGAVIG_360_DADOS -->' not in arquivo.read_text(encoding='utf-8'):
        raise ValueError('Envio automático disponível apenas para Megavig 360°')


def validar_dashboard(rota):
    import re
    if not re.fullmatch(r'/api/dashboards/visualizar/[a-f0-9]{32}', rota or ''):
        raise ValueError('Rota do dashboard inválida para envio automático')
    codigo = rota.rsplit('/', 1)[-1]
    arquivo = Path('/intranet/documentos_upload/dashboards-importados') / (codigo + '.html')
    if not arquivo.is_file():
        raise ValueError('Arquivo HTML do dashboard indisponível')
    return arquivo


def _autorizado(cur,uid,dashboard_id):
    cur.execute("""SELECT 1 FROM intranet_dash_vinculos v
      JOIN intranet_dash_caixinhas c ON c.id=v.caixinha_id AND c.ativo=TRUE
      JOIN intranet_dash_dashboards d ON d.id=v.dashboard_id AND d.ativo=TRUE
      JOIN intranet_usuarios u ON u.id=%s AND u.ativo=TRUE AND u.nivel_acesso IN (2,3) AND u.trocar_senha=FALSE
      WHERE d.id=%s AND EXISTS(SELECT 1 FROM intranet_dash_permissoes_vinculos p WHERE p.vinculo_id=v.id AND
       ((p.modalidade='USUARIO' AND p.usuario_id=u.id) OR
        (p.modalidade='CARGO' AND p.cargo=u.cargo) OR
        (p.modalidade='DEPARTAMENTO' AND p.departamento=u.departamento))) LIMIT 1""",(uid,dashboard_id))
    return cur.fetchone() is not None


def _vence(frequencia,agora):
    if frequencia=='DIARIO': return True
    if frequencia=='UTEIS': return agora.weekday()<5
    if frequencia=='SEMANAL': return agora.weekday()==0  # segunda-feira
    if frequencia=='MENSAL': return agora.day==1
    return False


def _executar(aid,uid,codigo,canal,chave,frequencia,horario):
    exec_id=None
    try:
        with _conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO pulse_execucoes(automacao_id,chave_execucao,status,iniciada_em)
                    VALUES(%s,%s,'EXECUTANDO',now()) ON CONFLICT(chave_execucao) DO NOTHING RETURNING id""",(aid,chave))
                r=cur.fetchone()
                if not r: return
                exec_id=r[0]
        if canal!='EMAIL': raise ValueError('Canal não suportado pelo executor')
        with _conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM pulse_automacoes WHERE id=%s AND usuario_id=%s",(aid,uid))
                st=cur.fetchone()
                if not st or st[0]!='ATIVA': raise ValueError('Agendamento não ativo')
                if not codigo.startswith('DASHBOARD:') or not codigo[10:].isdigit(): raise ValueError('Código inválido')
                if not _autorizado(cur,uid,int(codigo[10:])): raise ValueError('Permissão revogada')
                rota=rota_dashboard(cur,codigo)
                validar_dashboard(rota)
                cur.execute("""SELECT id,cpf,nome,nivel_acesso,tipo,trocar_senha,NULL
                    FROM intranet_usuarios WHERE id=%s AND ativo=TRUE""",(uid,))
                usuario=cur.fetchone()
                if not usuario: raise ValueError('Usuário inativo')
        from dashboards_acesso_api import enviar_pdf_dashboard_pulse_interno
        enviar_pdf_dashboard_pulse_interno(usuario,rota,None,pulse={"frequencia":frequencia,"horario":horario})
        with _conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE pulse_execucoes SET status='SUCESSO',concluida_em=now() WHERE id=%s",(exec_id,))
                cur.execute("INSERT INTO pulse_entregas(execucao_id,canal,status,enviada_em) VALUES(%s,'EMAIL','SUCESSO',now())",(exec_id,))
    except Exception as exc:
        logging.exception('Megavig Pulse: erro na execução %s',aid)
        if exec_id:
            try:
                with _conectar() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE pulse_execucoes SET status='ERRO',concluida_em=now(),erro=%s WHERE id=%s",(str(exc)[:1500],exec_id))
                        cur.execute("INSERT INTO pulse_entregas(execucao_id,canal,status,erro) VALUES(%s,%s,'ERRO',%s)",(exec_id,canal,str(exc)[:1500]))
            except Exception: logging.exception('Megavig Pulse: falha ao registrar erro')


def _tick():
    agora=datetime.now(TZ)
    # Captura somente o minuto corrente; não executa horários antigos após reinício.
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,usuario_id,codigo,frequencia,canal
                FROM pulse_automacoes WHERE status='ATIVA' AND horario=%s::time""",(agora.strftime('%H:%M:00'),))
            rows=cur.fetchall()
    for aid,uid,codigo,frequencia,canal in rows:
        if _vence(frequencia,agora):
            chave=f'{aid}:{agora.strftime("%Y-%m-%dT%H:%M")}'
            _executar(aid,uid,codigo,canal,chave,frequencia,agora.strftime("%H:%M"))


def _loop():
    while not _stop.is_set():
        try: _tick()
        except Exception: logging.exception('Megavig Pulse: erro no agendador')
        _stop.wait(15)


def iniciar_agendador(conectar):
    global _thread,_conectar
    if os.getenv('MEGAVIG_PULSE_AGENDADOR','1')!='1':
        logging.info('Megavig Pulse: agendador desativado; configure MEGAVIG_PULSE_AGENDADOR=1')
        return
    _conectar=conectar
    if _thread and _thread.is_alive(): return
    _stop.clear()
    _thread=threading.Thread(target=_loop,name='megavig-pulse',daemon=True)
    _thread.start()
    logging.info('Megavig Pulse: agendador iniciado')


def parar_agendador():
    _stop.set()
