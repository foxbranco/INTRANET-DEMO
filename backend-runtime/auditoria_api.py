import json


def registrar_auditoria(
    conectar,
    usuario_id,
    sessao_id,
    tipo_evento,
    acao,
    modulo,
    resultado,
    recurso=None,
    recurso_id=None,
    detalhes=None,
    ip_hash=None,
    tipo_dispositivo=None,
    duracao_ms=None
):
    """Registra um evento de auditoria no PostgreSQL."""

    with conectar() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO intranet_auditoria (
                    usuario_id,
                    sessao_id,
                    tipo_evento,
                    acao,
                    modulo,
                    recurso,
                    recurso_id,
                    resultado,
                    detalhes,
                    ip_hash,
                    tipo_dispositivo,
                    duracao_ms
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
            """, (
                usuario_id,
                str(sessao_id) if sessao_id is not None else None,
                tipo_evento,
                acao,
                modulo,
                recurso,
                recurso_id,
                resultado,
                json.dumps(detalhes) if detalhes is not None else None,
                ip_hash,
                tipo_dispositivo,
                duracao_ms
            ))
