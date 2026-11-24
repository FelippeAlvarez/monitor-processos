"""Agenda (v0.7.0): eventos (audiências, prazos, diligências...) ligados ou não a um processo.

- Tipos de evento são editáveis (a administração cria, renomeia, desativa e exclui os que não estão em uso).
- Eventos podem ser criados, editados (título, teor, tipo, data, hora, local, responsável, lembretes), concluídos, reabertos,
  cancelados e excluídos. Toda mudança fica no log do evento (quem, quando, o quê).
- Edição simultânea: cada evento tem uma 'versao'; quem salva com versão desatualizada recebe um aviso em vez de sobrescrever.
- Datas e horas são locais, sem fuso (um prazo é uma data, não um instante).
- Os lembretes (dias antes) são escolhidos a cada agendamento e guardados no evento; o envio por e-mail está em lembretes.py (v0.7.2).
- v0.7.2: o evento pode guardar a memória de cálculo do prazo (campo `calculo`): a data calculada é só sugestão; vale a data gravada no evento.
"""
import datetime as dt
import json
import re

SCHEMA_AGENDA = """
CREATE TABLE IF NOT EXISTS tipos_evento(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nome TEXT NOT NULL UNIQUE COLLATE NOCASE,
  cor TEXT NOT NULL DEFAULT '#2a6fb0',
  ativo INTEGER NOT NULL DEFAULT 1,
  ordem INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS eventos(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  processo_id INTEGER REFERENCES processos(id) ON DELETE CASCADE,
  tipo_id INTEGER NOT NULL REFERENCES tipos_evento(id),
  titulo TEXT NOT NULL,
  descricao TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL,
  hora TEXT NOT NULL DEFAULT '',
  local TEXT NOT NULL DEFAULT '',
  responsavel_id INTEGER,
  situacao TEXT NOT NULL DEFAULT 'aberto',
  lembretes TEXT NOT NULL DEFAULT '[]',
  origem TEXT NOT NULL DEFAULT '',
  versao INTEGER NOT NULL DEFAULT 1,
  criado_por TEXT NOT NULL DEFAULT '',
  criado_em TEXT NOT NULL DEFAULT '',
  atualizado_por TEXT NOT NULL DEFAULT '',
  atualizado_em TEXT NOT NULL DEFAULT '',
  concluido_por TEXT NOT NULL DEFAULT '',
  concluido_em TEXT NOT NULL DEFAULT '',
  conclusao_obs TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_ev_data ON eventos(data);
CREATE INDEX IF NOT EXISTS ix_ev_proc ON eventos(processo_id, data);
CREATE TABLE IF NOT EXISTS eventos_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evento_id INTEGER NOT NULL REFERENCES eventos(id) ON DELETE CASCADE,
  quando TEXT NOT NULL,
  por TEXT NOT NULL DEFAULT '',
  acao TEXT NOT NULL,
  detalhe TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_evlog ON eventos_log(evento_id, id);
"""

TIPOS_PADRAO = (("Audiência", "#7a3fb0"), ("Prazo fatal", "#b3261e"), ("Prazo interno", "#c98a1b"),
                ("Diligência / tarefa", "#2a6fb0"), ("Perícia", "#0f7b6c"), ("Reunião / atendimento", "#5b6578"), ("Outro", "#8a8f98"))
SITUACOES = ("aberto", "concluido", "cancelado")
FILTRO_SIT = ("ativos", "aberto", "concluido", "cancelado", "todos")
LEMBRETES_OK = (0, 1, 2, 3, 5, 7, 10, 15, 30)
LEMBRETES_PADRAO = [1, 0]
MAX_LISTA = 2000
_RE_COR = re.compile(r"#[0-9a-fA-F]{6}")
_RE_HORA = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


class ErroAgenda(ValueError):
    pass


class ConflitoVersao(ErroAgenda):
    pass


