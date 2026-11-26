"""v0.8 — Painel de gestão (só leitura): carga da equipe, carteira, processos parados, risco/alertas e publicações do DJEN.

Nada aqui grava no banco. Todos os números saem de consultas SQL simples sobre o que o programa já guarda;
o que a fonte (DataJud/DJEN) não informa aparece como "sem dados", e não como zero.
"""
import datetime as dt

import risco

PARADOS_OPCOES = (30, 60, 90, 180, 365)
PARADOS_PADRAO = 90
MAX_LISTA = 50
SEM_RESP = "(sem responsável)"


def _um(b, sql, args=()):
    with b.lock:
        r = b.conn.execute(sql, args).fetchone()
    return r[0] if r and r[0] is not None else 0


def _tabela(b, nome):
    with b.lock:
        return b.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone() is not None


def _dia(txt):
    """Primeiros 10 caracteres de uma data ISO; None se não for data válida."""
    t = str(txt or "")[:10]
    try:
        return dt.date.fromisoformat(t)
    except ValueError:
        return None


def _tribunal(alias):
    a = str(alias or "").strip()
    if a.startswith("api_publica_"):
        a = a[len("api_publica_"):]
    return a.upper() or "(não identificado)"


# ---------------------------------------------------------------- 1. carga da equipe
def equipe(b, hoje):
    h, h7 = hoje.isoformat(), (hoje + dt.timedelta(days=7)).isoformat()
    pessoas = {None: {"id": None, "nome": SEM_RESP}}
    if _tabela(b, "usuarios"):
        for u in b._linhas("SELECT id, nome FROM usuarios WHERE ativo=1 ORDER BY nome COLLATE NOCASE"):
            pessoas[u["id"]] = {"id": u["id"], "nome": u["nome"]}
    for v in pessoas.values():
        v.update(processos=0, vencidos=0, hoje=0, prox7=0, abertos=0, pub_novas=0, pub_analise=0)

    def pessoa(rid):
        if rid not in pessoas:   # responsável inativado ou removido: não esconde a carga dele
            pessoas[rid] = {"id": rid, "nome": "(usuário inativo)", "processos": 0, "vencidos": 0, "hoje": 0, "prox7": 0,
                            "abertos": 0, "pub_novas": 0, "pub_analise": 0}
        return pessoas[rid]

    for r in b._linhas("SELECT responsavel_id AS r, COUNT(*) AS n FROM processos WHERE ativo=1 GROUP BY responsavel_id"):
        pessoa(r["r"])["processos"] = r["n"]
    for r in b._linhas(
            "SELECT e.responsavel_id AS r, COUNT(*) AS abertos, SUM(e.data<?) AS venc, SUM(e.data=?) AS hj, "
            "SUM(e.data>? AND e.data<=?) AS p7 FROM eventos e WHERE e.situacao='aberto' GROUP BY e.responsavel_id", (h, h, h, h7)):
        p = pessoa(r["r"])
        p["abertos"], p["vencidos"], p["hoje"], p["prox7"] = r["abertos"], r["venc"] or 0, r["hj"] or 0, r["p7"] or 0
    for r in b._linhas(
            "SELECT p.responsavel_id AS r, b.situacao AS s, COUNT(DISTINCT p.id || '/' || b.grupo) AS n FROM publicacoes b "
            "JOIN processos p ON p.id=b.processo_id WHERE p.ativo=1 AND b.cancelada=0 AND b.situacao IN ('nova','analise') "
            "GROUP BY p.responsavel_id, b.situacao"):
        pessoa(r["r"])["pub_novas" if r["s"] == "nova" else "pub_analise"] = r["n"]
    lista = sorted(pessoas.values(), key=lambda x: (x["id"] is None, -(x["vencidos"] * 1000 + x["hoje"] * 100 + x["pub_novas"]), x["nome"].lower()))
    return [x for x in lista if x["id"] is not None or x["processos"] or x["abertos"] or x["pub_novas"] or x["pub_analise"]]


