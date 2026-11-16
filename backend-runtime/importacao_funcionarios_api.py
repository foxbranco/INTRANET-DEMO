"""Importação de funcionários: validação e confirmação transacional."""
import io
import hashlib
import json
import bcrypt
import re
from collections import Counter
from datetime import datetime, date
from typing import Optional
from fastapi import APIRouter, UploadFile, File, Header, HTTPException, Form
from openpyxl import load_workbook

router_importacao_funcionarios = APIRouter(prefix="/api/admin/importacao-funcionarios", tags=["Importação de funcionários"])
_autenticar = None
_conectar = None

CAMPOS = ["CPF", "Matrícula", "Nome completo", "Data de nascimento", "Data de admissão",
          "Data de demissão", "E-mail", "Telefone", "Departamento", "Nome do centro de custo",
          "Cargo", "CPF do gestor", "Nome do gestor"]
OBRIGATORIOS = {0, 1, 2, 3, 4, 8, 9, 10}

def configurar_importacao_funcionarios(conectar, autenticar):
    global _conectar, _autenticar
    _conectar, _autenticar = conectar, autenticar

def admin(authorization):
    if not _autenticar:
        raise HTTPException(503, "Módulo não configurado")
    u = _autenticar(authorization)
    if u[5] or int(u[3]) != 3:
        raise HTTPException(403, "Acesso exclusivo do administrador nível 3")
    return u

def cpf_limpo(valor):
    if valor is None:
        return ""
    if isinstance(valor, float) and not valor.is_integer():
        return ""
    s = str(int(valor)) if isinstance(valor, (int, float)) else str(valor).strip()
    return re.sub(r"\D", "", s)

def cpf_valido(s):
    if len(s) != 11 or s == s[0]*11:
        return False
    for tamanho in (9, 10):
        soma = sum(int(s[i]) * (tamanho + 1 - i) for i in range(tamanho))
        digito = (soma * 10) % 11
        if digito == 10: digito = 0
        if digito != int(s[tamanho]): return False
    return True

def data_valida(valor):
    if isinstance(valor, datetime): return valor.date()
    if isinstance(valor, date): return valor
    if isinstance(valor, str) and valor.strip():
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try: return datetime.strptime(valor.strip(), fmt).date()
            except ValueError: pass
    return None

COLUNAS_COMPARACAO = ("matricula", "nome", "data_nascimento", "data_admissao", "data_demissao", "email", "telefone", "departamento", "centro_custo", "cargo", "ativo")
ROTULOS = {"matricula":"Matrícula", "nome":"Nome completo", "data_nascimento":"Data de nascimento", "data_admissao":"Data de admissão", "data_demissao":"Data de demissão", "email":"E-mail", "telefone":"Telefone", "departamento":"Departamento", "centro_custo":"Nome do centro de custo", "cargo":"Cargo", "ativo":"Situação", "cpf_gestor":"CPF do gestor"}

def dados_planilha(v):
    return {"matricula":str(v[1]).strip(), "nome":str(v[2]).strip(),
            "data_nascimento":data_valida(v[3]), "data_admissao":data_valida(v[4]),
            "data_demissao":data_valida(v[5]),
            "email":str(v[6]).strip() if v[6] is not None else None,
            "telefone":str(v[7]).strip() if v[7] is not None else None,
            "departamento":str(v[8]).strip(), "centro_custo":str(v[9]).strip(),
            "cargo":str(v[10]).strip(), "ativo":data_valida(v[5]) is None,
            "cpf_gestor":cpf_limpo(v[11])}

