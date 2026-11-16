"""Megavig 360 — agregados reais com escopo de acesso no backend.
Indicadores sem esquema verificável retornam null (nunca zero fictício).
"""
import json
from fastapi import HTTPException

MARCADOR = '<!-- MEGAVIG_360_DADOS -->'

def preparar_visao_html(html, usuario_id, visao, permitidas):
    """Insere seletor apenas quando existem duas ou mais visoes autorizadas."""
    import html as html_lib
    labels = {'pessoal': 'Meu status', 'equipe': 'Minha equipe', 'empresa': 'Empresa'}
    if len(permitidas) > 1:
        botoes = ''.join(
            '<option value="' + v + '"' + (' selected' if v == visao else '') + '>' + labels[v] + '</option>'
            for v in permitidas
        )
        controle = ('<label class="megavig-visao-label" for="megavig-visao">Visualizacao</label>'
                    '<select class="megavig-visao-select" id="megavig-visao" aria-label="Selecionar visao">'
                    + botoes + '</select>')
        html = html.replace('<h1>Megavig 360°</h1>', '<div class="megavig-title-row"><h1>Megavig 360°</h1>' + controle + '</div>', 1)
    # Identificador obtido exclusivamente da sessao autenticada no backend.
    # A chave e individual por usuario no mesmo navegador. Persistencia entre
    # dispositivos exigira backend e nao faz parte da demonstracao atual.
    html = html.replace('<!-- MEGAVIG_360_USUARIO -->',
                        '<script>window.MEGAVIG_360_USUARIO=' + json.dumps(str(usuario_id)) + ';</script>', 1)
    return html