def agora():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SCHEMA_LEMB = """
CREATE TABLE IF NOT EXISTS lembretes_enviados(
  evento_id INTEGER NOT NULL REFERENCES eventos(id) ON DELETE CASCADE,
  dias_antes INTEGER NOT NULL,
  data_ref TEXT NOT NULL,
  destinatario TEXT NOT NULL,
  enviado_em TEXT NOT NULL,
  PRIMARY KEY(evento_id, dias_antes, data_ref, destinatario)
);
CREATE TABLE IF NOT EXISTS agenda_resumos(
  dia TEXT NOT NULL,
  destinatario TEXT NOT NULL,
  enviado_em TEXT NOT NULL,
  PRIMARY KEY(dia, destinatario)
);
"""


def migrar(b):
    b.conn.executescript(SCHEMA_AGENDA)
    if "calculo" not in [r[1] for r in b.conn.execute("PRAGMA table_info(eventos)").fetchall()]:
        b.conn.execute("ALTER TABLE eventos ADD COLUMN calculo TEXT NOT NULL DEFAULT ''")
    b.conn.executescript(SCHEMA_LEMB)
    if not b.conn.execute("SELECT 1 FROM tipos_evento LIMIT 1").fetchone():
        for i, (nome, cor) in enumerate(TIPOS_PADRAO):
            b.conn.execute("INSERT INTO tipos_evento(nome,cor,ordem) VALUES(?,?,?)", (nome, cor, i))
    b.conn.commit()


def _nome_resp(b):
    existe = b.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usuarios'").fetchone()
    return "(SELECT u.nome FROM usuarios u WHERE u.id=e.responsavel_id)" if existe else "NULL"


# ---------------------------------------------------------------- tipos
def tipos(b):
    return b._linhas("""SELECT t.id, t.nome, t.cor, t.ativo, t.ordem,
                        (SELECT COUNT(*) FROM eventos e WHERE e.tipo_id=t.id) AS em_uso
                        FROM tipos_evento t ORDER BY t.ativo DESC, t.ordem, t.nome COLLATE NOCASE""")


def _nome_tipo(nome):
    nome = " ".join(str(nome or "").split())
    if not 2 <= len(nome) <= 40:
        raise ErroAgenda("O nome do tipo deve ter de 2 a 40 caracteres.")
    return nome


def _cor(cor):
    cor = str(cor or "#2a6fb0").strip()
    if not _RE_COR.fullmatch(cor):
        raise ErroAgenda("Cor inválida (use o formato #RRGGBB).")
    return cor.lower()


def criar_tipo(b, nome, cor):
    nome, cor = _nome_tipo(nome), _cor(cor)
    with b.lock:
        if b.conn.execute("SELECT 1 FROM tipos_evento WHERE nome=?", (nome,)).fetchone():
            raise ErroAgenda("Já existe um tipo com esse nome.")
        ordem = b.conn.execute("SELECT COALESCE(MAX(ordem),0)+1 FROM tipos_evento").fetchone()[0]
        cur = b.conn.execute("INSERT INTO tipos_evento(nome,cor,ordem) VALUES(?,?,?)", (nome, cor, ordem))
        b.conn.commit()
        return cur.lastrowid


def editar_tipo(b, tid, dados):
    with b.lock:
        t = b.conn.execute("SELECT * FROM tipos_evento WHERE id=?", (tid,)).fetchone()
        if not t:
            raise ErroAgenda("Tipo não encontrado.")
        nome = _nome_tipo(dados["nome"]) if "nome" in dados else t["nome"]
        cor = _cor(dados["cor"]) if "cor" in dados else t["cor"]
        ativo = 1 if dados.get("ativo", t["ativo"]) else 0
        outro = b.conn.execute("SELECT 1 FROM tipos_evento WHERE nome=? AND id<>?", (nome, tid)).fetchone()
        if outro:
            raise ErroAgenda("Já existe um tipo com esse nome.")
        if not ativo and not b.conn.execute("SELECT 1 FROM tipos_evento WHERE ativo=1 AND id<>?", (tid,)).fetchone():
            raise ErroAgenda("Deve existir ao menos um tipo ativo.")
        b.conn.execute("UPDATE tipos_evento SET nome=?, cor=?, ativo=? WHERE id=?", (nome, cor, ativo, tid))
        b.conn.commit()


