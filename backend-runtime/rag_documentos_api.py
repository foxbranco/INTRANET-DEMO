"""Busca textual gratuita nos documentos cadastrados. Não é geração de respostas por LLM."""
from fastapi import APIRouter, Header, HTTPException, Query
from pathlib import Path
from functools import lru_cache
import re, unicodedata, zipfile, xml.etree.ElementTree as ET
from typing import Optional

router_rag_documentos = APIRouter()
_conectar = _exigir_nivel = None
BASE = Path('/intranet/documentos_upload').resolve()

def configurar_rag_documentos(conectar, exigir_nivel):
    global _conectar, _exigir_nivel
    _conectar, _exigir_nivel = conectar, exigir_nivel

def normalizar(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', s.lower()) if not unicodedata.combining(c))

@lru_cache(maxsize=256)
def extrair(caminho, mtime, tamanho):
    p = Path(caminho)
    ext = p.suffix.lower()
    paginas = []
    if ext == '.pdf':
        try:
            from pypdf import PdfReader
        except ImportError:
            return []  # requer pypdf no container
        try:
            leitor = PdfReader(str(p), strict=False)
            for i, pagina in enumerate(leitor.pages):
                paginas.append((i + 1, (pagina.extract_text() or '')[:100000]))
        except Exception:
            return []
    elif ext in ('.txt', '.csv'):
        try:
            paginas = [(None, p.read_text(encoding='utf-8', errors='replace')[:300000])]
        except Exception:
            return []
    elif ext == '.docx':
        try:
            with zipfile.ZipFile(p) as z:
                root = ET.fromstring(z.read('word/document.xml'))
                ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
                paginas = [(None, ' '.join(n.text or '' for n in root.iter(ns+'t'))) ]
        except Exception:
            return []
    return paginas

@router_rag_documentos.get('/api/documentos/busca-conteudo')
def busca_conteudo(q: str = Query(..., min_length=3, max_length=180), authorization: Optional[str] = Header(None)):
    _exigir_nivel(authorization, 2)
    termos = [t for t in re.findall(r'\w+', normalizar(q)) if len(t) > 2 and t not in {'qual','quais','como','para','sobre','uma','das','dos','que','com','por','onde','quando','seria','sao','tem','ter','meu','minha'}]
    if not termos:
        return {'resultados': [], 'observacao': 'Informe termos mais específicos.'}
    with _conectar() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id, titulo, nome_arquivo, caminho_arquivo FROM intranet_doc_arquivos ORDER BY id')
            docs = cur.fetchall()
    resultados = []
    for doc_id, titulo, nome, caminho in docs:
        try:
            p = Path(caminho).resolve()
            if not p.is_relative_to(BASE) or not p.is_file() or p.stat().st_size > 20*1024*1024:
                continue
            stat = p.stat()
            for pagina, texto in extrair(str(p), stat.st_mtime_ns, stat.st_size):
                texto = re.sub(r'\s+', ' ', texto).strip()
                if not texto:
                    continue
                norm = normalizar(texto)
                for trecho_inicio in range(0, len(texto), 1000):
                    trecho = texto[trecho_inicio:trecho_inicio+1200]
                    norm_trecho = normalizar(trecho)
                    pontuacao = sum(norm_trecho.count(t) for t in termos)
                    if pontuacao:
                        resultados.append({'documento_id': doc_id, 'titulo': titulo, 'arquivo': nome,
                                           'pagina': pagina, 'trecho': trecho[:650], 'relevancia': pontuacao})
        except (OSError, ValueError):
            continue
    resultados.sort(key=lambda x: x['relevancia'], reverse=True)
    return {'resultados': resultados[:8], 'observacao': 'Trechos localizados nos documentos. Não são respostas geradas por IA.'}