def preencher_dashboard(html, usuario, conectar, visao=None):
    if MARCADOR not in html:
        return html
    nivel = int(usuario[3])
    if nivel not in (1, 2, 3):
        raise HTTPException(403, 'Nível de acesso inválido')
    # Escolha de visao validada no servidor. Nunca confiar no seletor HTML.
    usuario_id = int(usuario[0])
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tipo FROM intranet_usuarios WHERE id=%s AND ativo=TRUE", (usuario_id,))
            registro = cur.fetchone()
            if not registro:
                raise HTTPException(403, 'Usuario inativo ou nao encontrado')
            gestor = str(registro[0]) == '2'
    permitidas = ['pessoal']
    if nivel in (2, 3) and gestor:
        permitidas.append('equipe')
    if nivel == 3:
        permitidas.append('empresa')
    padrao = 'empresa' if nivel == 3 else ('equipe' if gestor and nivel == 2 else 'pessoal')
    visao = visao or padrao
    if visao not in permitidas:
        raise HTTPException(403, 'Visao nao autorizada')
    html = preparar_visao_html(html, usuario_id, visao, permitidas)
    # Catalogo Pulse: somente dashboards com vinculo autorizado no banco.
    # Nao altera a consulta nem o escopo dos indicadores do Megavig 360.
    catalogo = []
    if nivel in (2, 3):
        with conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT d.id, d.nome, d.rota_interna
                    FROM intranet_dash_vinculos v
                    JOIN intranet_dash_caixinhas c ON c.id=v.caixinha_id AND c.ativo=TRUE
                    JOIN intranet_dash_dashboards d ON d.id=v.dashboard_id AND d.ativo=TRUE
                    JOIN intranet_usuarios u ON u.id=%s AND u.ativo=TRUE
                    WHERE EXISTS (
                        SELECT 1 FROM intranet_dash_permissoes_vinculos perm
                        WHERE perm.vinculo_id=v.id AND (
                            (perm.modalidade='USUARIO' AND perm.usuario_id=u.id)
                            OR (perm.modalidade='CARGO' AND perm.cargo=u.cargo)
                            OR (perm.modalidade='DEPARTAMENTO' AND perm.departamento=u.departamento)
                        )
                    )
                    ORDER BY d.nome, d.id
                """, (usuario_id,))
                catalogo = [{'id': r[0], 'nome': r[1], 'rota': r[2]} for r in cur.fetchall()]
    # Logotipo oficial: campo logo da configuracao corporativa.
    # A cada abertura consulta o banco; troca no ADMIN nao exige editar codigo.
    logo_data = None
    if nivel == 1:
        from pathlib import Path
        import base64
        import mimetypes
        import re
        with conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT logo FROM intranet_empresa_config WHERE id = 1 AND ativo = TRUE")
                registro_logo = cur.fetchone()
        nome_logo = registro_logo[0] if registro_logo else None
        if isinstance(nome_logo, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,180}\.(?:png|jpg|jpeg|webp)', nome_logo, re.I):
            pasta_logo = Path('/intranet/documentos_upload/empresa_assets')
            arquivo_logo = pasta_logo / nome_logo
            if arquivo_logo.is_file():
                mime_logo = mimetypes.guess_type(nome_logo)[0] or 'image/png'
                logo_data = 'data:' + mime_logo + ';base64,' + base64.b64encode(arquivo_logo.read_bytes()).decode('ascii')
    html = html.replace('<script>window.MEGAVIG_360_USUARIO=',
        '<script>window.MEGAVIG_PULSE_NIVEL=' + json.dumps(nivel) +
        ';window.MEGAVIG_PULSE_DASHBOARDS=' + json.dumps(catalogo, ensure_ascii=False).replace('<','\\u003c') +
        ';window.MEGAVIG_PULSE_LOGO=' + json.dumps(logo_data) + '</script><script>window.MEGAVIG_360_USUARIO=', 1)

    if visao != 'empresa':
        return preencher_dashboard_restrito(html, usuario, conectar, somente_proprio=(visao == 'pessoal'), visao=visao)

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                           WHERE table_schema = 'public'""")
            esquema = {}
            for tabela, coluna in cur.fetchall():
                esquema.setdefault(tabela, set()).add(coluna)

            def tem(tabela, *colunas):
                return all(c in esquema.get(tabela, set()) for c in colunas)

            def contar(tabela, condicao=None, requer=()):
                if not tem(tabela, *requer):
                    return None
                # Nomes de tabelas vêm somente de constantes internas, nunca de entradas externas.
                cur.execute('SELECT COUNT(*) FROM ' + tabela + ((' WHERE ' + condicao) if condicao else ''))
                return int(cur.fetchone()[0])

            def distintos(tabela, coluna, condicao=None):
                if not tem(tabela, coluna):
                    return None
                cur.execute('SELECT COUNT(DISTINCT ' + coluna + ') FROM ' + tabela +
                            ((' WHERE ' + condicao) if condicao else ''))
                return int(cur.fetchone()[0])

            dados = {
                'funcionarios': contar('intranet_usuarios', 'ativo = TRUE', ('ativo',)),
                'com_acesso': contar('intranet_usuarios', 'ativo = TRUE AND ultimo_acesso IS NOT NULL', ('ativo','ultimo_acesso')),
                'treinamentos_pendentes': contar('intranet_trein_participantes', "status = 'PENDENTE'", ('status',)),
                'treinamentos_concluidos': contar('intranet_trein_participantes', "status = 'CONCLUIDO'", ('status',)),
                'avaliacoes_pendentes': contar('intranet_aval_participantes', "status = 'PENDENTE'", ('status',)),
                'avaliacoes_concluidas': contar('intranet_aval_participantes', "status = 'CONCLUIDO'", ('status',)),
                'pesquisas_publicadas': contar('pesq_pesquisas', "status = 'PUBLICADA'", ('status',)),
                'pesquisas_participacoes': contar('pesq_participacao'),
                'comunicados_entregas': contar('intranet_publicacoes_destinatarios'),
                'comunicados_visualizacoes': contar('intranet_publicacoes_destinatarios', 'visualizado_em IS NOT NULL', ('visualizado_em',)),
                'comunicados_confirmacoes': contar('intranet_publicacoes_destinatarios', 'confirmado_em IS NOT NULL', ('confirmado_em',)),
                'logins_sucesso': contar('intranet_auditoria', "tipo_evento = 'AUTENTICACAO' AND acao = 'LOGIN' AND resultado = 'SUCESSO'", ('tipo_evento','acao','resultado')),
            }
            # Série histórica: fotografia no último dia de cada mês concluído;
            # no mês corrente, fotografia na data local de hoje (São Paulo).
            # Desligamento na data de referência não conta como ativo no fechamento.
            # Nunca filtrar meses passados pelo campo ativo atual.
            dados['colaboradores_mensal'] = None
            if tem('intranet_usuarios', 'data_admissao', 'data_demissao', 'ativo'):
                cur.execute("""
                    WITH hoje AS (
                        SELECT (NOW() AT TIME ZONE 'America/Sao_Paulo')::date AS data
                    ), meses AS (
                        SELECT g.mes::date AS inicio,
                               LEAST((g.mes + INTERVAL '1 month - 1 day')::date, h.data) AS referencia,
                               h.data AS hoje
                        FROM hoje h
                        CROSS JOIN LATERAL generate_series(
                            date_trunc('year', h.data)::date,
                            date_trunc('month', h.data)::date,
                            INTERVAL '1 month'
                        ) AS g(mes)
                    )
                    SELECT EXTRACT(MONTH FROM m.inicio)::int,
                           m.referencia,
                           COUNT(u.id)::int
                    FROM meses m
                    LEFT JOIN intranet_usuarios u
                      ON u.data_admissao IS NOT NULL
                     AND u.data_admissao <= m.referencia
                     AND (u.data_demissao IS NULL OR u.data_demissao > m.referencia)
                     AND (m.referencia <> m.hoje OR u.ativo = TRUE)
                    GROUP BY m.inicio, m.referencia
                    ORDER BY m.inicio
                """)
                dados['colaboradores_mensal'] = [
                    {'mes': mes, 'data': referencia.isoformat(), 'ativos': total}
                    for mes, referencia, total in cur.fetchall()
                ]
            # Engajamento: logins reais no ano corrente, horário local de São Paulo.
            # Índice = colaboradores ATIVOS distintos com login no ano / colaboradores ativos.
            dados['intranet_logins_ano'] = None
            dados['intranet_indice_acesso'] = None
            dados['intranet_usuarios_ano'] = None
            dados['intranet_logins_mensal'] = None
            if tem('intranet_auditoria', 'tipo_evento', 'acao', 'resultado', 'criado_em', 'usuario_id') and tem('intranet_usuarios', 'id', 'ativo'):
                cur.execute("""
                    WITH periodo AS (
                        SELECT date_trunc('year', NOW() AT TIME ZONE 'America/Sao_Paulo') AS inicio,
                               (NOW() AT TIME ZONE 'America/Sao_Paulo') AS agora
                    ), eventos AS (
                        SELECT a.usuario_id,
                               (a.criado_em AT TIME ZONE 'America/Sao_Paulo') AS momento
                        FROM intranet_auditoria a
                        WHERE a.tipo_evento='AUTENTICACAO'
                          AND a.acao='LOGIN' AND a.resultado='SUCESSO'
                    )
                    SELECT COUNT(*)::int,
                           COUNT(DISTINCT e.usuario_id) FILTER (WHERE u.ativo = TRUE)::int
                    FROM eventos e
                    CROSS JOIN periodo p
                    LEFT JOIN intranet_usuarios u ON u.id=e.usuario_id
                    WHERE e.momento >= p.inicio AND e.momento <= p.agora
                """)
                total, distintos_ativos = cur.fetchone()
                dados['intranet_logins_ano'] = total
                dados['intranet_usuarios_ano'] = distintos_ativos
                if dados['funcionarios']:
                    dados['intranet_indice_acesso'] = round(100.0 * distintos_ativos / dados['funcionarios'], 1)
                cur.execute("""
                    WITH hoje AS (SELECT (NOW() AT TIME ZONE 'America/Sao_Paulo')::date AS dia),
                    meses AS (
                        SELECT EXTRACT(MONTH FROM g.mes)::int AS mes,
                               g.mes::date AS inicio,
                               (g.mes + INTERVAL '1 month')::date AS proximo
                        FROM hoje h CROSS JOIN LATERAL generate_series(
                            date_trunc('year', h.dia)::date,
                            date_trunc('month', h.dia)::date,
                            INTERVAL '1 month') g(mes)
                    )
                    SELECT m.mes, COUNT(a.usuario_id)::int
                    FROM meses m
                    LEFT JOIN intranet_auditoria a
                      ON a.tipo_evento='AUTENTICACAO' AND a.acao='LOGIN'
                     AND a.resultado='SUCESSO'
                     AND (a.criado_em AT TIME ZONE 'America/Sao_Paulo') >= m.inicio
                     AND (a.criado_em AT TIME ZONE 'America/Sao_Paulo') < m.proximo
                    GROUP BY m.mes ORDER BY m.mes
                """)
                dados['intranet_logins_mensal'] = [{'mes': mes, 'logins': total} for mes, total in cur.fetchall()]
            # Estrutura organizacional: usar somente colunas cuja semântica é inequívoca.
            dados['gestores'] = contar('intranet_usuarios', 'ativo = TRUE AND tipo = 2', ('ativo', 'tipo'))
            for chave, colunas in [('departamentos', ('departamento','departamento_id')),
                                   ('centros_custo', ('centro_custo','centro_custo_id'))]:
                dados[chave] = None
                for coluna in colunas:
                    if tem('intranet_usuarios', coluna, 'ativo'):
                        dados[chave] = distintos('intranet_usuarios', coluna,
                                                 'ativo = TRUE AND ' + coluna + ' IS NOT NULL')
                        break
            # Publicações: contagens por destinatário, não por comunicado.
            dados['avisos_lidos'] = dados['comunicados_visualizacoes']
            dados['avisos_pendentes'] = contar('intranet_publicacoes_destinatarios',
                                              'visualizado_em IS NULL', ('visualizado_em',))
            # Avaliações: pendentes + iniciadas, quando o esquema disponibiliza status.
            dados['avaliacoes_andamento'] = contar('intranet_aval_participantes',
                "status IN ('PENDENTE', 'EM_ANDAMENTO')", ('status',))
            # Pesquisas: publicada é aberta somente quando não há outra regra de período.
            # Sem conhecer datas e encerramento, não atribuir uma classificação potencialmente falsa.
            dados['pesquisas_andamento'] = None
            dados['pesquisas_encerradas'] = None
            dados['pesquisas_departamentos'] = None
            # Treinamentos: participantes não equivalem a cursos distintos.
            # Contar cursos únicos por status de participante apenas se o vínculo existir.
            if tem('intranet_trein_participantes', 'curso_id', 'status'):
                for chave, condicao in [
                    ('treinamentos_realizados', "status = 'CONCLUIDO'"),
                    ('treinamentos_andamento', "status = 'EM_ANDAMENTO'")]:
                    dados[chave] = distintos('intranet_trein_participantes', 'curso_id', condicao)
            else:
                dados['treinamentos_realizados'] = None
                dados['treinamentos_andamento'] = None
            dados['treinamentos_disponiveis'] = None
            # Indicadores confirmados pelo esquema enviado em 11/11.
            dados['tipos_documentos_publicados'] = distintos('intranet_doc_arquivos', 'tipo_id')
            # Total de documentos cadastrados; tabela não possui status de publicação.
            dados['documentos_disponiveis'] = contar('intranet_doc_arquivos')
            # Downloads efetivamente auditados, não acessos ao módulo.
            dados['documentos_downloads'] = contar('intranet_auditoria',
                "tipo_evento='DOCUMENTO' AND acao='DOWNLOAD' AND modulo='DOCUMENTOS'",
                ('tipo_evento','acao','modulo'))

            dados['treinamentos_publicados_empresa'] = contar('intranet_trein_cursos', "status = 'PUBLICADO'", ('status',))
            # Resumo corporativo idêntico aos agrupamentos da tela ADMIN de Treinamentos.
            dados['treinamentos_cadastrados'] = contar('intranet_trein_cursos')
            dados['treinamentos_publicados_ead'] = contar('intranet_trein_cursos',
                "status = 'PUBLICADO' AND modalidade = 'EAD'", ('status','modalidade'))
            dados['treinamentos_publicados_presencial'] = contar('intranet_trein_cursos',
                "status = 'PUBLICADO' AND modalidade = 'PRESENCIAL'", ('status','modalidade'))
            dados['treinamentos_inscricoes'] = contar('intranet_trein_participantes')
            dados['treinamentos_pendentes'] = contar('intranet_trein_participantes',
                "status = 'PENDENTE'", ('status',))
            dados['treinamentos_concluidos'] = contar('intranet_trein_participantes',
                "status = 'CONCLUIDO'", ('status',))
            dados['treinamentos_participacoes_andamento'] = contar('intranet_trein_participantes',
                "status = 'EM_ANDAMENTO'", ('status',))
            # Indicadores separados por modalidade. Contagens por inscrição/participante.
            if tem('intranet_trein_participantes', 'curso_id', 'status') and tem('intranet_trein_cursos', 'id', 'modalidade'):
                for modalidade, prefixo in [('EAD','ead'), ('PRESENCIAL','presencial')]:
                    for sufixo, condicao in [('inscricoes', None), ('concluidos', "p.status = 'CONCLUIDO'"), ('andamento', "p.status = 'EM_ANDAMENTO'")]:
                        where = "c.modalidade = %s" + ((' AND ' + condicao) if condicao else '')
                        cur.execute('SELECT COUNT(*) FROM intranet_trein_participantes p JOIN intranet_trein_cursos c ON c.id = p.curso_id WHERE ' + where, (modalidade,))
                        dados['trein_' + prefixo + '_' + sufixo] = int(cur.fetchone()[0])
            else:
                for prefixo in ('ead', 'presencial'):
                    for sufixo in ('inscricoes','concluidos','andamento'):
                        dados['trein_' + prefixo + '_' + sufixo] = None
            # Carga horária geral dos cursos publicados por modalidade, sem multiplicar por participantes.
            if tem('intranet_trein_cursos', 'status', 'modalidade', 'carga_horaria_min'):
                for modalidade, prefixo in [('EAD','ead'),('PRESENCIAL','presencial')]:
                    cur.execute("SELECT ROUND(COALESCE(SUM(carga_horaria_min),0)/60.0,1) FROM intranet_trein_cursos WHERE status = 'PUBLICADO' AND modalidade = %s", (modalidade,))
                    dados['trein_' + prefixo + '_horas_gerais'] = float(cur.fetchone()[0])
            else:
                dados['trein_ead_horas_gerais'] = dados['trein_presencial_horas_gerais'] = None
            # O catálogo individual depende das regras de público e inscrições do endpoint /catalogo.
            dados['treinamentos_disponiveis'] = None
            def escalar(sql):
                cur.execute(sql)
                valor = cur.fetchone()[0]
                return float(valor) if valor is not None else None
            dados['horas_disponiveis'] = escalar("""SELECT ROUND(COALESCE(SUM(carga_horaria_min),0)/60.0,1)
                FROM intranet_trein_cursos WHERE status = 'PUBLICADO'""") if tem('intranet_trein_cursos','status','carga_horaria_min') else None
            # Horas realizadas / em andamento: soma da carga horária por inscrição.
            # São horas de formação por participante, não duração de cursos únicos.
            for chave, situacao in [('horas_realizadas','CONCLUIDO'), ('horas_andamento','EM_ANDAMENTO')]:
                dados[chave] = escalar("""SELECT ROUND(COALESCE(SUM(c.carga_horaria_min),0)/60.0,1)
                    FROM intranet_trein_participantes p JOIN intranet_trein_cursos c ON c.id=p.curso_id
                    WHERE p.status = '%s'""" % situacao) if tem('intranet_trein_participantes','status','curso_id') and tem('intranet_trein_cursos','id','carga_horaria_min') else None
            # Horas de cursos com conclusão no mês, cada curso contado uma vez por mês (EAD + presencial).
            # Não atribuir carga horária de curso a uma participação ainda pendente.
            # Exige data real de conclusão; sem ela a série é indisponível (null).
            dados['treinamentos_horas_mensal'] = None
            if tem('intranet_trein_participantes', 'curso_id', 'status', 'concluido_em') and tem('intranet_trein_cursos', 'id', 'carga_horaria_min'):
                cur.execute("""
                    WITH meses AS (
                        SELECT generate_series(1, EXTRACT(MONTH FROM NOW() AT TIME ZONE 'America/Sao_Paulo')::int) AS mes
                    ), concluidos AS (
                        SELECT u.mes, SUM(u.carga_horaria_min)::numeric / 60 AS horas
                        FROM (
                            SELECT DISTINCT
                                EXTRACT(MONTH FROM (p.concluido_em AT TIME ZONE 'America/Sao_Paulo'))::int AS mes,
                                c.id AS curso_id,
                                COALESCE(c.carga_horaria_min,0) AS carga_horaria_min
                            FROM intranet_trein_participantes p
                            JOIN intranet_trein_cursos c ON c.id = p.curso_id
                            WHERE p.status = 'CONCLUIDO' AND p.concluido_em IS NOT NULL
                              AND (p.concluido_em AT TIME ZONE 'America/Sao_Paulo') >= date_trunc('year', NOW() AT TIME ZONE 'America/Sao_Paulo')
                              AND (p.concluido_em AT TIME ZONE 'America/Sao_Paulo') <= (NOW() AT TIME ZONE 'America/Sao_Paulo')
                        ) u
                        GROUP BY u.mes
                    )
                    SELECT m.mes, ROUND(COALESCE(c.horas,0),1)
                    FROM meses m LEFT JOIN concluidos c ON c.mes=m.mes ORDER BY m.mes
                """)
                dados['treinamentos_horas_mensal'] = [{'mes': int(m), 'horas': float(h)} for m,h in cur.fetchall()]
            # Acesso ao módulo != visualização individual de uma foto ou vídeo.
            dados['fotos_visualizacoes'] = None
            dados['videos_visualizacoes'] = None
            if tem('intranet_auditoria','tipo_evento','acao','modulo'):
                for chave, modulo in [('galeria_acessos','GALERIA'),('videos_acessos','VIDEOS'),('documentos_acessos','DOCUMENTOS')]:
                    dados[chave] = contar('intranet_auditoria', "tipo_evento='NAVEGACAO' AND acao='ACESSO_MODULO' AND modulo='%s'" % modulo)
                dados['documentos_visualizacoes'] = contar('intranet_auditoria', "tipo_evento='DOCUMENTO' AND acao='VISUALIZACAO_PDF' AND modulo='DOCUMENTOS'")
            else:
                dados['galeria_acessos'] = dados['videos_acessos'] = dados['documentos_acessos'] = dados['documentos_visualizacoes'] = None
            # Pesquisas publicadas e dentro do período; encerradas por prazo, sem confundir rascunho.
            if tem('pesq_pesquisas','status','inicio_em','encerramento_em'):
                dados['pesquisas_andamento'] = contar('pesq_pesquisas', "status='PUBLICADA' AND (inicio_em IS NULL OR inicio_em<=NOW()) AND (encerramento_em IS NULL OR encerramento_em>=NOW())")
                dados['pesquisas_encerradas'] = contar('pesq_pesquisas', "status='ENCERRADA' OR (status='PUBLICADA' AND encerramento_em<NOW())")
            else:
                dados['pesquisas_andamento'] = dados['pesquisas_encerradas'] = None
            # Não atribuir departamento a respostas anônimas por cruzamento de registros.
            dados['pesquisas_departamentos'] = None
            dados['pesquisas_com_respostas'] = distintos('pesq_participacao', 'pesquisa_id')
            # Modalidades são ciclos, não quantidade de avaliações individuais.
            for chave, modalidade in [('avaliacoes_90',90),('avaliacoes_180',180)]:
                dados[chave] = contar('intranet_aval_ciclos', 'modalidade = %d' % modalidade, ('modalidade',))
            # Somente participantes com relatório consolidado; avaliadores 180°
            # preservados e agregados sem duplicar o participante.
            dados.update({
                'avaliacoes_media': None, 'avaliacoes_meta_esperada': None,
                'aval_comp_obtida': None, 'aval_comp_esperada': None,
                'aval_metas_obtida': None, 'aval_metas_esperada': None,
            })
            if all(tem(t, *cols) for t, cols in [
                ('intranet_aval_participantes', ('id','status')),
                ('intranet_aval_relatorios', ('participante_id','consolidado_em')),
                ('intranet_aval_avaliacoes', ('id','participante_id','status')),
                ('intranet_aval_respostas', ('avaliacao_id','criterio_id','nota','percentual_atingimento')),
                ('intranet_aval_criterios', ('id','tipo','nota_esperada')),
            ]):
                cur.execute("""
                    WITH base AS (
                      SELECT a.participante_id, c.id AS criterio_id, c.tipo,
                        CASE WHEN c.tipo='CRITERIO' THEN r.nota::numeric / 5
                             WHEN c.tipo='META' THEN
                               CASE WHEN r.percentual_atingimento >= 100 THEN 1::numeric ELSE 0::numeric END
                        END AS obtido,
                        CASE WHEN c.tipo='CRITERIO' THEN c.nota_esperada::numeric / 5
                             WHEN c.tipo='META' THEN 1::numeric END AS esperado
                      FROM intranet_aval_participantes p
                      JOIN intranet_aval_relatorios rel ON rel.participante_id=p.id AND rel.consolidado_em IS NOT NULL
                      JOIN intranet_aval_avaliacoes a ON a.participante_id=p.id AND a.status='CONCLUIDA'
                      JOIN intranet_aval_respostas r ON r.avaliacao_id=a.id
                      JOIN intranet_aval_criterios c ON c.id=r.criterio_id
                      WHERE p.status='CONCLUIDO' AND c.tipo IN ('CRITERIO','META')
                    ), criterio_consolidado AS (
                      SELECT participante_id,criterio_id,tipo,
                             AVG(obtido) AS obtido, AVG(esperado) AS esperado
                      FROM base WHERE obtido IS NOT NULL AND esperado IS NOT NULL
                      GROUP BY participante_id,criterio_id,tipo
                    ), participante_tipo AS (
                      SELECT participante_id,tipo, AVG(obtido) AS obtido, AVG(esperado) AS esperado
                      FROM criterio_consolidado GROUP BY participante_id,tipo
                    )
                    SELECT tipo, ROUND(AVG(obtido)*100,1), ROUND(AVG(esperado)*100,1)
                    FROM participante_tipo GROUP BY tipo
                """)
                grupos = {tipo: (float(o),float(e)) for tipo,o,e in cur.fetchall() if o is not None and e is not None}
                if 'CRITERIO' in grupos:
                    dados['aval_comp_obtida'],dados['aval_comp_esperada'] = grupos['CRITERIO']
                if 'META' in grupos:
                    dados['aval_metas_obtida'],dados['aval_metas_esperada'] = grupos['META']
                # Não combinar escalas diferentes como notas brutas; ambas
                # são normalizadas para 0–100 antes da média das categorias.
                if grupos:
                    dados['avaliacoes_media'] = round(sum(v[0] for v in grupos.values())/len(grupos),1)
                    dados['avaliacoes_meta_esperada'] = round(sum(v[1] for v in grupos.values())/len(grupos),1)

    payload = json.dumps(dados, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return html.replace(MARCADOR, '<script type="application/json" id="megavig360-dados">' + payload + '</script>', 1)


# Escopo restrito: somente métricas cujo vínculo ao usuário é verificável.
# Valores desconhecidos são null, nunca totais corporativos disfarçados.
def preencher_dashboard_restrito(html, usuario, conectar, somente_proprio=False, visao="equipe"):

    try:
        usuario_id = int(usuario[0])
    except (TypeError, ValueError, IndexError):
        raise HTTPException(403, 'Identidade do usuário indisponível')

    dados = {}
    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                           WHERE table_schema='public'""")
            esquema = {}
            for tabela, coluna in cur.fetchall():
                esquema.setdefault(tabela, set()).add(coluna)

            def identificador(nome):
                # Identificadores são exclusivamente nomes de tabelas/colunas confirmados no esquema.
                if not isinstance(nome, str) or not nome.isidentifier():
                    raise HTTPException(500, 'Identificador SQL inválido')
                return '"' + nome.replace('"', '""') + '"'

            def tem(t, *cols):
                return all(c in esquema.get(t, set()) for c in cols)

            if not tem('intranet_usuarios', 'id', 'ativo'):
                raise HTTPException(503, 'Cadastro de usuários sem estrutura verificável')
            cur.execute('SELECT id FROM intranet_usuarios WHERE id=%s AND ativo=TRUE', (usuario_id,))
            if cur.fetchone() is None:
                raise HTTPException(403, 'Usuário inativo ou não encontrado')

            ids = [usuario_id]
            # Gestor: incluir apenas subordinados diretos com vinculo ativo,
            # sem expandir a hierarquia recursivamente.
            if tem('intranet_usuarios', 'tipo'):
                cur.execute('SELECT tipo FROM intranet_usuarios WHERE id=%s', (usuario_id,))
                tipo = cur.fetchone()[0]
                if str(tipo) == '2' and not somente_proprio:
                    if not tem('intranet_gestores_funcionarios',
                               'gestor_id', 'funcionario_id', 'ativo', 'fim_em'):
                        raise HTTPException(503, 'Estrutura de Gestores e Equipes indisponivel')
                    cur.execute("""
                        SELECT DISTINCT u.id
                        FROM intranet_gestores_funcionarios g
                        JOIN intranet_usuarios u ON u.id = g.funcionario_id
                        WHERE g.gestor_id = %s
                          AND g.ativo = TRUE
                          AND g.fim_em IS NULL
                          AND u.ativo = TRUE
                    """, (usuario_id,))
                    ids.extend(int(r[0]) for r in cur.fetchall())

            ids = list(dict.fromkeys(ids))
            def contar(tabela, coluna_usuario, condicao=None, params=()):
                if not tem(tabela, coluna_usuario):
                    return None
                q = 'SELECT COUNT(*) FROM ' + identificador(tabela) + ' WHERE ' + identificador(coluna_usuario) + '::text = ANY(%s::text[])'
                if condicao:
                    q += ' AND ' + condicao
                cur.execute(q, ([str(i) for i in ids], *params))
                return int(cur.fetchone()[0])

            def distintos_usuarios(coluna):
                if not tem('intranet_usuarios', coluna):
                    return None
                cur.execute('SELECT COUNT(DISTINCT ' + identificador(coluna) + ') FROM intranet_usuarios WHERE id = ANY(%s) AND ativo=TRUE AND ' + identificador(coluna) + ' IS NOT NULL', (ids,))
                return int(cur.fetchone()[0])

            dados['funcionarios'] = contar('intranet_usuarios', 'id', 'ativo=TRUE')
            dados['gestores'] = contar('intranet_usuarios', 'id', 'ativo=TRUE AND tipo=2') if tem('intranet_usuarios','tipo') else None
            dados['departamentos'] = next((distintos_usuarios(c) for c in ('departamento','departamento_id')
                                           if tem('intranet_usuarios',c)), None)
            dados['centros_custo'] = next((distintos_usuarios(c) for c in ('centro_custo','centro_custo_id')
                                          if tem('intranet_usuarios',c)), None)
            dados['com_acesso'] = contar('intranet_usuarios','id', 'ativo=TRUE AND ultimo_acesso IS NOT NULL') if tem('intranet_usuarios','ultimo_acesso') else None

            # Participações e eventos com vínculo explícito à pessoa.
            for chave, tabela, coluna, cond in (
                ('treinamentos_pendentes','intranet_trein_participantes','usuario_id',"status='PENDENTE'"),
                ('treinamentos_concluidos','intranet_trein_participantes','usuario_id',"status='CONCLUIDO'"),
                ('avaliacoes_concluidas','intranet_aval_participantes','funcionario_id',"status='CONCLUIDO'"),
                ('avaliacoes_andamento','intranet_aval_participantes','funcionario_id',"status IN ('PENDENTE','EM_ANDAMENTO')"),
                ('avisos_pendentes','intranet_publicacoes_destinatarios','usuario_id','visualizado_em IS NULL'),
                ('comunicados_confirmacoes','intranet_publicacoes_destinatarios','usuario_id','confirmado_em IS NOT NULL'),
            ):
                dados[chave] = contar(tabela,coluna,cond) if tem(tabela,coluna,'status' if 'status' in cond else ('visualizado_em' if 'visualizado_em' in cond else 'confirmado_em')) else None

            # Treinamentos: inscrições exclusivamente dos IDs autorizados.
            # "Publicados" aqui são cursos publicados COM inscrição da equipe;
            # não representam o catálogo completo elegível ao usuário.
            chaves_trein = (
                'treinamentos_cadastrados', 'treinamentos_publicados_empresa',
                'treinamentos_publicados_ead', 'treinamentos_publicados_presencial',
                'treinamentos_inscricoes', 'treinamentos_pendentes',
                'treinamentos_concluidos', 'treinamentos_participacoes_andamento',
                'treinamentos_realizados', 'treinamentos_andamento',
                'treinamentos_disponiveis', 'horas_disponiveis',
                'horas_realizadas', 'horas_andamento', 'treinamentos_horas_mensal',
                'trein_ead_inscricoes', 'trein_ead_concluidos',
                'trein_ead_andamento', 'trein_ead_horas_gerais',
                'trein_presencial_inscricoes', 'trein_presencial_concluidos',
                'trein_presencial_andamento', 'trein_presencial_horas_gerais',
            )
            for chave in chaves_trein:
                dados[chave] = None
            if tem('intranet_trein_participantes', 'usuario_id', 'curso_id', 'status', 'concluido_em') and tem(
                'intranet_trein_cursos', 'id', 'status', 'modalidade', 'carga_horaria_min'
            ):
                cur.execute("""
                    SELECT
                        COUNT(*)::int,
                        COUNT(*) FILTER (WHERE p.status='PENDENTE')::int,
                        COUNT(*) FILTER (WHERE p.status='EM_ANDAMENTO')::int,
                        COUNT(*) FILTER (WHERE p.status='CONCLUIDO')::int,
                        COUNT(DISTINCT p.curso_id)::int,
                        COUNT(DISTINCT p.curso_id) FILTER (WHERE p.status='CONCLUIDO')::int,
                        COUNT(DISTINCT p.curso_id) FILTER (WHERE p.status='EM_ANDAMENTO')::int,
                        COUNT(DISTINCT p.curso_id) FILTER (WHERE c.status='PUBLICADO')::int,
                        COUNT(DISTINCT p.curso_id) FILTER (WHERE c.status='PUBLICADO' AND c.modalidade='EAD')::int,
                        COUNT(DISTINCT p.curso_id) FILTER (WHERE c.status='PUBLICADO' AND c.modalidade='PRESENCIAL')::int,
                        ROUND(COALESCE(SUM(c.carga_horaria_min) FILTER (WHERE p.status='CONCLUIDO'),0)/60.0,1),
                        ROUND(COALESCE(SUM(c.carga_horaria_min) FILTER (WHERE p.status='EM_ANDAMENTO'),0)/60.0,1)
                    FROM intranet_trein_participantes p
                    JOIN intranet_trein_cursos c ON c.id=p.curso_id
                    WHERE p.usuario_id=ANY(%s)
                """, (ids,))
                (dados['treinamentos_inscricoes'], dados['treinamentos_pendentes'],
                 dados['treinamentos_participacoes_andamento'], dados['treinamentos_concluidos'],
                 dados['treinamentos_cadastrados'], dados['treinamentos_realizados'],
                 dados['treinamentos_andamento'], dados['treinamentos_publicados_empresa'],
                 dados['treinamentos_publicados_ead'], dados['treinamentos_publicados_presencial'],
                 horas_concluidas, horas_em_andamento) = cur.fetchone()
                dados['horas_realizadas'] = float(horas_concluidas)
                dados['horas_andamento'] = float(horas_em_andamento)
                for modalidade, prefixo in (('EAD', 'ead'), ('PRESENCIAL', 'presencial')):
                    cur.execute("""
                        SELECT COUNT(*)::int,
                               COUNT(*) FILTER (WHERE p.status='CONCLUIDO')::int,
                               COUNT(*) FILTER (WHERE p.status='EM_ANDAMENTO')::int,
                               ROUND(COALESCE(SUM(c.carga_horaria_min) FILTER
                                   (WHERE c.status='PUBLICADO'),0)/60.0,1)
                        FROM intranet_trein_participantes p
                        JOIN intranet_trein_cursos c ON c.id=p.curso_id
                        WHERE p.usuario_id=ANY(%s) AND c.modalidade=%s
                    """, (ids, modalidade))
                    inscricoes, concluidos, andamento, _horas_inscricoes = cur.fetchone()
                    dados['trein_'+prefixo+'_inscricoes'] = inscricoes
                    dados['trein_'+prefixo+'_concluidos'] = concluidos
                    dados['trein_'+prefixo+'_andamento'] = andamento
                    # Carga horária de cursos publicados distintos com inscrição da equipe.
                    cur.execute("""
                        SELECT ROUND(COALESCE(SUM(x.carga_horaria_min),0)/60.0,1)
                        FROM (SELECT DISTINCT c.id, c.carga_horaria_min
                              FROM intranet_trein_participantes p
                              JOIN intranet_trein_cursos c ON c.id=p.curso_id
                              WHERE p.usuario_id=ANY(%s) AND c.modalidade=%s
                                AND c.status='PUBLICADO') x
                    """, (ids, modalidade))
                    dados['trein_'+prefixo+'_horas_gerais'] = float(cur.fetchone()[0])
                # Série mensal: cursos distintos concluídos no mês pela equipe,
                # cada curso contado uma vez por mês, como na visão corporativa.
                cur.execute("""
                    WITH meses AS (
                        SELECT generate_series(1, EXTRACT(MONTH FROM
                            NOW() AT TIME ZONE 'America/Sao_Paulo')::int) AS mes
                    ), cursos_mes AS (
                        SELECT DISTINCT EXTRACT(MONTH FROM
                                   (p.concluido_em AT TIME ZONE 'America/Sao_Paulo'))::int AS mes,
                               c.id AS curso_id, COALESCE(c.carga_horaria_min,0) AS minutos
                        FROM intranet_trein_participantes p
                        JOIN intranet_trein_cursos c ON c.id=p.curso_id
                        WHERE p.usuario_id=ANY(%s) AND p.status='CONCLUIDO'
                          AND p.concluido_em IS NOT NULL
                          AND (p.concluido_em AT TIME ZONE 'America/Sao_Paulo') >=
                              date_trunc('year', NOW() AT TIME ZONE 'America/Sao_Paulo')
                          AND (p.concluido_em AT TIME ZONE 'America/Sao_Paulo') <=
                              (NOW() AT TIME ZONE 'America/Sao_Paulo')
                    )
                    SELECT m.mes::int, ROUND(COALESCE(SUM(c.minutos),0)/60.0,1)
                    FROM meses m LEFT JOIN cursos_mes c ON c.mes=m.mes
                    GROUP BY m.mes ORDER BY m.mes
                """, (ids,))
                dados['treinamentos_horas_mensal'] = [
                    {'mes': int(m), 'horas': float(h)} for m, h in cur.fetchall()
                ]
            # "Disponíveis" exige regras de público do catálogo; não inventar.
            # Horas disponíveis dependem desse mesmo catálogo.

            # Série mensal: fotografia dos colaboradores da equipe atual em cada mês.
            # Não equivale à composição histórica da equipe, que exigiria vínculos datados.
            dados['colaboradores_mensal'] = None
            if tem('intranet_usuarios', 'id', 'data_admissao', 'data_demissao', 'ativo'):
                cur.execute("""
                    WITH hoje AS (
                        SELECT (NOW() AT TIME ZONE 'America/Sao_Paulo')::date AS data
                    ), meses AS (
                        SELECT g.mes::date AS inicio,
                               LEAST((g.mes + INTERVAL '1 month - 1 day')::date, h.data) AS referencia,
                               h.data AS hoje
                        FROM hoje h CROSS JOIN LATERAL generate_series(
                            date_trunc('year', h.data)::date,
                            date_trunc('month', h.data)::date,
                            INTERVAL '1 month') g(mes)
                    )
                    SELECT EXTRACT(MONTH FROM m.inicio)::int, m.referencia,
                           COUNT(u.id)::int
                    FROM meses m LEFT JOIN intranet_usuarios u
                      ON u.id = ANY(%s)
                     AND u.data_admissao IS NOT NULL
                     AND u.data_admissao <= m.referencia
                     AND (u.data_demissao IS NULL OR u.data_demissao > m.referencia)
                     AND (m.referencia <> m.hoje OR u.ativo = TRUE)
                    GROUP BY m.inicio, m.referencia ORDER BY m.inicio
                """, (ids,))
                dados['colaboradores_mensal'] = [
                    {'mes': mes, 'data': referencia.isoformat(), 'ativos': total}
                    for mes, referencia, total in cur.fetchall()
                ]

            # Pesquisas: apenas participações identificáveis e não anônimas.
            # Nunca cruzar respostas, envios ou metadados para inferir autoria anônima.
            dados['pesquisas_participacoes'] = None
            dados['pesquisas_com_respostas'] = None
            dados['pesquisas_publicadas'] = None
            dados['pesquisas_andamento'] = None
            dados['pesquisas_encerradas'] = None
            dados['pesquisas_departamentos'] = None
            if tem('pesq_participacao', 'usuario_id', 'pesquisa_id') and tem('pesq_pesquisas', 'id', 'anonima', 'status', 'inicio_em', 'encerramento_em'):
                cur.execute("""
                    SELECT COUNT(*)::int, COUNT(DISTINCT p.pesquisa_id)::int
                    FROM pesq_participacao p
                    JOIN pesq_pesquisas q ON q.id = p.pesquisa_id
                    WHERE p.usuario_id = ANY(%s) AND q.anonima IS FALSE
                """, (ids,))
                dados['pesquisas_participacoes'], dados['pesquisas_com_respostas'] = cur.fetchone()
                # Contagem de pesquisas distintas já respondidas pela equipe,
                # segmentadas pelo estado atual; não é catálogo de pesquisas elegíveis.
                cur.execute("""
                    SELECT
                      COUNT(DISTINCT q.id) FILTER (WHERE q.status='PUBLICADA')::int,
                      COUNT(DISTINCT q.id) FILTER (WHERE q.status='PUBLICADA'
                        AND (q.inicio_em IS NULL OR q.inicio_em<=NOW())
                        AND (q.encerramento_em IS NULL OR q.encerramento_em>=NOW()))::int,
                      COUNT(DISTINCT q.id) FILTER (WHERE q.status='ENCERRADA'
                        OR (q.status='PUBLICADA' AND q.encerramento_em<NOW()))::int
                    FROM pesq_participacao p
                    JOIN pesq_pesquisas q ON q.id=p.pesquisa_id
                    WHERE p.usuario_id=ANY(%s) AND q.anonima IS FALSE
                """, (ids,))
                (dados['pesquisas_publicadas'], dados['pesquisas_andamento'],
                 dados['pesquisas_encerradas']) = cur.fetchone()

            # Avaliações: funcionário avaliado é funcionario_id, não usuario_id.
            # Métricas agregadas de status não expõem notas nem relatórios individuais.
            dados['avaliacoes_pendentes'] = contar('intranet_aval_participantes', 'funcionario_id',
                                                   "status='PENDENTE'") if tem('intranet_aval_participantes','status') else None
            dados['avaliacoes_90'] = None
            dados['avaliacoes_180'] = None
            if tem('intranet_aval_participantes','funcionario_id','ciclo_id') and tem('intranet_aval_ciclos','id','modalidade'):
                cur.execute("""
                    SELECT COUNT(DISTINCT c.id) FILTER (WHERE c.modalidade=90)::int,
                           COUNT(DISTINCT c.id) FILTER (WHERE c.modalidade=180)::int
                    FROM intranet_aval_participantes p
                    JOIN intranet_aval_ciclos c ON c.id=p.ciclo_id
                    WHERE p.funcionario_id=ANY(%s)
                """, (ids,))
                dados['avaliacoes_90'], dados['avaliacoes_180'] = cur.fetchone()

            # Resultados consolidados: somente participantes autorizados.
            # O gestor ve os seus subordinados DIRETOS; para o proprio funcionario,
            # o relatorio deve ter sido liberado explicitamente.
            for chave in ('avaliacoes_media', 'avaliacoes_meta_esperada',
                          'aval_comp_obtida', 'aval_comp_esperada',
                          'aval_metas_obtida', 'aval_metas_esperada'):
                dados[chave] = None
            if all(tem(t, *cols) for t, cols in [
                ('intranet_aval_participantes', ('id', 'funcionario_id', 'gestor_id', 'status')),
                ('intranet_aval_relatorios', ('participante_id', 'consolidado_em', 'liberado_em')),
                ('intranet_aval_avaliacoes', ('id', 'participante_id', 'status')),
                ('intranet_aval_respostas', ('avaliacao_id', 'criterio_id', 'nota', 'percentual_atingimento')),
                ('intranet_aval_criterios', ('id', 'tipo', 'nota_esperada')),
            ]):
                # A verificacao de gestor_id evita expor resultados de funcionarios
                # que estejam na equipe atual mas tenham outro gestor na avaliacao.
                cur.execute("""
                    WITH base AS (
                        SELECT p.id AS participante_id, c.id AS criterio_id, c.tipo,
                               CASE WHEN c.tipo='CRITERIO' THEN r.nota::numeric / 5
                                    WHEN c.tipo='META' THEN
                                        CASE WHEN r.percentual_atingimento >= 100 THEN 1::numeric
                                             WHEN r.percentual_atingimento IS NOT NULL THEN 0::numeric
                                        END
                               END AS obtido,
                               CASE WHEN c.tipo='CRITERIO' THEN c.nota_esperada::numeric / 5
                                    WHEN c.tipo='META' THEN 1::numeric END AS esperado
                        FROM intranet_aval_participantes p
                        JOIN intranet_aval_relatorios rel
                          ON rel.participante_id=p.id AND rel.consolidado_em IS NOT NULL
                        JOIN intranet_aval_avaliacoes a
                          ON a.participante_id=p.id AND a.status='CONCLUIDA'
                        JOIN intranet_aval_respostas r ON r.avaliacao_id=a.id
                        JOIN intranet_aval_criterios c ON c.id=r.criterio_id
                        WHERE p.status='CONCLUIDO' AND c.tipo IN ('CRITERIO','META')
                          AND p.funcionario_id=ANY(%s)
                          AND ((p.funcionario_id=%s AND rel.liberado_em IS NOT NULL)
                               OR (p.funcionario_id<>%s AND p.gestor_id=%s))
                    ), por_criterio AS (
                        SELECT participante_id, criterio_id, tipo,
                               AVG(obtido) AS obtido, AVG(esperado) AS esperado
                        FROM base WHERE obtido IS NOT NULL AND esperado IS NOT NULL
                        GROUP BY participante_id, criterio_id, tipo
                    ), por_participante AS (
                        SELECT participante_id, tipo, AVG(obtido) AS obtido,
                               AVG(esperado) AS esperado
                        FROM por_criterio GROUP BY participante_id, tipo
                    )
                    SELECT tipo, ROUND(AVG(obtido)*100,1), ROUND(AVG(esperado)*100,1)
                    FROM por_participante GROUP BY tipo
                """, (ids, usuario_id, usuario_id, usuario_id))
                grupos = {tipo: (float(o), float(e)) for tipo, o, e in cur.fetchall()
                          if o is not None and e is not None}
                if 'CRITERIO' in grupos:
                    dados['aval_comp_obtida'], dados['aval_comp_esperada'] = grupos['CRITERIO']
                if 'META' in grupos:
                    dados['aval_metas_obtida'], dados['aval_metas_esperada'] = grupos['META']
                if grupos:
                    dados['avaliacoes_media'] = round(sum(v[0] for v in grupos.values()) / len(grupos), 1)
                    dados['avaliacoes_meta_esperada'] = round(sum(v[1] for v in grupos.values()) / len(grupos), 1)

            # Auditoria: contagem de eventos da própria pessoa/equipe, não de toda empresa.
            if tem('intranet_auditoria','usuario_id','tipo_evento','acao','resultado','criado_em'):
                cur.execute("""SELECT COUNT(*) FROM intranet_auditoria
                               WHERE usuario_id=ANY(%s) AND tipo_evento='AUTENTICACAO'
                                 AND acao='LOGIN' AND resultado='SUCESSO'
                                 AND (criado_em AT TIME ZONE 'America/Sao_Paulo') >=
                                     date_trunc('year', NOW() AT TIME ZONE 'America/Sao_Paulo')""", (ids,))
                dados['intranet_logins_ano'] = int(cur.fetchone()[0])
                cur.execute("""SELECT COUNT(DISTINCT a.usuario_id) FROM intranet_auditoria a
                               JOIN intranet_usuarios u ON u.id=a.usuario_id AND u.ativo=TRUE
                               WHERE a.usuario_id=ANY(%s) AND a.tipo_evento='AUTENTICACAO'
                                 AND a.acao='LOGIN' AND a.resultado='SUCESSO'
                                 AND (a.criado_em AT TIME ZONE 'America/Sao_Paulo') >=
                                     date_trunc('year', NOW() AT TIME ZONE 'America/Sao_Paulo')""", (ids,))
                n = int(cur.fetchone()[0])
                dados['intranet_indice_acesso'] = round(100*n/dados['funcionarios'],1) if dados['funcionarios'] else None
                cur.execute("""SELECT EXTRACT(MONTH FROM criado_em AT TIME ZONE 'America/Sao_Paulo')::int, COUNT(*)::int
                               FROM intranet_auditoria WHERE usuario_id=ANY(%s)
                               AND tipo_evento='AUTENTICACAO' AND acao='LOGIN' AND resultado='SUCESSO'
                               AND (criado_em AT TIME ZONE 'America/Sao_Paulo') >=
                                   date_trunc('year', NOW() AT TIME ZONE 'America/Sao_Paulo')
                               GROUP BY 1 ORDER BY 1""", (ids,))
                contagens = dict(cur.fetchall())
                cur.execute("SELECT EXTRACT(MONTH FROM NOW() AT TIME ZONE 'America/Sao_Paulo')::int")
                mes_atual = cur.fetchone()[0]
                dados['intranet_logins_mensal'] = [{'mes':m,'logins':contagens.get(m,0)} for m in range(1,mes_atual+1)]

                if tem('intranet_auditoria','modulo'):
                    for chave, modulo in [('galeria_acessos','GALERIA'),('videos_acessos','VIDEOS')]:
                        cur.execute("""SELECT COUNT(*) FROM intranet_auditoria
                                       WHERE usuario_id=ANY(%s) AND tipo_evento='NAVEGACAO'
                                       AND acao='ACESSO_MODULO' AND modulo=%s""", (ids,modulo))
                        dados[chave] = int(cur.fetchone()[0])
                    cur.execute("""SELECT COUNT(*) FROM intranet_auditoria WHERE usuario_id=ANY(%s)
                                   AND tipo_evento='DOCUMENTO' AND acao='DOWNLOAD' AND modulo='DOCUMENTOS'""", (ids,))
                    dados['documentos_downloads'] = int(cur.fetchone()[0])
                    cur.execute("""SELECT COUNT(*) FROM intranet_auditoria WHERE usuario_id=ANY(%s)
                                   AND tipo_evento='DOCUMENTO' AND acao='VISUALIZACAO_PDF' AND modulo='DOCUMENTOS'""", (ids,))
                    dados['documentos_visualizacoes'] = int(cur.fetchone()[0])

            # Políticas e Documentos: indicador quantitativo, sem conceder acesso a arquivos.
            # Nivel 1 permanece sem acesso ao modulo e nao recebe contagem.
            # Nivel 2: documentos dos titulos cujo nome corresponde ao departamento
            # de algum colaborador ativo no escopo (proprio usuario + equipe direta).
            # Os JOINs usam a estrutura confirmada: arquivo -> tipo -> titulo.
            dados['documentos_disponiveis'] = None
            if int(usuario[3]) == 2 and all((
                tem('intranet_usuarios', 'id', 'departamento', 'ativo'),
                tem('intranet_doc_arquivos', 'id', 'tipo_id'),
                tem('intranet_doc_tipos', 'id', 'titulo_id'),
                tem('intranet_doc_titulos', 'id', 'nome'),
            )):
                cur.execute("""
                    SELECT COUNT(DISTINCT a.id)::int
                    FROM intranet_doc_arquivos a
                    JOIN intranet_doc_tipos tp ON tp.id = a.tipo_id
                    JOIN intranet_doc_titulos t ON t.id = tp.titulo_id
                    WHERE EXISTS (
                        SELECT 1 FROM intranet_usuarios u
                        WHERE u.id = ANY(%s) AND u.ativo = TRUE
                          AND u.departamento IS NOT NULL
                          AND UPPER(BTRIM(u.departamento::text)) = UPPER(BTRIM(t.nome::text))
                    )
                """, (ids,))
                dados['documentos_disponiveis'] = int(cur.fetchone()[0])
            # Nao alterar permissoes de acesso ao modulo ou aos arquivos.
            # Pesquisas anônimas nunca são atribuídas a pessoas por cruzamento.
            # Avaliações consolidadas requerem o mesmo filtro em toda a cadeia de joins.
            # Até validação dessas relações, indicadores ficam indisponíveis.

    payload = json.dumps(dados, ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    html = html.replace('● Executivo · Nível 3', '● Meu status' if visao == 'pessoal' else '● Minha equipe')
    return html.replace(MARCADOR, '<script type="application/json" id="megavig360-dados">'+payload+'</script>', 1)