def comparar_registro(novo, atual):
    mudancas=[]
    for campo in COLUNAS_COMPARACAO:
        valor=novo[campo]
        # Campos opcionais vazios não sobrescrevem o cadastro; demissão vazia significa ativo.
        if campo in ("email", "telefone") and not valor:
            continue
        if campo in ("data_nascimento", "data_admissao") and valor is None:
            continue
        antigo=atual.get(campo)
        if campo in ("email", "telefone"):
            antigo=antigo or None
            valor=valor or None
        if antigo != valor:
            mudancas.append({"campo":ROTULOS[campo], "anterior":str(antigo) if antigo is not None else "(vazio)", "novo":str(valor) if valor is not None else "(vazio)"})
    if novo["cpf_gestor"]:
        if novo["cpf_gestor"] != (atual.get("cpf_gestor") or ""):
            mudancas.append({"campo":"CPF do gestor", "anterior":atual.get("cpf_gestor") or "(sem gestor)", "novo":novo["cpf_gestor"]})
    return mudancas

def consultar_existentes(cur, cpfs):
    if not cpfs:return {}
    cur.execute("""SELECT u.id,u.cpf,u.matricula,u.nome,u.data_nascimento,u.data_admissao,
        u.data_demissao,u.email,u.telefone,u.departamento,u.centro_custo,u.cargo,u.ativo,
        g.cpf FROM intranet_usuarios u
        LEFT JOIN intranet_gestores_funcionarios v ON v.funcionario_id=u.id AND v.ativo=TRUE
        LEFT JOIN intranet_usuarios g ON g.id=v.gestor_id WHERE u.cpf=ANY(%s)""", (cpfs,))
    campos=("id","cpf","matricula","nome","data_nascimento","data_admissao","data_demissao","email","telefone","departamento","centro_custo","cargo","ativo","cpf_gestor")
    return {r[1]:dict(zip(campos,r)) for r in cur.fetchall()}

