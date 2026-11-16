from datetime import date
from typing import Optional
from fastapi import APIRouter, Header, HTTPException

router_aniversariantes = APIRouter(prefix="/api/aniversariantes", tags=["Aniversariantes"])

def configurar_aniversariantes(conectar, autenticar):
    router_aniversariantes.state_conectar = conectar
    router_aniversariantes.state_autenticar = autenticar

@router_aniversariantes.get("")
def listar_aniversariantes(data: date, authorization: Optional[str] = Header(None)):
    usuario = router_aniversariantes.state_autenticar(authorization)
    if usuario[5]:
        raise HTTPException(403, "Troca de senha obrigatoria")

    with router_aniversariantes.state_conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, nome, departamento, foto_perfil,
                       data_nascimento, data_admissao
                FROM intranet_usuarios
                WHERE ativo = TRUE
                  AND data_demissao IS NULL
                  AND (
                    (EXTRACT(MONTH FROM data_nascimento) = %s
                     AND EXTRACT(DAY FROM data_nascimento) = %s)
                    OR
                    (EXTRACT(MONTH FROM data_admissao) = %s
                     AND EXTRACT(DAY FROM data_admissao) = %s)
                  )
                ORDER BY nome
            """, (data.month, data.day, data.month, data.day))
            registros = cur.fetchall()

    nascimento = []
    empresa = []

    for r in registros:
        id_, nome, departamento, foto, nasc, admissao = r
        base = {
            "id": id_,
            "nome": nome,
            "departamento": departamento,
            "foto_perfil": foto
        }

        if nasc and (nasc.month, nasc.day) == (data.month, data.day):
            nascimento.append(base)

        if admissao and (admissao.month, admissao.day) == (data.month, data.day):
            anos = data.year - admissao.year
            if anos >= 1:
                empresa.append({**base, "anos_empresa": anos})

    return {
        "data": data.isoformat(),
        "nascimento": nascimento,
        "empresa": empresa
    }