# ---------------------------------------------------------------- 2. carteira
def carteira(b):
    ativos = _um(b, "SELECT COUNT(*) FROM processos WHERE ativo=1")
    mon = _um(b, "SELECT COUNT(*) FROM processos WHERE ativo=1 AND monitorar=1")
    arq = _um(b, "SELECT COUNT(*) FROM processos WHERE ativo=0")
    cli = b._linhas("SELECT COALESCE(NULLIF(TRIM(cliente),''),'(sem cliente)') AS nome, COUNT(*) AS n, SUM(monitorar=1) AS mon "
                    "FROM processos WHERE ativo=1 GROUP BY 1 ORDER BY n DESC, nome COLLATE NOCASE LIMIT 15")
    n_cli = _um(b, "SELECT COUNT(DISTINCT COALESCE(NULLIF(TRIM(cliente),''),'(sem cliente)')) FROM processos WHERE ativo=1")
    trib = {}
    for r in b._linhas("SELECT alias, COUNT(*) AS n FROM processos WHERE ativo=1 GROUP BY alias"):
        k = _tribunal(r["alias"])
        trib[k] = trib.get(k, 0) + r["n"]
    cla = b._linhas("SELECT COALESCE(NULLIF(TRIM(classe),''),'(sem classe — ainda não consultado)') AS nome, COUNT(*) AS n "
                    "FROM processos WHERE ativo=1 GROUP BY 1 ORDER BY n DESC, nome COLLATE NOCASE LIMIT 10")
    return {"ativos": ativos, "monitorados": mon, "nao_monitorados": ativos - mon, "arquivados": arq,
            "por_cliente": [{"nome": r["nome"], "n": r["n"], "monitorados": r["mon"] or 0} for r in cli], "n_clientes": n_cli,
            "por_tribunal": [{"nome": k, "n": v} for k, v in sorted(trib.items(), key=lambda kv: (-kv[1], kv[0]))],
            "por_classe": [{"nome": r["nome"], "n": r["n"]} for r in cla]}


# ---------------------------------------------------------------- 3. processos parados
def parados(b, hoje, dias):
    """Só processos MONITORADOS e já consultados com sucesso: sem dado fresco, 'parado' seria palpite.
    Data de referência = a mais recente entre o último andamento e a última publicação."""
    linhas = b._linhas(
        "SELECT p.id, p.numero, p.apelido, p.cliente, p.alias, p.classe, "
        "(SELECT MAX(substr(m.data_hora,1,10)) FROM movimentos m WHERE m.processo_id=p.id) AS mov, "
        "(SELECT MAX(substr(x.data_disp,1,10)) FROM publicacoes x WHERE x.processo_id=p.id AND x.cancelada=0) AS pub, "
        "(SELECT u.nome FROM usuarios u WHERE u.id=p.responsavel_id) AS resp "
        "FROM processos p WHERE p.ativo=1 AND p.monitorar=1 AND p.status='ok'"
        if _tabela(b, "usuarios") else
        "SELECT p.id, p.numero, p.apelido, p.cliente, p.alias, p.classe, "
        "(SELECT MAX(substr(m.data_hora,1,10)) FROM movimentos m WHERE m.processo_id=p.id) AS mov, "
        "(SELECT MAX(substr(x.data_disp,1,10)) FROM publicacoes x WHERE x.processo_id=p.id AND x.cancelada=0) AS pub, NULL AS resp "
        "FROM processos p WHERE p.ativo=1 AND p.monitorar=1 AND p.status='ok'")
    out, sem_dados = [], 0
    for r in linhas:
        datas = [d for d in (_dia(r["mov"]), _dia(r["pub"])) if d]
        if not datas:
            sem_dados += 1
            continue
        ult = max(datas)
        parado = (hoje - ult).days
        if parado >= dias:
            out.append({"id": r["id"], "numero": r["numero"], "apelido": r["apelido"], "cliente": r["cliente"],
                        "tribunal": _tribunal(r["alias"]), "classe": r["classe"], "responsavel": r["resp"] or "",
                        "ultimo": ult.isoformat(), "dias": parado})
    out.sort(key=lambda x: (-x["dias"], x["numero"]))
    return {"dias": dias, "total": len(out), "sem_dados": sem_dados, "lista": out[:MAX_LISTA]}


