from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

router_dashboards_acesso = APIRouter(
    prefix="/api/dashboards",
    tags=["Dashboards - Acesso"]
)

_conectar = None
_autenticar = None


def configurar_dashboards_acesso(conectar, autenticar):
    global _conectar, _autenticar
    _conectar = conectar
    _autenticar = autenticar




def _nome_empresa_relatorios():
    """Usa a mesma configuração corporativa aplicada na Intranet."""
    with _conectar() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM intranet_empresa_config WHERE id=1 AND ativo=TRUE")
        registro = cur.fetchone()
        campos = [col.name for col in cur.description]
    cfg = dict(zip(campos, registro)) if registro else {}
    return str(cfg.get('nome_curto') or cfg.get('nome') or 'Intranet').strip()[:90]

def exigir_usuario(authorization):
    if _autenticar is None:
        raise HTTPException(status_code=503, detail="Autenticacao nao configurada")
    usuario = _autenticar(authorization)
    if not usuario:
        raise HTTPException(status_code=401, detail="Sessao invalida")
    if usuario[5]:
        raise HTTPException(status_code=403, detail="Troca de senha obrigatoria")
    return usuario


@router_dashboards_acesso.get("/status")
def status_dashboards_acesso(authorization: Optional[str] = Header(None)):
    usuario = exigir_usuario(authorization)
    return {
        "status": "ok",
        "usuario_id": usuario[0],
        "nivel_acesso": usuario[3]
    }


# Cada caixinha aparece somente se houver ao menos um vinculo autorizado.
@router_dashboards_acesso.get("/caixinhas")
def listar_caixinhas_autorizadas(authorization: Optional[str] = Header(None)):
    usuario = exigir_usuario(authorization)
    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.nome, c.descricao, c.icone, c.ordem
                FROM intranet_dash_caixinhas c
                JOIN intranet_usuarios u ON u.id = %s
                WHERE c.ativo = TRUE
                  AND u.ativo = TRUE
                  AND EXISTS (
                      SELECT 1
                      FROM intranet_dash_vinculos v
                      JOIN intranet_dash_dashboards d
                        ON d.id = v.dashboard_id AND d.ativo = TRUE
                      JOIN intranet_dash_permissoes_vinculos p
                        ON p.vinculo_id = v.id
                      WHERE v.caixinha_id = c.id
                        AND (
                            (p.modalidade = 'USUARIO' AND p.usuario_id = u.id)
                            OR (p.modalidade = 'CARGO' AND p.cargo = u.cargo)
                            OR (p.modalidade = 'DEPARTAMENTO'
                                AND p.departamento = u.departamento)
                        )
                  )
                ORDER BY c.ordem, c.nome, c.id
            """, (usuario[0],))
            colunas = [col.name for col in cur.description]
            registros = [dict(zip(colunas, linha)) for linha in cur.fetchall()]

    return {"total": len(registros), "caixinhas": registros}


# A autorizacao vale exclusivamente para o vinculo caixinha-dashboard.
@router_dashboards_acesso.get("/caixinhas/{caixinha_id}/dashboards")
def listar_dashboards_autorizados(
    caixinha_id: int,
    authorization: Optional[str] = Header(None)
):
    usuario = exigir_usuario(authorization)
    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")
    if caixinha_id <= 0:
        raise HTTPException(422, "ID invalido")

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT d.id, d.nome, d.descricao, d.codigo,
                       d.rota_interna, v.ordem
                FROM intranet_dash_vinculos v
                JOIN intranet_dash_caixinhas c ON c.id = v.caixinha_id
                JOIN intranet_dash_dashboards d ON d.id = v.dashboard_id
                JOIN intranet_usuarios u ON u.id = %s
                WHERE c.id = %s
                  AND c.ativo = TRUE
                  AND d.ativo = TRUE
                  AND u.ativo = TRUE
                  AND EXISTS (
                      SELECT 1
                      FROM intranet_dash_permissoes_vinculos p
                      WHERE p.vinculo_id = v.id
                        AND (
                            (p.modalidade = 'USUARIO' AND p.usuario_id = u.id)
                            OR (p.modalidade = 'CARGO' AND p.cargo = u.cargo)
                            OR (p.modalidade = 'DEPARTAMENTO'
                                AND p.departamento = u.departamento)
                        )
                  )
                ORDER BY v.ordem, d.nome, d.id
            """, (usuario[0], caixinha_id))
            colunas = [col.name for col in cur.description]
            registros = [dict(zip(colunas, linha)) for linha in cur.fetchall()]

    return {
        "caixinha_id": caixinha_id,
        "total": len(registros),
        "dashboards": registros
    }