def excluir_tipo(b, tid):
    with b.lock:
        if not b.conn.execute("SELECT 1 FROM tipos_evento WHERE id=?", (tid,)).fetchone():
            raise ErroAgenda("Tipo não encontrado.")
        n = b.conn.execute("SELECT COUNT(*) FROM eventos WHERE tipo_id=?", (tid,)).fetchone()[0]
        if n:
            raise ErroAgenda(f"Este tipo está em uso por {n} evento(s) e não pode ser excluído. Desative-o: ele deixa de aparecer "
                             "para novos eventos, mas os antigos continuam como estão.")
        if not b.conn.execute("SELECT 1 FROM tipos_evento WHERE ativo=1 AND id<>?", (tid,)).fetchone():
            raise ErroAgenda("Deve existir ao menos um tipo ativo.")
        b.conn.execute("DELETE FROM tipos_evento WHERE id=?", (tid,))
        b.conn.commit()


# ---------------------------------------------------------------- eventos
def _data(txt, campo="Data"):
    try:
        return dt.date.fromisoformat(str(txt or "")[:10]).isoformat()
    except ValueError:
        raise ErroAgenda(f"{campo} inválida (use AAAA-MM-DD).")


def validar(b, c, auth=None, completo=True):
    """Confere e limpa os campos vindos da interface. Levanta ErroAgenda com mensagem em português."""
    r = {}
    titulo = " ".join(str(c.get("titulo") or "").split())
    if completo or "titulo" in c:
        if not 2 <= len(titulo) <= 160:
            raise ErroAgenda("O título deve ter de 2 a 160 caracteres.")
        r["titulo"] = titulo
    if completo or "descricao" in c:
        d = str(c.get("descricao") or "").strip()
        if len(d) > 4000:
            raise ErroAgenda("O teor/descrição passa de 4.000 caracteres.")
        r["descricao"] = d
    if completo or "data" in c:
        r["data"] = _data(c.get("data"))
    if completo or "hora" in c:
        h = str(c.get("hora") or "").strip()
        if h and not _RE_HORA.fullmatch(h):
            raise ErroAgenda("Hora inválida (use HH:MM).")
        r["hora"] = h
    if completo or "local" in c:
        loc = " ".join(str(c.get("local") or "").split())
        if len(loc) > 200:
            raise ErroAgenda("O local/link passa de 200 caracteres.")
        r["local"] = loc
    if completo or "tipo_id" in c:
        try:
            tid = int(c.get("tipo_id"))
        except (TypeError, ValueError):
            raise ErroAgenda("Escolha o tipo do evento.")
        t = b.conn.execute("SELECT ativo FROM tipos_evento WHERE id=?", (tid,)).fetchone()
        if not t:
            raise ErroAgenda("Tipo de evento inexistente.")
        r["tipo_id"] = tid
        r["_tipo_ativo"] = bool(t["ativo"])
    if completo or "processo_id" in c:
        pid = c.get("processo_id")
        if pid in (None, "", 0, "0"):
            r["processo_id"] = None
        else:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                raise ErroAgenda("Processo inválido.")
            if not b.conn.execute("SELECT 1 FROM processos WHERE id=?", (pid,)).fetchone():
                raise ErroAgenda("Processo não encontrado.")
            r["processo_id"] = pid
    if completo or "responsavel_id" in c:
        rid = c.get("responsavel_id")
        if rid in (None, "", 0, "0"):
            r["responsavel_id"] = None
        else:
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                raise ErroAgenda("Responsável inválido.")
            u = auth.obter(rid) if auth else {"ativo": True}
            if not u or not u.get("ativo"):
                raise ErroAgenda("Responsável inexistente ou desativado.")
            r["responsavel_id"] = rid
    if completo or "lembretes" in c:
        raw = c.get("lembretes", LEMBRETES_PADRAO)
        if not isinstance(raw, list):
            raise ErroAgenda("Lembretes inválidos.")
        try:
            ls = sorted({int(x) for x in raw}, reverse=True)
        except (TypeError, ValueError):
            raise ErroAgenda("Lembretes inválidos.")
        if any(x not in LEMBRETES_OK for x in ls):
            raise ErroAgenda("Lembrete com antecedência não permitida.")
        r["lembretes"] = json.dumps(ls)
    if completo or "calculo" in c:
        r["calculo"] = _calculo(c.get("calculo"))
    return r