@router_importacao_funcionarios.post("/validar")
async def validar(arquivo: UploadFile = File(...), authorization: Optional[str] = Header(None)):
    admin(authorization)
    if not (arquivo.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(400, "Envie um arquivo .xlsx")
    conteudo = await arquivo.read(8*1024*1024+1)
    await arquivo.close()
    if len(conteudo) > 8*1024*1024:
        raise HTTPException(413, "Arquivo maior que 8 MB")
    try:
        wb = load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
        ws = wb.active
        cabecalho = [re.sub(r"\s*\*\s*$", "", str(x.value or "").strip()) for x in next(ws.iter_rows(min_row=1, max_row=1))]
        if cabecalho != CAMPOS:
            raise HTTPException(400, "Cabeçalho diferente do modelo oficial de 13 colunas")
        linhas = []
        for numero, valores in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
            if not any(v is not None and str(v).strip() for v in valores): continue
            linhas.append((numero, list(valores)))
            if len(linhas) > 2000:
                raise HTTPException(400, "Limite máximo: 2.000 funcionários")
    except HTTPException: raise
    except Exception:
        raise HTTPException(400, "Excel inválido ou corrompido")
    finally:
        try: wb.close()
        except Exception: pass

    cpfs = Counter(cpf_limpo(v[0]) for _, v in linhas)
    problemas, aceitas = [], []
    hoje = date.today()
    for numero, v in linhas:
        erros = []
        for i in OBRIGATORIOS:
            if v[i] is None or not str(v[i]).strip():
                erros.append((CAMPOS[i], "Campo obrigatório vazio"))
        cpf = cpf_limpo(v[0])
        if not cpf_valido(cpf): erros.append(("CPF", "CPF inválido"))
        if cpf and cpfs[cpf] > 1: erros.append(("CPF", "CPF repetido no arquivo"))
        nascimento, admissao, demissao = (data_valida(v[i]) for i in (3,4,5))
        for idx, dt in [(3,nascimento),(4,admissao),(5,demissao)]:
            if v[idx] is not None and str(v[idx]).strip() and dt is None:
                erros.append((CAMPOS[idx], "Data inválida"))
        if nascimento and nascimento > hoje: erros.append(("Data de nascimento", "Data futura"))
        if admissao and nascimento and admissao < nascimento: erros.append(("Data de admissão", "Anterior ao nascimento"))
        if demissao and admissao and demissao < admissao: erros.append(("Data de demissão", "Anterior à admissão"))
        gestor = cpf_limpo(v[11])
        if gestor and not cpf_valido(gestor): erros.append(("CPF do gestor", "CPF do gestor inválido"))
        if gestor and gestor == cpf: erros.append(("CPF do gestor", "Funcionário não pode ser seu próprio gestor"))
        for idx, limite in [(1,30),(2,160),(6,150),(7,20),(8,100),(9,100),(10,150)]:
            if v[idx] is not None and len(str(v[idx]).strip()) > limite:
                erros.append((CAMPOS[idx], f"Máximo de {limite} caracteres"))
        if v[6] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(v[6]).strip()):
            erros.append(("E-mail", "E-mail inválido"))
        if erros:
            problemas.extend({"linha": numero, "cpf": cpf, "campo": campo, "mensagem": msg}
                             for campo, msg in erros)
        else:
            aceitas.append({"linha": numero, "cpf": cpf, "nome": str(v[2]).strip(),
                            "cpf_gestor": gestor, "situacao": "INATIVO" if demissao else "ATIVO"})
    # Verificação de existência do gestor, após validar todos os CPFs do arquivo.
    cpfs_arquivo = {x["cpf"] for x in aceitas}
    externos = {x["cpf_gestor"] for x in aceitas if x["cpf_gestor"] and x["cpf_gestor"] not in cpfs_arquivo}
    existentes = set()
    if externos:
        with _conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT cpf FROM intranet_usuarios WHERE cpf = ANY(%s) AND ativo = TRUE", (list(externos),))
                existentes = {r[0] for r in cur.fetchall()}
    aceitas_final = []
    for x in aceitas:
        if x["cpf_gestor"] in externos and x["cpf_gestor"] not in existentes:
            problemas.append({"linha": x["linha"], "cpf": x["cpf"],
                              "campo": "CPF do gestor", "mensagem": "Gestor não localizado; corrigir e reenviar"})
        else:
            aceitas_final.append(x)
    hash_arquivo = hashlib.sha256(conteudo).hexdigest()
    with _conectar() as conn:
        with conn.cursor() as cur:
            existentes_db = consultar_existentes(cur, [x["cpf"] for x in aceitas_final])
    mapa_linhas = {cpf_limpo(v[0]):v for _,v in linhas}
    novos=atualizacoes=sem_alteracoes=0
    detalhes=[]
    for x in aceitas_final:
        anterior=existentes_db.get(x["cpf"])
        if anterior is None:
            novos+=1
            x["acao"]="NOVO"
        else:
            mudancas=comparar_registro(dados_planilha(mapa_linhas[x["cpf"]]), anterior)
            x["acao"]="ATUALIZAR" if mudancas else "SEM_ALTERACOES"
            if mudancas:
                atualizacoes+=1
                detalhes.append({"linha":x["linha"],"cpf":x["cpf"],"nome":x["nome"],"alteracoes":mudancas})
            else:
                sem_alteracoes+=1
    mensagem=(f"Prévia sem gravação: {novos} novos, {atualizacoes} para atualizar "
              f"e {sem_alteracoes} sem alterações.")
    return {"ja_importado":False, "novos":novos, "atualizar":atualizacoes,
            "sem_alteracoes":sem_alteracoes, "detalhes_alteracoes":detalhes,
            "pode_confirmar":bool(novos or atualizacoes), "mensagem":mensagem,
            "arquivo":arquivo.filename,"total":len(linhas),"validos":len(aceitas_final),
            "rejeitados":len({p["linha"] for p in problemas}),"erros":problemas,
            "previa":aceitas_final[:100],"hash_validado":hash_arquivo,"modo":"VALIDACAO_SEM_GRAVACAO"}