def exigir_acesso_dashboard(authorization, rota_interna):
    usuario = exigir_usuario(authorization)

    if _conectar is None:
        raise HTTPException(503, "Banco nao configurado")

    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1
                FROM intranet_dash_vinculos v
                JOIN intranet_dash_caixinhas c
                  ON c.id = v.caixinha_id
                JOIN intranet_dash_dashboards d
                  ON d.id = v.dashboard_id
                JOIN intranet_usuarios u
                  ON u.id = %s
                WHERE c.ativo = TRUE
                  AND d.ativo = TRUE
                  AND u.ativo = TRUE
                  AND d.rota_interna = %s
                  AND EXISTS (
                      SELECT 1
                      FROM intranet_dash_permissoes_vinculos p
                      WHERE p.vinculo_id = v.id
                        AND (
                            (p.modalidade = 'USUARIO'
                             AND p.usuario_id = u.id)
                            OR
                            (p.modalidade = 'CARGO'
                             AND p.cargo = u.cargo)
                            OR
                            (p.modalidade = 'DEPARTAMENTO'
                             AND p.departamento = u.departamento)
                        )
                  )
                LIMIT 1
            """, (usuario[0], rota_interna))

            if cur.fetchone() is None:
                raise HTTPException(
                    403, "Dashboard nao autorizado"
                )

    return usuario

# VISUALIZACAO PROTEGIDA DOS DASHBOARDS IMPORTADOS
from pathlib import Path
from fastapi.responses import HTMLResponse

@router_dashboards_acesso.get("/visualizar/{codigo}", include_in_schema=False)
def visualizar_dashboard_importado(
    codigo: str,
    authorization: Optional[str] = Header(None),
    visao: Optional[str] = Query(None),
):
    if len(codigo) != 32 or any(c not in "0123456789abcdef" for c in codigo):
        raise HTTPException(404, "Dashboard nao encontrado")

    rota = "/api/dashboards/visualizar/" + codigo
    usuario = exigir_acesso_dashboard(authorization, rota)

    arquivo = Path("/intranet/documentos_upload/dashboards-importados") / (codigo + ".html")

    if not arquivo.is_file():
        raise HTTPException(404, "Arquivo do dashboard nao encontrado")

    conteudo = arquivo.read_text(encoding="utf-8")
    if "<!-- MEGAVIG_360_DADOS -->" in conteudo:
        from dashboard_megavig_360_api import preencher_dashboard
        conteudo = preencher_dashboard(conteudo, usuario, _conectar, visao=visao)

    if "<!-- MEGAVIG_MAPA_OPERACOES_DADOS -->" in conteudo:
        # Dados injetados somente apos validar a permissao do dashboard.
        import json
        from decimal import Decimal
        with _conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT p.id, p.centro_custo_nominal, p.cliente,
                    p.tipo_servico, p.cep, p.logradouro, p.numero, p.bairro,
                    p.cidade, p.uf, p.latitude, p.longitude,
                    p.precisao_localizacao, p.status,
                    COUNT(u.id) AS funcionarios
                    FROM intranet_postos p
                    LEFT JOIN intranet_usuarios u
                      ON TRIM(u.centro_custo)=p.centro_custo_nominal
                     AND u.ativo=TRUE
                    WHERE p.status='ATIVO'
                    GROUP BY p.id ORDER BY p.centro_custo_nominal""")
                colunas = [c.name for c in cur.description]
                postos = [dict(zip(colunas, linha)) for linha in cur.fetchall()]
        for posto in postos:
            for campo in ('latitude', 'longitude'):
                if isinstance(posto.get(campo), Decimal):
                    posto[campo] = float(posto[campo])
        dados_json = json.dumps(postos, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
        conteudo = conteudo.replace('<!-- MEGAVIG_MAPA_OPERACOES_DADOS -->',
            '<script type="application/json" id="megavig-mapa-dados">' + dados_json + '</script>', 1)

    # Apenas o mapa OpenFreeMap recebe permissoes de rede; outros dashboards permanecem isolados.
    mapa_ruas = 'id="maplibre"' in conteudo and 'id="megavig-mapa-dados"' in conteudo
    # MapLibre carrega estilo, fontes, sprites e tiles de hosts externos.
    # A permissao abaixo vale SOMENTE para o HTML identificado como mapa.
    # Os demais dashboards continuam com connect-src 'none'.
    politica = (
        "sandbox allow-scripts; default-src 'none'; "
        "script-src 'unsafe-inline' https://unpkg.com blob:; "
        "style-src 'unsafe-inline' https://unpkg.com; "
        "img-src data: blob: https:; "
        "connect-src https:; "
        "font-src data: https:; "
        "worker-src blob:; child-src blob:; "
        "base-uri 'none'; form-action 'none'"
        if mapa_ruas else
        "sandbox allow-scripts; default-src 'none'; "
        "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data: blob:; connect-src 'none'; "
        "base-uri 'none'; form-action 'none'"
    )
    return HTMLResponse(
        content=conteudo,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": politica
        }
    )