def _calculo(x):
    """Memória de cálculo do prazo (sugestão): só os campos conhecidos, com tamanho limitado. Vazio = sem cálculo."""
    if not x:
        return ""
    if not isinstance(x, dict):
        raise ErroAgenda("Memória de cálculo inválida.")
    out = {}
    for k in ("disponibilizacao", "publicacao", "inicio", "vencimento"):
        if x.get(k):
            out[k] = _data(x[k])
    try:
        out["dias"] = int(x.get("dias"))
    except (TypeError, ValueError):
        raise ErroAgenda("Memória de cálculo inválida (dias).")
    memo = x.get("memoria") or []
    if not isinstance(memo, list) or len(memo) > 20:
        raise ErroAgenda("Memória de cálculo inválida.")
    out["memoria"] = [str(m)[:600] for m in memo]
    out["aviso"] = str(x.get("aviso") or "")[:400]
    return json.dumps(out, ensure_ascii=False)


def _log(b, eid, por, acao, detalhe=""):
    b.conn.execute("INSERT INTO eventos_log(evento_id,quando,por,acao,detalhe) VALUES(?,?,?,?,?)", (eid, agora(), por, acao, detalhe))


def criar(b, c, por, auth=None, origem=""):
    with b.lock:
        v = validar(b, c, auth)
        if not v.pop("_tipo_ativo"):
            raise ErroAgenda("Este tipo de evento está desativado.")
        cur = b.conn.execute(
            "INSERT INTO eventos(processo_id,tipo_id,titulo,descricao,data,hora,local,responsavel_id,lembretes,origem,"
            "criado_por,criado_em,atualizado_por,atualizado_em,calculo) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (v["processo_id"], v["tipo_id"], v["titulo"], v["descricao"], v["data"], v["hora"], v["local"], v["responsavel_id"],
             v["lembretes"], origem or ("prazo_calculado" if v["calculo"] else ""), por, agora(), por, agora(), v["calculo"]))
        eid = cur.lastrowid
        _log(b, eid, por, "criado", f"{v['titulo']} — {v['data']}" + (" (prazo calculado pelo programa; data confirmada pelo usuário)" if v["calculo"] else ""))
        b.conn.commit()
        return eid


_ROT = {"titulo": "título", "descricao": "teor", "data": "data", "hora": "hora", "local": "local", "tipo_id": "tipo",
        "processo_id": "processo", "responsavel_id": "responsável", "lembretes": "lembretes", "calculo": "cálculo"}