# ---------------------------------------------------------------- 4. risco e alertas
def alertas(b, cfg_prazos, hoje):
    h, h7 = hoje.isoformat(), (hoje + dt.timedelta(days=7)).isoformat()
    r = risco.resumo(b, cfg_prazos, hoje=h)
    fatais = b._linhas(
        "SELECT e.id, e.titulo, e.data, e.processo_id, p.numero AS processo_numero, p.apelido AS processo_apelido, "
        "p.cliente AS cliente FROM eventos e JOIN tipos_evento t ON t.id=e.tipo_id LEFT JOIN processos p ON p.id=e.processo_id "
        "WHERE e.situacao='aberto' AND t.nome='Prazo fatal' AND e.data<=? ORDER BY e.data, e.id LIMIT ?", (h7, MAX_LISTA))
    for f in fatais:
        f["atrasado"] = f["data"] < h
    falhas = b._linhas(
        "SELECT id, numero, apelido, cliente, status, mensagem, dje_status, dje_mensagem FROM processos "
        "WHERE ativo=1 AND monitorar=1 AND (status IN ('erro','nao_encontrado') OR dje_status='erro') ORDER BY numero LIMIT ?", (MAX_LISTA,))
    n_falhas = _um(b, "SELECT COUNT(*) FROM processos WHERE ativo=1 AND monitorar=1 AND (status IN ('erro','nao_encontrado') OR dje_status='erro')")
    oabs_erro = b._linhas("SELECT numero, uf, rotulo, mensagem FROM oabs WHERE ativo=1 AND status='erro' ORDER BY numero")
    return {"penhora": {"executado": r["executado"], "n_executado": r["n_executado"], "oportunidades": r["oportunidades"],
                        "lista": [{"id": x.get("processo_id"), "apelido": x.get("apelido"), "cliente": x.get("cliente"), "numero": x.get("numero"),
                                   "grau": x.get("grau"), "rotulo": x.get("rotulo")} for x in r["lista"]]},
            "prazos_fatais": fatais, "falhas_total": n_falhas,
            "falhas": [{"id": f["id"], "numero": f["numero"], "apelido": f["apelido"], "cliente": f["cliente"],
                        "motivo": (f["mensagem"] if f["status"] in ("erro", "nao_encontrado") else f["dje_mensagem"]) or
                                  ("DataJud: " + f["status"] if f["status"] != "ok" else "DJEN: erro")} for f in falhas],
            "oabs_com_erro": [{"oab": f"{o['numero']}/{o['uf']}", "rotulo": o["rotulo"], "mensagem": o["mensagem"]} for o in oabs_erro]}


# ---------------------------------------------------------------- 5. publicações do DJEN
def publicacoes(b, hoje, inicio_dia_utc):
    h = hoje.isoformat()
    disp_hoje = _um(b, "SELECT COUNT(DISTINCT processo_id || '/' || grupo) FROM publicacoes WHERE cancelada=0 AND substr(data_disp,1,10)=?", (h,))
    detect_hoje = _um(b, "SELECT COUNT(DISTINCT processo_id || '/' || grupo) FROM publicacoes WHERE cancelada=0 AND detectado_em>=?", (inicio_dia_utc,))
    novas = _um(b, "SELECT COUNT(DISTINCT processo_id || '/' || grupo) FROM publicacoes WHERE cancelada=0 AND situacao='nova'")
    analise = _um(b, "SELECT COUNT(DISTINCT processo_id || '/' || grupo) FROM publicacoes WHERE cancelada=0 AND situacao='analise'")
    mais_antiga = b._linhas("SELECT MIN(substr(data_disp,1,10)) AS d FROM publicacoes WHERE cancelada=0 AND situacao='nova' AND data_disp<>''")
    antiga = _dia(mais_antiga[0]["d"]) if mais_antiga else None
    serie = []
    for i in range(6, -1, -1):
        d = (hoje - dt.timedelta(days=i)).isoformat()
        serie.append({"dia": d, "n": _um(b, "SELECT COUNT(DISTINCT processo_id || '/' || grupo) FROM publicacoes "
                                           "WHERE cancelada=0 AND substr(data_disp,1,10)=?", (d,))})
    filtros_novas = _um(b, "SELECT COUNT(*) FROM oab_pubs WHERE cancelada=0 AND visto=0")
    return {"disponibilizadas_hoje": disp_hoje, "detectadas_hoje": detect_hoje, "nao_tratadas": novas, "em_analise": analise,
            "mais_antiga_nao_tratada": antiga.isoformat() if antiga else "", "dias_mais_antiga": (hoje - antiga).days if antiga else 0,
            "serie_7d": serie, "filtros_nao_vistas": filtros_novas}


def painel(b, cfg_prazos=None, hoje=None, parados_dias=PARADOS_PADRAO, inicio_dia_utc=None):
    from db import inicio_do_dia_utc
    hoje = hoje or dt.date.today()
    if parados_dias not in PARADOS_OPCOES:
        parados_dias = PARADOS_PADRAO
    return {"hoje": hoje.isoformat(), "equipe": equipe(b, hoje), "carteira": carteira(b), "parados": parados(b, hoje, parados_dias),
            "alertas": alertas(b, cfg_prazos, hoje), "publicacoes": publicacoes(b, hoje, inicio_dia_utc or inicio_do_dia_utc()),
            "opcoes_parados": list(PARADOS_OPCOES)}