# MEGAVIG 360 - PDF enviado ao e-mail do usuario autenticado.
# Requer Chromium e pacote Python playwright instalados no container da API.
@router_dashboards_acesso.post("/megavig-360/enviar-pdf-meu-email")
def enviar_pdf_megavig_360(
    authorization: Optional[str] = Header(None),
    x_intranet_dashboard_route: Optional[str] = Header(None),
    visao: Optional[str] = Query(None),
):
    rota = x_intranet_dashboard_route or ""
    usuario = exigir_acesso_dashboard(authorization, rota)
    return enviar_pdf_megavig_360_interno(usuario, rota, visao)


def enviar_pdf_megavig_360_interno(usuario, rota, visao=None, pulse=None):
    """Somente chamada interna; o agendador valida acesso novamente antes de invocar."""
    import os
    import smtplib
    import logging
    import re
    from html import escape
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from email.message import EmailMessage
    from email.utils import make_msgid

    prefixo = "/api/dashboards/visualizar/"
    codigo = rota[len(prefixo):] if rota.startswith(prefixo) else ""
    if len(codigo) != 32 or any(c not in "0123456789abcdef" for c in codigo):
        raise HTTPException(400, "Identificacao do dashboard invalida")

    arquivo = Path("/intranet/documentos_upload/dashboards-importados") / (codigo + ".html")
    if not arquivo.is_file():
        raise HTTPException(404, "Dashboard nao encontrado")
    conteudo = arquivo.read_text(encoding="utf-8")
    if "<!-- MEGAVIG_360_DADOS -->" not in conteudo:
        raise HTTPException(403, "O dashboard selecionado nao e o Megavig 360")

    with _conectar() as conn:
        with conn.cursor() as cur:
            # Destinatario sempre consultado no banco; nunca fornecido pelo cliente.
            cur.execute("SELECT email, nome FROM intranet_usuarios WHERE id=%s AND ativo=TRUE", (usuario[0],))
            registro = cur.fetchone()
    destinatario = (registro[0] or "").strip() if registro else ""
    nome_gerador = (registro[1] or "").strip() if registro else ""
    if not destinatario or "@" not in destinatario or "\n" in destinatario or "\r" in destinatario:
        raise HTTPException(422, "Usuario sem e-mail valido cadastrado")

    # Reutiliza exatamente a mesma consulta e regras da visualizacao protegida.
    from dashboard_megavig_360_api import preencher_dashboard
    conteudo = preencher_dashboard(conteudo, usuario, _conectar, visao=visao)
    nome_empresa = _nome_empresa_relatorios()
    titulo_dashboard = nome_empresa + ' 360°'
    # No PDF/e-mail não há a moldura da Intranet que personaliza o título na tela.
    conteudo = conteudo.replace('Intranet 360°', titulo_dashboard).replace('Megavig 360°', titulo_dashboard)

    # A visao efetiva vem do HTML ja validado pelo backend, nao do parametro bruto.
    selecionada = re.search(r'<option value="(pessoal|equipe|empresa)" selected', conteudo)
    visao_efetiva = selecionada.group(1) if selecionada else 'pessoal'
    rotulos = {'pessoal': 'Meu status', 'equipe': 'Minha equipe', 'empresa': 'Empresa'}
    rotulo_visao = rotulos[visao_efetiva]
    data_geracao = datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%d/%m/%Y às %H:%M')
    identificacao = (
        '<div id="megavig360-identificacao-relatorio" style="font:600 12px Arial,sans-serif;'
        'background:#eaf3f9;color:#153f60;border-radius:6px;padding:8px 12px;margin:7px 0;">'
        '<strong>Visão: ' + escape(rotulo_visao) + '</strong>'
        ' &nbsp; | &nbsp; Gerado por: ' + escape(nome_gerador or 'Usuário autenticado')
        + ' &nbsp; | &nbsp; ' + escape(data_geracao) + '</div>'
    )
    pulse_texto = ''
    pulse_html = ''
    if pulse:
        freq = {'DIARIO':'Diário','UTEIS':'Dias úteis','SEMANAL':'Semanal','MENSAL':'Mensal'}.get(pulse.get('frequencia'), 'Programado')
        horario = str(pulse.get('horario') or '')[:5]
        pulse_texto = '\nEnviado automaticamente pelo Pulse. Agendamento: ' + freq + ' às ' + horario + '.'
        pulse_html = ('<p style="background:#eaf3f9;padding:10px 12px;border-radius:6px;color:#153f60">'
            '<b>Enviado automaticamente pelo Pulse</b><br>'
            'Agendamento: ' + escape(freq) + ' &bull; ' + escape(horario) + ' &bull; E-mail</p>')
    # Identificacao aparece tanto na imagem do e-mail quanto no PDF.
    if pulse_html:
        identificacao += pulse_html
    conteudo = conteudo.replace('<main id="content">', identificacao + '<main id="content">', 1)

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            navegador = pw.chromium.launch(
                headless=True,
                executable_path=os.getenv("MEGAVIG_PDF_CHROMIUM", "/usr/bin/chromium"),
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                pagina = navegador.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
                pagina.set_content(conteudo, wait_until="load", timeout=30000)
                pagina.wait_for_selector("#content .section", timeout=15000)
                # O botao de envio nao deve aparecer no relatorio impresso.
                pagina.add_style_tag(content="""@page{size:A3 landscape;margin:9mm}
                    .megavig-email-btn,.megavig-email-msg,.megavig-visao-label,.megavig-visao-select{display:none!important}
                    body{background:#fff!important}#content{grid-template-columns:repeat(3,minmax(0,1fr))!important}
                    #content>.section{break-inside:avoid;page-break-inside:avoid}
                    .top{-webkit-print-color-adjust:exact;print-color-adjust:exact}
                    *{-webkit-print-color-adjust:exact;print-color-adjust:exact}""")
                # Captura o painel para exibicao imediata no corpo do e-mail.
                pagina.emulate_media(media="screen")
                imagem = pagina.screenshot(type="png", full_page=True, animations="disabled", timeout=30000)
                pdf = pagina.pdf(format="A3", landscape=True, print_background=True,
                                 prefer_css_page_size=True, margin={"top":"9mm","right":"9mm","bottom":"9mm","left":"9mm"})
            finally:
                navegador.close()
    except Exception as exc:
        logging.exception("Falha ao gerar PDF Megavig 360")
        raise HTTPException(503, "Nao foi possivel gerar o PDF. Verifique Chromium/Playwright no container") from exc

    mensagem = EmailMessage()
    mensagem["Subject"] = titulo_dashboard + " - " + rotulo_visao
    mensagem["From"] = os.environ.get("SMTP_FROM", "")
    mensagem["To"] = destinatario
    mensagem.set_content(titulo_dashboard + "\nVisão: " + rotulo_visao + "\nGerado por: " + nome_gerador + "\nData: " + data_geracao + pulse_texto + "\nO PDF segue anexo.")
    cid = make_msgid(domain="megavig.local")
    mensagem.add_alternative(
        '<html><body style="font-family:Arial,sans-serif;color:#17324d">'
        '<h2>' + escape(titulo_dashboard) + '</h2>'
        '<p><b>Visão: ' + escape(rotulo_visao) + '</b><br>Gerado por: ' + escape(nome_gerador) + '<br>Data: ' + escape(data_geracao) + '</p>'
        + pulse_html + '<p>Confira abaixo o painel gerado com os dados atuais da Intranet.</p>'
        '<img src="cid:' + cid[1:-1] + '" alt="Painel Executivo 360" '
        'style="display:block;width:100%;max-width:1600px;height:auto;border:0">'
        '<p>O relatorio completo tambem esta disponivel no PDF anexo.</p>'
        '</body></html>', subtype="html"
    )
    mensagem.get_payload()[-1].add_related(imagem, maintype="image", subtype="png", cid=cid,
                                            filename="Dashboard-360-Painel.png", disposition="inline")
    mensagem.add_attachment(pdf, maintype="application", subtype="pdf", filename="Dashboard-360-Painel-Executivo.pdf")
    try:
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587")), timeout=20) as smtp:
            smtp.starttls()
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            smtp.send_message(mensagem)
    except Exception as exc:
        logging.exception("Falha ao enviar PDF Megavig 360")
        raise HTTPException(502, "Nao foi possivel enviar o e-mail. Verifique o SMTP") from exc

    return {"mensagem": "Painel enviado no corpo do e-mail, com PDF anexo."}


def enviar_pdf_dashboard_pulse_interno(usuario, rota, visao=None, pulse=None):
    """Entrega interna agendada. Preserva o gerador original para o Megavig 360.

    Outros dashboards são renderizados a partir do HTML local cadastrado;
    dependências externas ou dados fornecidos exclusivamente pela bridge não são suportados.
    """
    import os
    import re
    import smtplib
    import logging
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from html import escape
    from email.message import EmailMessage
    from email.utils import make_msgid
    from pathlib import Path

    if not re.fullmatch(r'/api/dashboards/visualizar/[a-f0-9]{32}', rota or ''):
        raise HTTPException(422, 'Rota de dashboard inválida')
    codigo = rota.rsplit('/', 1)[-1]
    arquivo = Path('/intranet/documentos_upload/dashboards-importados') / (codigo + '.html')
    if not arquivo.is_file():
        raise HTTPException(404, 'Dashboard indisponível')
    conteudo = arquivo.read_text(encoding='utf-8')
    if '<!-- MEGAVIG_360_DADOS -->' in conteudo:
        return enviar_pdf_megavig_360_interno(usuario, rota, visao, pulse=pulse)

    # Consulta os metadados e o e-mail no servidor; o cliente não escolhe destinatário.
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT d.nome, u.email, u.nome
                FROM intranet_dash_dashboards d
                JOIN intranet_usuarios u ON u.id=%s AND u.ativo=TRUE
                WHERE d.rota_interna=%s AND d.ativo=TRUE""", (usuario[0], rota))
            registro = cur.fetchone()
    if not registro:
        raise HTTPException(404, 'Dashboard ou usuário indisponível')
    nome_dashboard, destinatario, nome_usuario = registro
    destinatario = (destinatario or '').strip()
    if not destinatario or not re.fullmatch(r'[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+', destinatario):
        raise HTTPException(422, 'E-mail corporativo inválido no cadastro')
    nome_dashboard = str(nome_dashboard or 'Dashboard')
    data_geracao = datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%d/%m/%Y às %H:%M')
    frequencia = {'DIARIO':'Diário','UTEIS':'Dias úteis','SEMANAL':'Semanal','MENSAL':'Mensal'}.get((pulse or {}).get('frequencia'), 'Programado')
    horario = str((pulse or {}).get('horario') or '')[:5]
    identificacao = (
        '<div style="font:600 12px Arial,sans-serif;background:#eaf3f9;color:#153f60;'
        'border-radius:6px;padding:9px;margin:8px 0">'
        'Relatório: ' + escape(nome_dashboard) + ' | Gerado por: ' + escape(str(nome_usuario or ''))
        + ' | ' + escape(data_geracao) + '<br>'
        'Enviado automaticamente pelo Pulse — ' + escape(frequencia)
        + ' às ' + escape(horario) + '</div>'
    )
    if re.search(r'<body\b[^>]*>', conteudo, flags=re.I):
        conteudo = re.sub(r'(<body\b[^>]*>)', lambda m: m.group(1) + identificacao, conteudo, count=1, flags=re.I)
    else:
        conteudo = identificacao + conteudo

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            navegador = pw.chromium.launch(headless=True,
                executable_path=os.getenv('MEGAVIG_PDF_CHROMIUM', '/usr/bin/chromium'),
                args=['--no-sandbox', '--disable-dev-shm-usage'])
            try:
                pagina = navegador.new_page(viewport={'width':1600,'height':1000}, device_scale_factor=1)
                pagina.set_content(conteudo, wait_until='load', timeout=30000)
                pagina.wait_for_timeout(1500)
                # Falha explicitamente quando não há conteúdo visível.
                if len(pagina.locator('body').inner_text().strip()) < 25:
                    raise ValueError('Dashboard sem conteúdo renderizado para o PDF')
                pagina.add_style_tag(content="""@page{size:A3 landscape;margin:9mm}
                    body{background:#fff!important}*{-webkit-print-color-adjust:exact;print-color-adjust:exact}""")
                imagem = pagina.screenshot(type='png', full_page=True, animations='disabled', timeout=30000)
                pdf = pagina.pdf(format='A3', landscape=True, print_background=True,
                    prefer_css_page_size=True, margin={'top':'9mm','right':'9mm','bottom':'9mm','left':'9mm'})
            finally:
                navegador.close()
    except Exception as exc:
        logging.exception('Megavig Pulse: erro ao gerar PDF do dashboard %s', nome_dashboard)
        raise HTTPException(503, 'Falha ao renderizar o dashboard para PDF: ' + str(exc)[:200]) from exc

    msg = EmailMessage()
    msg['Subject'] = 'Pulse - ' + nome_dashboard.replace('\r',' ').replace('\n',' ')
    msg['From'] = os.environ.get('SMTP_FROM', '')
    msg['To'] = destinatario
    msg.set_content('Pulse\nRelatório: ' + nome_dashboard + '\nGerado em: ' + data_geracao + '\nPDF em anexo.')
    cid = make_msgid(domain='megavig.local')
    msg.add_alternative(
        '<html><body style="font-family:Arial,sans-serif;color:#17324d"><h2>'
        + escape(nome_dashboard) + '</h2><p>Enviado automaticamente pelo Pulse<br>'
        + escape(frequencia) + ' às ' + escape(horario) + '<br>Gerado em ' + escape(data_geracao)
        + '</p><img src="cid:' + cid[1:-1] + '" style="max-width:100%;height:auto" alt="Dashboard">'
        + '<p>Relatório completo no PDF anexo.</p></body></html>', subtype='html')
    msg.get_payload()[-1].add_related(imagem, maintype='image', subtype='png', cid=cid,
        filename='Pulse-Dashboard.png', disposition='inline')
    msg.add_attachment(pdf, maintype='application', subtype='pdf', filename='Pulse-Dashboard.pdf')
    try:
        with smtplib.SMTP(os.environ['SMTP_HOST'], int(os.getenv('SMTP_PORT','587')), timeout=20) as smtp:
            smtp.starttls()
            smtp.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
            smtp.send_message(msg)
    except Exception as exc:
        logging.exception('Megavig Pulse: falha SMTP para %s', nome_dashboard)
        raise HTTPException(502, 'Falha no envio SMTP') from exc
    return {'mensagem':'Dashboard enviado pelo Pulse com PDF anexo.'}