def editar(b, eid, c, por, auth=None):
    """Atualiza os campos enviados. c['versao'] deve ser a versão que a pessoa estava vendo."""
    with b.lock:
        antigo = b.conn.execute("SELECT * FROM eventos WHERE id=?", (eid,)).fetchone()
        if not antigo:
            raise ErroAgenda("Evento não encontrado.")
        try:
            vers = int(c.get("versao"))
        except (TypeError, ValueError):
            raise ErroAgenda("Falta a versão do evento (recarregue a página).")
        if vers != antigo["versao"]:
            raise ConflitoVersao(f"Este evento foi alterado por {antigo['atualizado_por'] or 'outra pessoa'} depois que você o abriu. "
                                 "Feche, reabra e refaça a alteração para não apagar o trabalho dela.")
        v = validar(b, c, auth, completo=False)
        ativo = v.pop("_tipo_ativo", True)
        if "tipo_id" in v and v["tipo_id"] != antigo["tipo_id"] and not ativo:
            raise ErroAgenda("Este tipo de evento está desativado.")
        mudou = {k: x for k, x in v.items() if antigo[k] != x}
        if not mudou:
            return antigo["versao"]
        sets = ", ".join(f"{k}=?" for k in mudou)
        b.conn.execute(f"UPDATE eventos SET {sets}, versao=versao+1, atualizado_por=?, atualizado_em=? WHERE id=?",
                       (*mudou.values(), por, agora(), eid))
        det = []
        for k, novo in mudou.items():
            if k == "descricao":
                det.append("teor alterado")
            elif k == "tipo_id":
                nome = lambda i: (b.conn.execute("SELECT nome FROM tipos_evento WHERE id=?", (i,)).fetchone() or ["?"])[0]  # noqa: E731
                det.append(f"tipo: {nome(antigo['tipo_id'])} → {nome(novo)}")
            elif k == "calculo":
                det.append("memória de cálculo atualizada")
            elif k in ("responsavel_id", "processo_id"):
                det.append(f"{_ROT[k]} alterado")
            else:
                det.append(f"{_ROT[k]}: {antigo[k] or '—'} → {novo or '—'}")
        _log(b, eid, por, "editado", "; ".join(det))
        b.conn.commit()
        return antigo["versao"] + 1


def mudar_situacao(b, eid, situacao, por, obs=""):
    if situacao not in SITUACOES:
        raise ErroAgenda("Situação inválida.")
    obs = str(obs or "").strip()[:500]
    with b.lock:
        e = b.conn.execute("SELECT * FROM eventos WHERE id=?", (eid,)).fetchone()
        if not e:
            raise ErroAgenda("Evento não encontrado.")
        if e["situacao"] == situacao:
            return
        if situacao == "concluido":
            b.conn.execute("UPDATE eventos SET situacao=?, concluido_por=?, concluido_em=?, conclusao_obs=?, versao=versao+1, "
                           "atualizado_por=?, atualizado_em=? WHERE id=?", (situacao, por, agora(), obs, por, agora(), eid))
        else:
            b.conn.execute("UPDATE eventos SET situacao=?, concluido_por='', concluido_em='', conclusao_obs='', versao=versao+1, "
                           "atualizado_por=?, atualizado_em=? WHERE id=?", (situacao, por, agora(), eid))
        _log(b, eid, por, {"concluido": "concluído", "aberto": "reaberto", "cancelado": "cancelado"}[situacao], obs)
        b.conn.commit()


def excluir(b, eid):
    with b.lock:
        if not b.conn.execute("SELECT 1 FROM eventos WHERE id=?", (eid,)).fetchone():
            raise ErroAgenda("Evento não encontrado.")
        b.conn.execute("DELETE FROM eventos WHERE id=?", (eid,))
        b.conn.commit()


_SEL = ("SELECT e.*, t.nome AS tipo_nome, t.cor AS tipo_cor, p.numero AS processo_numero, p.apelido AS processo_apelido, "
        "p.cliente AS processo_cliente, {resp} AS responsavel_nome FROM eventos e "
        "JOIN tipos_evento t ON t.id=e.tipo_id LEFT JOIN processos p ON p.id=e.processo_id ")


def _enriquecer(linhas, hoje):
    for e in linhas:
        e["atrasado"] = e["situacao"] == "aberto" and e["data"] < hoje
        try:
            e["lembretes"] = json.loads(e["lembretes"] or "[]")
        except ValueError:
            e["lembretes"] = []
        try:
            e["calculo"] = json.loads(e["calculo"]) if e.get("calculo") else None
        except ValueError:
            e["calculo"] = None
    return linhas