@router_importacao_funcionarios.post("/confirmar")
async def confirmar_importacao(
    arquivo: UploadFile = File(...),
    hash_validado: str = Form(...),
    authorization: Optional[str] = Header(None),
):
    """Grava apenas um arquivo idêntico ao validado, integralmente válido.

    Uma transação inclui funcionários, promoção de gestores e vínculos.
    """
    administrador = admin(authorization)
    if not re.fullmatch(r"[a-f0-9]{64}", hash_validado):
        raise HTTPException(400, "Identificador de validação inválido")
    conteudo = await arquivo.read(8 * 1024 * 1024 + 1)
    await arquivo.close()
    if len(conteudo) > 8 * 1024 * 1024:
        raise HTTPException(413, "Arquivo maior que 8 MB")
    if hashlib.sha256(conteudo).hexdigest() != hash_validado:
        raise HTTPException(409, "Arquivo modificado desde a validação. Valide novamente.")
    copia = UploadFile(filename=arquivo.filename, file=io.BytesIO(conteudo))
    previa = await validar(copia, authorization)
    if not previa.get("pode_confirmar"):
        raise HTTPException(409, "Nenhum cadastro novo ou alterado. Não há o que importar.")
    if previa["rejeitados"] or not previa["total"]:
        raise HTTPException(409, "Arquivo contém erros ou está vazio. Corrija e valide novamente.")
    try:
        wb = load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
        ws = wb.active
        registros = []
        for valores in ws.iter_rows(min_row=2, values_only=True):
            if not any(v is not None and str(v).strip() for v in valores):
                continue
            v = list(valores)
            registros.append({
                "cpf": cpf_limpo(v[0]), "matricula": str(v[1]).strip(),
                "nome": str(v[2]).strip(), "nascimento": data_valida(v[3]),
                "admissao": data_valida(v[4]), "demissao": data_valida(v[5]),
                "email": str(v[6]).strip() if v[6] is not None else None,
                "telefone": str(v[7]).strip() if v[7] is not None else None,
                "departamento": str(v[8]).strip(),
                "centro_custo": str(v[9]).strip(), "cargo": str(v[10]).strip(),
                "gestor": cpf_limpo(v[11]),
            })
    finally:
        try: wb.close()
        except Exception: pass
    # Bloqueio de concorrência entre importações, com transação única.
    try:
        with _conectar() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(48201112)")
                cpfs = [r["cpf"] for r in registros]
                cur.execute("SELECT id, cpf FROM intranet_usuarios WHERE cpf = ANY(%s) FOR UPDATE", (cpfs,))
                existentes = {cpf: uid for uid, cpf in cur.fetchall()}
                anteriores = consultar_existentes(cur, cpfs)
                ids = dict(existentes)
                inseridos = atualizados = 0
                for r in registros:
                    ativo = r["demissao"] is None
                    if r["cpf"] in existentes:
                        uid = existentes[r["cpf"]]
                        novo = {"matricula":r["matricula"],"nome":r["nome"],
                                "data_nascimento":r["nascimento"],"data_admissao":r["admissao"],
                                "data_demissao":r["demissao"],"email":r["email"],
                                "telefone":r["telefone"],"departamento":r["departamento"],
                                "centro_custo":r["centro_custo"],"cargo":r["cargo"],
                                "ativo":ativo,"cpf_gestor":r["gestor"]}
                        if not comparar_registro(novo, anteriores[r["cpf"]]):
                            ids[r["cpf"]] = uid
                            continue
                        cur.execute("""
                            UPDATE intranet_usuarios SET
                              matricula=COALESCE(NULLIF(%s,''),matricula), nome=%s,
                              data_nascimento=COALESCE(%s,data_nascimento), data_admissao=COALESCE(%s,data_admissao), data_demissao=%s,
                              email=COALESCE(NULLIF(%s,''),email),
                              telefone=COALESCE(NULLIF(%s,''),telefone),
                              departamento=%s, centro_custo=%s, cargo=%s,
                              ativo=%s, atualizado_em=NOW()
                            WHERE id=%s
                        """, (r["matricula"], r["nome"], r["nascimento"],
                              r["admissao"], r["demissao"], r["email"], r["telefone"],
                              r["departamento"], r["centro_custo"], r["cargo"], ativo, uid))
                        atualizados += 1
                    else:
                        senha_hash = bcrypt.hashpw(r["cpf"].encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
                        cur.execute("""
                            INSERT INTO intranet_usuarios
                            (cpf, matricula, nome, data_nascimento, data_admissao,
                             data_demissao, email, telefone, departamento, centro_custo,
                             cargo, ativo, nivel_acesso, tipo, senha_hash, trocar_senha)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,1,%s,TRUE)
                            RETURNING id
                        """, (r["cpf"], r["matricula"], r["nome"], r["nascimento"],
                              r["admissao"], r["demissao"], r["email"], r["telefone"],
                              r["departamento"], r["centro_custo"], r["cargo"], ativo, senha_hash))
                        uid = cur.fetchone()[0]
                        inseridos += 1
                    ids[r["cpf"]] = uid
                gestores = {r["gestor"] for r in registros if r["gestor"]}
                externos = list(gestores - set(ids))
                if externos:
                    cur.execute("SELECT cpf,id FROM intranet_usuarios WHERE cpf=ANY(%s) AND ativo=TRUE", (externos,))
                    ids.update({cpf: uid for cpf, uid in cur.fetchall()})
                if not gestores.issubset(ids):
                    raise HTTPException(409, "Gestor não encontrado. Valide novamente.")
                if gestores:
                    cur.execute("UPDATE intranet_usuarios SET tipo=2 WHERE id=ANY(%s)", ([ids[cpf] for cpf in gestores],))
                vinculos = 0
                for r in registros:
                    funcionario = ids[r["cpf"]]
                    if not r["gestor"]:
                        continue  # Gestor em branco preserva vínculo atual
                    gestor = ids.get(r["gestor"]) if r["gestor"] else None
                    cur.execute("""
                        SELECT id, gestor_id FROM intranet_gestores_funcionarios
                        WHERE funcionario_id=%s AND ativo=TRUE FOR UPDATE
                    """, (funcionario,))
                    atual = cur.fetchone()
                    if atual and atual[1] == gestor:
                        continue
                    if atual:
                        # Avaliações em andamento exigem transferência específica;
                        # nunca alterar silenciosamente vínculos referenciados.
                        cur.execute("""
                            SELECT 1 FROM intranet_aval_participantes
                            WHERE vinculo_gestor_id=%s LIMIT 1
                        """, (atual[0],))
                        if cur.fetchone():
                            raise HTTPException(409, "Há avaliação vinculada a gestor anterior; mudança requer tratamento específico.")
                        cur.execute("""
                            UPDATE intranet_gestores_funcionarios
                            SET ativo=FALSE, fim_em=NOW(), alterado_por=%s
                            WHERE id=%s
                        """, (administrador[0], atual[0]))
                    if gestor is not None:
                        if gestor == funcionario:
                            raise HTTPException(409, "Funcionário não pode ser seu próprio gestor")
                        cur.execute("""
                            WITH RECURSIVE cadeia(id, caminho) AS (
                              SELECT %s::bigint, ARRAY[%s::bigint]
                              UNION ALL
                              SELECT g.gestor_id, cadeia.caminho || g.gestor_id
                              FROM cadeia JOIN intranet_gestores_funcionarios g
                                ON g.funcionario_id=cadeia.id AND g.ativo=TRUE
                              WHERE NOT g.gestor_id=ANY(cadeia.caminho)
                            ) SELECT 1 FROM cadeia WHERE id=%s LIMIT 1
                        """, (gestor, gestor, funcionario))
                        if cur.fetchone():
                            raise HTTPException(409, "Hierarquia circular de gestores detectada")
                        cur.execute("""
                            INSERT INTO intranet_gestores_funcionarios
                            (funcionario_id, gestor_id, alterado_por)
                            VALUES (%s,%s,%s)
                        """, (funcionario, gestor, administrador[0]))
                        vinculos += 1
                if not (inseridos or atualizados or vinculos):
                    raise HTTPException(409, "Nenhuma alteração encontrada. Importe somente quando houver mudanças.")
                # Auditoria na MESMA transação: se o log falhar, nada é gravado.
                # Adapta os metadados às colunas existentes na tabela de auditoria.
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='intranet_auditoria'
                """)
                colunas_auditoria = {linha[0] for linha in cur.fetchall()}
                detalhes = {
                    "arquivo": arquivo.filename,
                    "sha256": hash_validado,
                    "total": len(registros),
                    "inseridos": inseridos,
                    "atualizados": atualizados,
                    "vinculos_criados": vinculos,
                    "administrador_id": administrador[0],
                }
                campos = ["usuario_id", "sessao_id", "tipo_evento", "acao", "modulo", "resultado"]
                valores = [administrador[0], str(administrador[6]), "IMPORTACAO",
                           "IMPORTACAO_FUNCIONARIOS", "ADMIN", "SUCESSO"]
                if "recurso" in colunas_auditoria:
                    campos.append("recurso")
                    valores.append((arquivo.filename or "planilha.xlsx")[:200])
                if "detalhes" in colunas_auditoria:
                    campos.append("detalhes")
                    valores.append(json.dumps(detalhes, ensure_ascii=False))
                if "recurso_id" in colunas_auditoria:
                    campos.append("recurso_id")
                    valores.append(hash_validado)
                marcadores = ", ".join(["%s"] * len(campos))
                cur.execute(
                    f"INSERT INTO intranet_auditoria ({', '.join(campos)}) VALUES ({marcadores})",
                    valores,
                )
        return {"mensagem": "Importação concluída com sucesso.",
                "inseridos": inseridos, "atualizados": atualizados,
                "vinculos_criados": vinculos, "sem_alteracoes":len(registros)-inseridos-atualizados,
                "total": len(registros)}
    except HTTPException:
        raise
    except Exception as exc:
        # Nunca expor detalhes internos do banco ao navegador.
        raise HTTPException(409, "Importação cancelada integralmente por conflito ou inconsistência no banco") from exc


@router_importacao_funcionarios.get("/historico")
def historico_importacoes(authorization: Optional[str] = Header(None)):
    admin(authorization)
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.id, a.criado_em, a.usuario_id, u.nome,
                       a.resultado, a.detalhes
                FROM intranet_auditoria a
                LEFT JOIN intranet_usuarios u ON u.id = a.usuario_id
                WHERE a.modulo = 'ADMIN'
                  AND a.acao = 'IMPORTACAO_FUNCIONARIOS'
                ORDER BY a.id DESC LIMIT 50
            """)
            registros = cur.fetchall()
    saida = []
    for identificador, data, usuario_id, nome, resultado, detalhes in registros:
        if isinstance(detalhes, str):
            try:
                detalhes = json.loads(detalhes)
            except (ValueError, TypeError):
                detalhes = {}
        if not isinstance(detalhes, dict):
            detalhes = {}
        saida.append({
            "id": identificador,
            "data": data.isoformat() if data else None,
            "administrador": nome or f"ID {usuario_id}",
            "arquivo": detalhes.get("arquivo", ""),
            "total": detalhes.get("total", 0),
            "inseridos": detalhes.get("inseridos", 0),
            "atualizados": detalhes.get("atualizados", 0),
            "vinculos_criados": detalhes.get("vinculos_criados", 0),
            "resultado": resultado,
        })
    return {"historico": saida}
