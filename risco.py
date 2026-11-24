"""Risco de penhora (v0.7.3, planejado antes como v0.6.5): leitura APROXIMADA de andamentos e publicações para avisar a equipe, com antecedência,
que um processo de execução / cumprimento de sentença caminha para penhora (inclusive de dinheiro em conta).

Finalidade: orientar o cliente e abrir negociação (acordo, parcelamento) antes do bloqueio — nunca orientar movimentação patrimonial
para frustrar a execução (alienar ou onerar bens depois da citação pode ser fraude à execução: CPC, art. 792).

Como funciona
- A equipe marca, por processo, se o nosso cliente é EXECUTADO (devedor) ou EXEQUENTE (credor). Só processos marcados são avaliados.
- O programa procura, no texto das publicações do DJEN e nos andamentos do DataJud, sinais da fase de execução, da ordem de
  pagamento, da citação/intimação, do pedido ou deferimento de penhora e do bloqueio efetivado, e monta uma linha do tempo.
- Para o EXECUTADO: grau baixo / médio / alto / crítico. Para o EXEQUENTE: situação do crédito e, quando o prazo de pagamento já
  venceu sem sinal de pedido de penhora, o alerta "oportunidade".
- É leitura de texto por palavras-chave: pode errar para mais ou para menos. Serve de alerta, não de certeza: confira nos autos.
Base legal consultada no texto compilado do Planalto (arquivo de 10/10/2026): CPC arts. 523 (cumprimento: 15 dias para pagar; multa e
honorários de 10%; mandado de penhora), 829 (execução: 3 dias da citação), 854 (indisponibilidade de ativos financeiros sem ciência
prévia; 5 dias para o executado provar impenhorabilidade ou excesso) e 792 (fraude à execução).
"""
import datetime as dt
import logging
import re
import time
import unicodedata

import agenda
import mailer
import prazos

log = logging.getLogger("monitor")
POSICOES = ("", "executado", "exequente")
GRAUS = ("", "baixo", "medio", "alto", "critico")
ROTULO_GRAU = {"": "sem sinais", "baixo": "baixo", "medio": "médio", "alto": "alto", "critico": "crítico"}
RANK = {g: i for i, g in enumerate(GRAUS)}
RANK["oportunidade"] = 3
MAX_TEXTO = 8000
AVISO = ("Leitura aproximada por palavras-chave de andamentos e publicações: pode errar para mais ou para menos. "
         "Confira sempre nos autos. Finalidade: orientar o cliente e negociar; não orientar movimentação patrimonial para frustrar a execução "
         "(CPC, art. 792).")

SCHEMA_RISCO = """
CREATE TABLE IF NOT EXISTS risco_estado(
  processo_id INTEGER PRIMARY KEY REFERENCES processos(id) ON DELETE CASCADE,
  grau TEXT NOT NULL DEFAULT '',
  avisado TEXT NOT NULL DEFAULT '',
  atualizado_em TEXT NOT NULL DEFAULT ''
);
"""


def migrar(b):
    with b.lock:
        cols = [r[1] for r in b.conn.execute("PRAGMA table_info(processos)").fetchall()]
        if "posicao" not in cols:
            b.conn.execute("ALTER TABLE processos ADD COLUMN posicao TEXT NOT NULL DEFAULT ''")
        b.conn.executescript(SCHEMA_RISCO)
        b.conn.commit()