def obter(b, eid, hoje=None):
    hoje = hoje or dt.date.today().isoformat()
    r = b._linhas(_SEL.format(resp=_nome_resp(b)) + "WHERE e.id=?", (eid,))
    if not r:
        return None
    e = _enriquecer(r, hoje)[0]
    e["log"] = b._linhas("SELECT quando, por, acao, detalhe FROM eventos_log WHERE evento_id=? ORDER BY id DESC", (eid,))
    return e


def listar(b, de=None, ate=None, processo=None, tipos=None, resps=None, situacao="ativos", q="", hoje=None, limite=MAX_LISTA):
    """resps: lista com ids (int) e/ou 'sem'. tipos: lista de ids."""
    hoje = hoje or dt.date.today().isoformat()
    onde, args = ["1=1"], []
    if de:
        onde.append("e.data>=?")
        args.append(_data(de, "Data inicial"))
    if ate:
        onde.append("e.data<=?")
        args.append(_data(ate, "Data final"))
    if processo:
        onde.append("e.processo_id=?")
        args.append(int(processo))
    if tipos:
        onde.append("e.tipo_id IN (%s)" % ",".join("?" * len(tipos)))
        args += [int(t) for t in tipos]
    if resps:
        ids = [int(x) for x in resps if str(x) != "sem"]
        partes = []
        if ids:
            partes.append("e.responsavel_id IN (%s)" % ",".join("?" * len(ids)))
            args += ids
        if "sem" in [str(x) for x in resps]:
            partes.append("e.responsavel_id IS NULL")
        onde.append("(" + " OR ".join(partes) + ")")
    if situacao == "ativos":
        onde.append("e.situacao<>'cancelado'")
    elif situacao in SITUACOES:
        onde.append("e.situacao=?")
        args.append(situacao)
    q = " ".join(str(q or "").split())[:80]
    if q:
        onde.append("(e.titulo LIKE ? OR e.descricao LIKE ? OR p.numero LIKE ? OR p.apelido LIKE ? OR p.cliente LIKE ?)")
        args += [f"%{q}%"] * 5
    sql = _SEL.format(resp=_nome_resp(b)) + "WHERE " + " AND ".join(onde) + " ORDER BY e.data, e.hora='' , e.hora, e.id LIMIT ?"
    return _enriquecer(b._linhas(sql, (*args, int(limite))), hoje)


def resumo(b, hoje=None, resp=None, cliente=None):
    """Cartão 'Prazos e compromissos' do Início. resp: id ou 'sem'; cliente: nome exato (eventos sem processo ficam de fora)."""
    hoje_d = hoje or dt.date.today()
    hoje_s = hoje_d.isoformat()
    onde, args = ["e.situacao='aberto'"], []
    if resp == "sem":
        onde.append("e.responsavel_id IS NULL")
    elif resp:
        onde.append("e.responsavel_id=?")
        args.append(int(resp))
    if cliente:
        onde.append("p.cliente=?")
        args.append(cliente)
    base = _SEL.format(resp=_nome_resp(b)) + "WHERE " + " AND ".join(onde)
    vencidos = _enriquecer(b._linhas(base + " AND e.data<? ORDER BY e.data, e.hora LIMIT 200", (*args, hoje_s)), hoje_s)
    prox = _enriquecer(b._linhas(base + " AND e.data>=? ORDER BY e.data, e.hora='', e.hora, e.id LIMIT 200", (*args, hoje_s)), hoje_s)
    fim7 = (hoje_d + dt.timedelta(days=7)).isoformat()
    hoje_n = sum(1 for e in prox if e["data"] == hoje_s)
    sete = sum(1 for e in prox if hoje_s < e["data"] <= fim7)
    lista = (vencidos[:4] + prox)[:8]
    campos = ("id", "titulo", "data", "hora", "tipo_nome", "tipo_cor", "processo_id", "processo_numero", "processo_apelido",
              "responsavel_nome", "atrasado")
    return {"vencidos": len(vencidos), "hoje": hoje_n, "proximos7": sete,
            "lista": [{k: e[k] for k in campos} for e in lista]}