def _n(t):
    t = unicodedata.normalize("NFKD", t or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def _r(p):
    return re.compile(p, re.S)


def _prep(t):
    """Normaliza e isola "desbloqueio" (que contém "bloqueio" e enganaria as outras regras)."""
    return re.sub(r"desbloque\w*", "xdesbloq", _n(t))


# (tipo, regex sobre o texto normalizado). A ordem importa só para o rótulo; a lógica usa as datas.
REGRAS = (
    ("fase_exec", _r(r"cumprimento (provisorio |definitivo )?de sentenca|execucao de titulo extrajudicial|execucao fiscal|fase de execucao|"
                     r"inicio d[ao] (cumprimento|execucao)|execucao de (alimentos|quantia)")),
    ("ordem_pagar", _r(r"intim\w+ .{0,80}(executad|devedor)\w* .{0,80}pag\w+|para pagar (o )?(debito|valor|divida|quantia)|pagamento voluntario|"
                       r"art\.? ?523|art\.? ?829|cite-se .{0,50}executad|citacao d[oa] executad|mandado de citacao|"
                       r"pague\w* .{0,40}(3|tres) dias|(15|quinze) dias .{0,60}(pag|multa)|multa de (10|dez)")),
    ("citado", _r(r"(citacao|citado|intimado|intimacao) (do executado )?(efetivad|realizad|positiv|cumprid)|certidao .{0,40}(citacao|intimacao).{0,30}"
                  r"(positiv|cumprid)|ar positivo|aviso de recebimento .{0,40}(positivo|juntad|cumprid)|executado (foi |restou )?(devidamente )?(citado|intimado)|"
                  r"citacao positiva|devedor (foi )?(citado|intimado)")),
    ("pedido_penhora", _r(r"(requer\w*|pedido de|solicit\w*|postul\w*|pleite\w*) .{0,80}(penhora|sisbajud|bacenjud|bloqueio|renajud|infojud|arresto)|"
                         r"penhora (online|on-line)|requer\w* (a )?(realizacao de )?(penhora|bloqueio)")),
    ("penhora_deferida", _r(r"(defiro|determino|autorizo|acolho|deferido|determinada|ordem de) .{0,120}(sisbajud|bacenjud|bloqueio|penhora|indisponibilidade|renajud|"
                            r"constricao)|ordem de bloqueio|minuta de bloqueio|protocolo(u|ada)? .{0,40}(sisbajud|bacenjud)")),
    ("penhora_efetivada", _r(r"(bloqueio|indisponibilidade) (efetivad|realizad|parcial|positiv|de valores)|valores? (bloquead|indisponibilizad|penhorad)|"
                             r"resultado (positivo|parcial) .{0,40}(sisbajud|bacenjud|bloqueio)|auto de penhora|termo de penhora|penhora realizada|"
                             r"bens? penhorad|transferencia .{0,40}(valor|montante|conta|juizo)|conversao .{0,30}penhora|ativos? financeiros? bloquead")),
    ("desbloqueio", _r(r"xdesbloq|levantamento d[ae] (penhora|constricao|bloqueio)|cancelamento d[ae] (indisponibilidade|penhora)|"
                       r"bloqueio (negativo|infrutifero)|resultado negativo|sem exito .{0,30}(sisbajud|bloqueio)|libera\w+ .{0,30}(valor|bloqueio|penhora)")),
    ("acordo", _r(r"acordo .{0,40}(homologad|celebrad|firmad|cumprid)|homologo .{0,40}acordo|suspens\w+ .{0,60}(acordo|art\.? ?922|parcelamento)|"
                  r"parcelamento (do debito|da divida)|art\.? ?922")),
    ("extinto", _r(r"extin\w+ .{0,50}(pagamento|satisfacao|art\.? ?924)|quitacao|satisfacao (do|da) (credito|obrigacao)|art\.? ?924|julgo extint\w+ .{0,30}execucao")),
    ("defesa", _r(r"impugnacao ao cumprimento|embargos (a|de) execucao|efeito suspensivo|excecao de pre-?executividade")),
)
NOMES = {"fase_exec": "Fase de execução / cumprimento", "ordem_pagar": "Ordem de pagamento / citação do executado", "citado": "Executado citado ou intimado",
         "pedido_penhora": "Pedido de penhora / bloqueio", "penhora_deferida": "Penhora ou bloqueio determinado", "penhora_efetivada": "Bloqueio / penhora efetivados",
         "desbloqueio": "Desbloqueio ou resultado negativo", "acordo": "Acordo / suspensão", "extinto": "Pagamento / extinção", "defesa": "Impugnação ou embargos"}


def _trecho(txt_orig, txt_norm, m):
    """Trecho do texto ORIGINAL ao redor do achado (a normalização por NFKD mantém o comprimento em português na imensa maioria dos casos)."""
    base = txt_orig if len(txt_orig) == len(txt_norm) else txt_norm.replace("xdesbloq", "desbloqueio")
    a, z = max(0, m.start() - 70), min(len(base), m.end() + 90)
    return ("…" if a else "") + " ".join(base[a:z].split()) + ("…" if z < len(base) else "")


def coletar(b, pid):
    """Textos datados do processo: publicações (uma por grupo) e andamentos."""
    itens = []
    for p in b._linhas("SELECT MIN(id) AS id, MAX(data_disp) AS d, tipo_com, classe, texto FROM publicacoes WHERE processo_id=? AND cancelada=0 GROUP BY grupo", (pid,)):
        itens.append({"fonte": "publicacao", "data": (p["d"] or "")[:10], "texto": f"{p['tipo_com'] or ''} {p['classe'] or ''} {(p['texto'] or '')[:MAX_TEXTO]}"})
    for m in b._linhas("SELECT nome, complementos, data_hora FROM movimentos WHERE processo_id=?", (pid,)):
        itens.append({"fonte": "andamento", "data": (m["data_hora"] or "")[:10], "texto": f"{m['nome'] or ''} {m['complementos'] or ''}"[:MAX_TEXTO]})
    return [x for x in itens if x["data"]]


def sinais(itens, classe=""):
    out = []
    if classe:
        n = _prep(classe)
        for tipo, rx in REGRAS:
            if tipo == "fase_exec" and rx.search(n):
                out.append({"tipo": "fase_exec", "data": "", "fonte": "classe", "trecho": classe})
    for it in itens:
        n = _prep(it["texto"])
        for tipo, rx in REGRAS:
            m = rx.search(n)
            if m:
                out.append({"tipo": tipo, "data": it["data"], "fonte": it["fonte"], "trecho": _trecho(it["texto"], n, m)})
    return sorted(out, key=lambda s: (s["data"], s["tipo"]))


def _ultimo(sig, *tipos):
    xs = [s["data"] for s in sig if s["tipo"] in tipos and s["data"]]
    return max(xs) if xs else ""


def _tem(sig, *tipos):
    return any(s["tipo"] in tipos for s in sig)


def _tipo_exec(sig, classe):
    t = _n(classe) + " " + " ".join(_n(s["trecho"]) for s in sig)
    if "cumprimento" in t or re.search(r"art\.? ?523", t):
        return "cumprimento"
    if "extrajudicial" in t or "art. 829" in t or "art 829" in t:
        return "extrajudicial"
    return ""


def prazo_pagamento(sig, classe, cal):
    """Vencimento APROXIMADO do prazo de pagamento voluntário (CPC, art. 523: 15 dias; art. 829: 3 dias), contado a partir do último sinal
    de citação/intimação efetiva (ou, na falta, da ordem de pagamento). Dias úteis (CPC, art. 219). Prefere errar para o lado mais curto."""
    tipo = _tipo_exec(sig, classe)
    dias = {"cumprimento": 15, "extrajudicial": 3}.get(tipo)
    base = [s for s in sig if s["tipo"] == "citado" and s["data"]] or [s for s in sig if s["tipo"] == "ordem_pagar" and s["data"]]
    if not dias or not base:
        return None
    s = base[-1]
    d0 = dt.date.fromisoformat(s["data"])
    try:
        if s["fonte"] == "publicacao":
            r = prazos.calcular(d0, dias, cal)
            venc, nota = r["vencimento"], f"{dias} dias úteis a contar da publicação disponibilizada em {s['data'][8:]}/{s['data'][5:7]}/{s['data'][:4]}"
        else:
            x, n = cal.proximo_util(d0), 0
            while True:
                if not cal.motivo_nao_util(x, True):
                    n += 1
                    if n == dias:
                        break
                x += dt.timedelta(days=1)
            venc, nota = x.isoformat(), f"{dias} dias úteis a contar do ato de {s['data'][8:]}/{s['data'][5:7]}/{s['data'][:4]} (andamento)"
    except prazos.ErroPrazo:
        return None
    return {"dias": dias, "vencimento": venc, "base": nota, "artigo": "CPC, art. 523" if tipo == "cumprimento" else "CPC, art. 829", "aproximado": True}


def avaliar(b, p, cal=None, hoje=None):
    """p: linha de processos (com `posicao`). Devolve dict com grau, rótulo, sinais, prazo e sugestões."""
    cal = cal or prazos.Calendario()
    hoje = hoje or dt.date.today().isoformat()
    sig = sinais(coletar(b, p["id"]), p.get("classe") or "")
    pos = p.get("posicao") or ""
    ult = lambda *t: _ultimo(sig, *t)  # noqa: E731
    prazo = prazo_pagamento(sig, p.get("classe") or "", cal)
    vencido = bool(prazo and prazo["vencimento"] < hoje)
    esc = max(ult("fase_exec", "ordem_pagar", "citado", "pedido_penhora", "penhora_deferida", "penhora_efetivada"), "")
    fim = ult("extinto")
    acordo = ult("acordo")
    desb = ult("desbloqueio")
    efet, defe, ped = ult("penhora_efetivada"), ult("penhora_deferida"), ult("pedido_penhora")
    if fim and fim >= esc:
        grau, rot = "", "Execução encerrada (pagamento/extinção) — confirme nos autos"
    elif acordo and acordo >= max(ult("pedido_penhora", "penhora_deferida", "penhora_efetivada", "citado", "ordem_pagar"), ""):
        grau, rot = "baixo", "Acordo ou suspensão registrados após os últimos atos de execução"
    elif efet and efet >= max(defe, desb or ""):
        grau, rot = "critico", "Bloqueio/penhora aparentemente efetivados"
    elif defe and defe >= max(efet, desb or ""):
        grau, rot = "critico", "Penhora ou bloqueio determinados pelo juízo"
    elif ped and ped >= (desb or ""):
        grau, rot = "alto", "Pedido de penhora/bloqueio do exequente"
    elif vencido:
        grau, rot = "alto", "Prazo de pagamento voluntário aparentemente vencido"
    elif _tem(sig, "citado", "ordem_pagar"):
        grau, rot = "medio", "Executado citado/intimado para pagar — prazo em curso" if _tem(sig, "citado") else "Ordem de pagamento/citação do executado"
    elif _tem(sig, "fase_exec"):
        grau, rot = "baixo", "Fase de execução/cumprimento, sem sinais de citação ou penhora"
    else:
        grau, rot = "", "Sem sinais de execução nos textos guardados"
    res = {"processo_id": p["id"], "numero": p["numero"], "apelido": p.get("apelido") or "", "cliente": p.get("cliente") or "", "posicao": pos,
           "responsavel_id": p.get("responsavel_id"), "classe": p.get("classe") or "", "grau": grau, "rotulo": rot, "prazo": prazo,
           "sinais": [{**s, "nome": NOMES[s["tipo"]]} for s in sig][-12:], "n_sinais": len(sig), "aviso": AVISO}
    if pos == "exequente":
        # visão do credor: o que fazer com o devedor; "oportunidade" quando o prazo venceu sem sinal de pedido de penhora
        nada = not _tem(sig, "pedido_penhora", "penhora_deferida", "penhora_efetivada")
        if res["grau"] == "" and fim and fim >= esc:
            res["situacao"] = "Encerrada"
        elif vencido and nada and not acordo >= (prazo["vencimento"] if prazo else ""):
            res["grau"], res["situacao"] = "oportunidade", "Prazo de pagamento vencido sem sinal de pedido de penhora"
            res["rotulo"] = "Oportunidade: avaliar requerer a penhora (prazo do executado aparentemente vencido)"
        elif _tem(sig, "penhora_efetivada"):
            res["grau"], res["situacao"] = "acompanhar", "Bloqueio/penhora aparentemente efetivados — acompanhar conversão e levantamento"
        elif _tem(sig, "penhora_deferida", "pedido_penhora"):
            res["grau"], res["situacao"] = "acompanhar", "Penhora requerida ou determinada — acompanhar"
        elif prazo and not vencido:
            res["grau"], res["situacao"] = "acompanhar", "Prazo de pagamento do executado em curso"
        elif _tem(sig, "ordem_pagar", "citado"):
            res["grau"], res["situacao"] = "acompanhar", "Aguardando citação/intimação ou o fim do prazo"
        elif _tem(sig, "fase_exec"):
            res["grau"], res["situacao"] = "acompanhar", "Fase de execução, sem sinais de citação"
        else:
            res["grau"], res["situacao"] = "", "Sem sinais de execução nos textos guardados"
        res["rotulo"] = res["rotulo"] if res["grau"] == "oportunidade" else res["situacao"]
    res["sugestoes"] = sugestoes(res)
    return res


def sugestoes(r):
    g, pos = r["grau"], r["posicao"]
    s = []
    if pos == "executado":
        if g == "medio":
            s += ["Orientar o cliente sobre o prazo de pagamento voluntário e sobre a multa de 10% e os honorários de 10% se não pagar (CPC, art. 523, § 1º).",
                  "Avaliar proposta de acordo ou parcelamento com o exequente antes do fim do prazo; conferir nos autos a data da citação/intimação."]
        elif g == "alto":
            s += ["Procurar o exequente para negociar já: a penhora de dinheiro em conta pode ser determinada sem ciência prévia do executado (CPC, art. 854, caput).",
                  "Preparar, desde já, a documentação para eventual defesa (impenhorabilidade, excesso de execução)."]
        elif g == "critico":
            s += ["Conferir nos autos, com urgência, se houve indisponibilidade: o executado é intimado na pessoa do advogado e tem 5 dias para provar impenhorabilidade ou excesso (CPC, art. 854, §§ 2º e 3º).",
                  "Avaliar negociação imediata e pedido de desbloqueio de valores impenhoráveis ou excessivos."]
        elif g == "baixo":
            s += ["Acompanhar: a execução existe, mas não há sinal de citação ou de penhora nos textos guardados."]
        if g in ("medio", "alto", "critico"):
            s.append("Não orientar o cliente a movimentar ou alienar bens para evitar a execução: pode configurar fraude à execução e é ineficaz perante o exequente (CPC, art. 792).")
    elif pos == "exequente":
        if g == "oportunidade":
            s += ["O prazo de pagamento do executado aparenta ter vencido sem sinal de pedido de penhora: avaliar requerê-la (CPC, art. 523, § 3º) e, para dinheiro em conta, a indisponibilidade de ativos (CPC, art. 854).",
                  "Conferir nos autos se houve pagamento, impugnação com efeito suspensivo ou acordo antes de peticionar."]
        elif g == "acompanhar":
            s += ["Acompanhar o andamento; conferir nos autos a data da citação/intimação e o fim do prazo."]
    return s


# ---------------------------------------------------------------- lista, estado e aviso
def definir_posicao(b, pid, posicao):
    if posicao not in POSICOES:
        raise ValueError("Posição inválida: use 'executado', 'exequente' ou vazio.")
    with b.lock:
        if not b.conn.execute("SELECT 1 FROM processos WHERE id=?", (pid,)).fetchone():
            raise ValueError("Processo não encontrado.")
        b.conn.execute("UPDATE processos SET posicao=? WHERE id=?", (posicao, pid))
        if not posicao:
            b.conn.execute("DELETE FROM risco_estado WHERE processo_id=?", (pid,))
        b.conn.commit()
    _cache["v"] = None


def listar(b, cfg_prazos=None, posicao=None, hoje=None, q=""):
    cal = prazos.Calendario((cfg_prazos or {}).get("feriados_extras"), (cfg_prazos or {}).get("recesso"))
    sql, args = "SELECT * FROM processos WHERE ativo=1 AND posicao<>''", []
    if posicao in ("executado", "exequente"):
        sql += " AND posicao=?"
        args.append(posicao)
    out = [avaliar(b, p, cal, hoje) for p in b._linhas(sql, tuple(args))]
    q = _n(q).strip()
    if q:
        out = [r for r in out if q in _n(f"{r['numero']} {r['apelido']} {r['cliente']}")]
    out.sort(key=lambda r: (-RANK.get(r["grau"], 0), r["cliente"].lower(), r["numero"]))
    return out


def resumo(b, cfg_prazos=None, hoje=None):
    r = listar(b, cfg_prazos, hoje=hoje)
    ex = [x for x in r if x["posicao"] == "executado"]
    return {"executado": {g: sum(1 for x in ex if x["grau"] == g) for g in ("critico", "alto", "medio", "baixo")}, "n_executado": len(ex),
            "oportunidades": sum(1 for x in r if x["grau"] == "oportunidade"), "n_exequente": sum(1 for x in r if x["posicao"] == "exequente"),
            "lista": [x for x in r if x["grau"] in ("critico", "alto", "oportunidade")][:6]}


_cache = {"t": 0.0, "v": None}


def contagem(b, cfg_prazos=None, ttl=120):
    """Números do selo da aba (críticos+altos e oportunidades), com cache curto para não reavaliar a cada atualização de status."""
    if _cache["v"] is None or time.time() - _cache["t"] > ttl:
        r = resumo(b, cfg_prazos)
        _cache["v"] = {"graves": r["executado"]["critico"] + r["executado"]["alto"], "oportunidades": r["oportunidades"], "n": r["n_executado"] + r["n_exequente"]}
        _cache["t"] = time.time()
    return _cache["v"]


_ult_aviso = {"t": 0.0}


def avisar(b, cfg, enviar=None, hoje=None, intervalo=600):
    """Compara o grau atual com o último avisado; e-mail só quando SOBE para alto/crítico (executado) ou surge oportunidade (exequente).
    Destinatários: responsável do processo (se tiver e-mail) e e-mails gerais. Roda no máximo a cada `intervalo` segundos."""
    em = cfg.get("email") or {}
    if not em.get("ativo") or not em.get("destinatario"):
        return {"enviados": 0, "motivo": "e-mail desligado"}
    if intervalo and time.time() - _ult_aviso["t"] < intervalo:
        return {"enviados": 0, "motivo": "aguardando intervalo"}
    _ult_aviso["t"] = time.time()
    enviar = enviar or mailer.enviar
    gerais = [x.strip() for x in re.split(r"[;,]", em["destinatario"]) if x.strip()]
    n = 0
    for r in listar(b, cfg.get("prazos"), hoje=hoje):
        est = b._linhas("SELECT * FROM risco_estado WHERE processo_id=?", (r["processo_id"],))
        antes = est[0]["avisado"] if est else ""
        alvo = r["grau"] if (r["posicao"] == "executado" and r["grau"] in ("alto", "critico")) or r["grau"] == "oportunidade" else ""
        novo_avisado = antes
        if alvo and RANK.get(alvo, 0) > RANK.get(antes, 0):
            dest = {g.lower(): "" for g in gerais}
            u = b.email_do_responsavel(r["responsavel_id"]) if r.get("responsavel_id") else None
            if u:
                dest[u["email"].strip().lower()] = u["nome"]
            ok = False
            for d, nome in dest.items():
                try:
                    enviar({**em, "destinatario": d}, f"[Monitor de Processos] Risco de penhora {ROTULO_GRAU.get(alvo, 'oportunidade')}: {r['apelido'] or r['cliente'] or r['numero']}",
                           montar_email(r, nome))
                    ok = True
                except Exception as e:  # noqa: BLE001
                    log.warning("Falha ao enviar o aviso de risco de penhora a %s: %s", d, e)
            if ok:
                n += 1
                novo_avisado = alvo
        elif not alvo and antes:
            novo_avisado = ""      # voltou a um grau menor: se subir de novo, avisa de novo
        with b.lock:
            b.conn.execute("INSERT INTO risco_estado(processo_id,grau,avisado,atualizado_em) VALUES(?,?,?,?) "
                           "ON CONFLICT(processo_id) DO UPDATE SET grau=excluded.grau, avisado=excluded.avisado, atualizado_em=excluded.atualizado_em",
                           (r["processo_id"], r["grau"], novo_avisado, agenda.agora()))
            b.conn.commit()
    return {"enviados": n, "motivo": "ok"}


def montar_email(r, nome=""):
    L = [f"Olá{', ' + nome if nome else ''}.", "",
         f"Processo: {r['numero']}" + (f" — {r['apelido'] or r['cliente']}" if (r["apelido"] or r["cliente"]) else ""),
         f"Nosso cliente é: {'EXECUTADO (devedor)' if r['posicao'] == 'executado' else 'EXEQUENTE (credor)'}",
         f"Avaliação: {r['rotulo']}" + (f" (grau {ROTULO_GRAU[r['grau']]})" if r["posicao"] == "executado" else "")]
    if r.get("prazo"):
        v = r["prazo"]["vencimento"]
        L.append(f"Prazo de pagamento (aproximado): {v[8:]}/{v[5:7]}/{v[:4]} — {r['prazo']['base']} ({r['prazo']['artigo']}). Confira nos autos.")
    L += ["", "Sinais encontrados:"] + [f"• {s['data'][8:]}/{s['data'][5:7]}/{s['data'][:4]} — {s['nome']}: {s['trecho'][:200]}" for s in r["sinais"][-5:] if s["data"]]
    L += [""] + [f"→ {x}" for x in r["sugestoes"]] + ["", r["aviso"]]
    return "\n".join(L)