def contagem(b, hoje=None):
    """Selo da aba Calendário: eventos abertos vencidos e de hoje (todos os responsáveis)."""
    hoje = hoje or dt.date.today().isoformat()
    r = b.conn.execute("SELECT SUM(data<?), SUM(data=?) FROM eventos WHERE situacao='aberto'", (hoje, hoje)).fetchone()
    return {"vencidos": r[0] or 0, "hoje": r[1] or 0}


# ---------------------------------------------------------------- .ics
def _esc(t):
    return str(t or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _dobrar(linha):
    """RFC 5545: linhas com mais de 75 bytes são dobradas (continuação começa com um espaço)."""
    b = linha.encode("utf-8")
    if len(b) <= 75:
        return linha
    partes, i, lim = [], 0, 75
    while i < len(b):
        fim = min(i + lim, len(b))
        while fim < len(b) and (b[fim] & 0xC0) == 0x80:   # não corta no meio de um caractere
            fim -= 1
        partes.append(b[i:fim].decode("utf-8"))
        i, lim = fim, 74
    return "\r\n ".join(partes)


def ics(eventos, agora_utc=None):
    carimbo = (agora_utc or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Alvarez de Sa Advocacia//Monitor de Processos//PT-BR",
         "CALSCALE:GREGORIAN", "X-WR-CALNAME:Monitor de Processos"]
    for e in eventos:
        d = dt.date.fromisoformat(e["data"])
        L += ["BEGIN:VEVENT", f"UID:evento-{e['id']}@monitor-processos", f"DTSTAMP:{carimbo}"]
        if e.get("hora"):
            ini = dt.datetime.combine(d, dt.time.fromisoformat(e["hora"]))
            L += [f"DTSTART:{ini:%Y%m%dT%H%M%S}", f"DTEND:{ini + dt.timedelta(hours=1):%Y%m%dT%H%M%S}"]
        else:
            L += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + dt.timedelta(days=1):%Y%m%d}"]
        resumo_ = f"[{e['tipo_nome']}] {e['titulo']}" + (f" — {e['processo_apelido'] or e['processo_cliente']}" if (e.get("processo_apelido") or e.get("processo_cliente")) else "")
        desc = []
        if e.get("processo_numero"):
            desc.append("Processo: " + e["processo_numero"])
        if e.get("responsavel_nome"):
            desc.append("Responsável: " + e["responsavel_nome"])
        if e.get("descricao"):
            desc.append(e["descricao"])
        L.append("SUMMARY:" + _esc(resumo_))
        if desc:
            L.append("DESCRIPTION:" + _esc("\n".join(desc)))
        if e.get("local"):
            L.append("LOCATION:" + _esc(e["local"]))
        L.append("STATUS:" + ("CANCELLED" if e["situacao"] == "cancelado" else "CONFIRMED"))
        if e["situacao"] == "aberto":
            for n in sorted(e.get("lembretes") or [], reverse=True):
                # redundância dos lembretes: alarme do próprio calendário (às 9h do dia, se o evento não tem hora)
                if e.get("hora"):
                    gatilho = f"-P{n}D" if n else "-PT30M"
                else:
                    gatilho = f"-PT{n * 24 - 9}H" if n else "PT9H"
                L += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:" + _esc("Lembrete: " + e["titulo"]), "TRIGGER:" + gatilho, "END:VALARM"]
        L.append("END:VEVENT")
    L.append("END:VCALENDAR")
    return ("\r\n".join(_dobrar(x) for x in L) + "\r\n").encode("utf-8")
