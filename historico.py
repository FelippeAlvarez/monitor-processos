"""Histórico do processo (v0.7.1): linha do tempo única com andamentos (DataJud), publicações (DJEN),
eventos da agenda e anotações internas.

- Andamentos e publicações vêm do banco (nada é consultado ao CNJ aqui). Publicações iguais viram uma só linha (campo `copias`).
- Anotações internas não têm data própria de negócio: entram pela data/hora em que foram escritas.
- Paginação no servidor por cursor (`antes`), do mais novo para o mais antigo.
- Edição simultânea de anotação: campo `versao`, como nos eventos (ConflitoVersao).
"""
import datetime as dt
import re

from agenda import ConflitoVersao, ErroAgenda, agora

SCHEMA_HIST = """
CREATE TABLE IF NOT EXISTS anotacoes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  processo_id INTEGER NOT NULL REFERENCES processos(id) ON DELETE CASCADE,
  texto TEXT NOT NULL,
  criado_por TEXT NOT NULL DEFAULT '',
  criado_em TEXT NOT NULL,
  atualizado_por TEXT NOT NULL DEFAULT '',
  atualizado_em TEXT NOT NULL DEFAULT '',
  versao INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_anot_proc ON anotacoes(processo_id, criado_em);
CREATE INDEX IF NOT EXISTS ix_mov_proc_data ON movimentos(processo_id, data_hora);
CREATE INDEX IF NOT EXISTS ix_pub_proc_disp ON publicacoes(processo_id, data_disp);
CREATE INDEX IF NOT EXISTS ix_ev_proc ON eventos(processo_id, data);
"""
FONTES = ("andamento", "publicacao", "evento", "anotacao")
MAX_LIMITE = 200
TEXTO_MAX = 6000
ANOT_MAX = 4000


def migrar(b):
    with b.lock:
        b.conn.executescript(SCHEMA_HIST)
        b.conn.commit()


def _local(utc):
    """'2026-11-24T14:25:00Z' -> hora local do computador, sem fuso ('2026-11-24T11:25:00')."""
    try:
        d = dt.datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).astimezone()
        return d.strftime("%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return (utc or "")[:19]


def _q19(s):
    s = (s or "").strip()
    return (s + "T00:00:00")[:19] if len(s) == 10 else s[:19]


def _dentro(quando, de, ate):
    d = quando[:10]
    return (not de or d >= de) and (not ate or d <= ate)


def _tem(q, *campos):
    return not q or any(q in (c or "").lower() for c in campos)


def linha(b, pid, fontes=None, de=None, ate=None, q="", antes=None, limite=40):
    """Itens do histórico (mais novos primeiro). `antes`: cursor devolvido na página anterior. Devolve (itens, proximo)."""
    limite = max(1, min(int(limite or 40), MAX_LIMITE))
    fontes = [f for f in (fontes or FONTES) if f in FONTES] or list(FONTES)
    q = (q or "").strip().lower()
    cur = None
    if antes:
        try:
            qq, fonte, i = str(antes).split("~")
            cur = (qq, fonte, int(i))
        except ValueError:
            raise ErroAgenda("Cursor de página inválido.")
    de, ate = (de or "")[:10], (ate or "")[:10]
    itens = []
    folga = limite + 200   # folga para empates de data/hora no cursor
    lim_q = cur[0] if cur else (_q19(ate) + "~" if ate else "9999")
    if "andamento" in fontes:
        sql, args = ["SELECT * FROM movimentos WHERE processo_id=? AND substr(data_hora,1,19)<=?"], [pid, lim_q[:19] if cur else (ate + "T23:59:59" if ate else "9999")]
        if de:
            sql.append("AND substr(data_hora,1,10)>=?"); args.append(de)
        if q:
            sql.append("AND lower(nome||' '||complementos) LIKE ?"); args.append("%" + q + "%")
        sql.append("ORDER BY data_hora DESC, id DESC LIMIT ?"); args.append(folga)
        for m in b._linhas(" ".join(sql), tuple(args)):
            itens.append({"fonte": "andamento", "id": m["id"], "quando": (m["data_hora"] or "")[:19], "titulo": m["nome"] or "Andamento",
                          "texto": (m["complementos"] or "")[:TEXTO_MAX], "novo": not m["visto"], "grau": m["grau"]})
    if "publicacao" in fontes:
        sql, args = ["SELECT MIN(id) AS id, grupo, MAX(data_disp) AS data_disp, COUNT(*) AS copias, orgao, tipo_com, tipo_doc, classe, texto,"
                     " situacao, MAX(cancelada) AS cancelada, MAX(link) AS link, MAX(hash) AS hash FROM publicacoes WHERE processo_id=? AND substr(data_disp,1,10)<=?"], \
                    [pid, cur[0][:10] if cur else (ate or "9999")]
        if de:
            sql.append("AND substr(data_disp,1,10)>=?"); args.append(de)
        if q:
            sql.append("AND lower(texto||' '||orgao||' '||tipo_com) LIKE ?"); args.append("%" + q + "%")
        sql.append("GROUP BY grupo ORDER BY data_disp DESC, id DESC LIMIT ?"); args.append(folga)
        for p in b._linhas(" ".join(sql), tuple(args)):
            itens.append({"fonte": "publicacao", "id": p["id"], "quando": (p["data_disp"] or "")[:10] + "T00:00:00",
                          "titulo": (p["tipo_com"] or "Publicação") + (f" — {p['orgao']}" if p["orgao"] else ""), "texto": re.sub(r"^Processo:\s*\S+\s*Conteudo:\s*", "", p["texto"] or "", flags=re.I)[:TEXTO_MAX],
                          "copias": p["copias"], "situacao": p["situacao"], "cancelada": bool(p["cancelada"]), "classe": p["classe"],
                          "link": p["link"] or "", "hash": p["hash"] or ""})
    if "evento" in fontes:
        for e in b._linhas("SELECT e.*, t.nome AS tipo_nome, t.cor AS tipo_cor, "
                           "(SELECT u.nome FROM usuarios u WHERE u.id=e.responsavel_id) AS responsavel_nome "
                           "FROM eventos e JOIN tipos_evento t ON t.id=e.tipo_id WHERE e.processo_id=?", (pid,)):
            quando = e["data"] + "T" + ((e["hora"] or "00:00") + ":00")
            if _dentro(quando, de, ate) and _tem(q, e["titulo"], e["descricao"], e["local"]):
                itens.append({"fonte": "evento", "id": e["id"], "quando": quando, "titulo": e["titulo"], "texto": (e["descricao"] or "")[:TEXTO_MAX],
                              "tipo_nome": e["tipo_nome"], "tipo_cor": e["tipo_cor"], "situacao": e["situacao"], "hora": e["hora"],
                              "responsavel_nome": e["responsavel_nome"], "local": e["local"],
                              "atrasado": e["situacao"] == "aberto" and e["data"] < dt.date.today().isoformat()})
    if "anotacao" in fontes:
        for a in b._linhas("SELECT * FROM anotacoes WHERE processo_id=?", (pid,)):
            quando = _local(a["criado_em"])
            if _dentro(quando, de, ate) and _tem(q, a["texto"], a["criado_por"]):
                itens.append({"fonte": "anotacao", "id": a["id"], "quando": quando, "titulo": "Anotação interna", "texto": a["texto"],
                              "por": a["criado_por"], "editada": a["versao"] > 1, "versao": a["versao"], "criado_por": a["criado_por"]})
    ordem = {"evento": 3, "anotacao": 2, "publicacao": 1, "andamento": 0}
    chave = lambda x: (x["quando"], ordem[x["fonte"]], x["id"])  # noqa: E731
    if cur:
        itens = [x for x in itens if chave(x) < (cur[0], ordem.get(cur[1], 0), cur[2])]
    itens.sort(key=chave, reverse=True)
    pagina = itens[:limite]
    proximo = None
    if len(itens) > limite:
        u = pagina[-1]
        proximo = f"{u['quando']}~{u['fonte']}~{u['id']}"
    return pagina, proximo


def contagens(b, pid):
    c = lambda sql: b.conn.execute(sql, (pid,)).fetchone()[0]  # noqa: E731
    with b.lock:
        return {"andamento": c("SELECT COUNT(*) FROM movimentos WHERE processo_id=?"),
                "publicacao": c("SELECT COUNT(DISTINCT grupo) FROM publicacoes WHERE processo_id=?"),
                "evento": c("SELECT COUNT(*) FROM eventos WHERE processo_id=?"),
                "anotacao": c("SELECT COUNT(*) FROM anotacoes WHERE processo_id=?")}


def _texto(t):
    t = str(t or "").strip()
    if not t:
        raise ErroAgenda("Escreva o texto da anotação.")
    if len(t) > ANOT_MAX:
        raise ErroAgenda(f"A anotação passa de {ANOT_MAX} caracteres.")
    return t


def criar_anotacao(b, pid, texto, por):
    texto = _texto(texto)
    with b.lock:
        if not b.conn.execute("SELECT 1 FROM processos WHERE id=?", (pid,)).fetchone():
            raise ErroAgenda("Processo não encontrado.")
        cur = b.conn.execute("INSERT INTO anotacoes(processo_id,texto,criado_por,criado_em,atualizado_por,atualizado_em) VALUES(?,?,?,?,?,?)",
                             (pid, texto, por, agora(), por, agora()))
        b.conn.commit()
        return cur.lastrowid


def obter_anotacao(b, aid):
    r = b._linhas("SELECT * FROM anotacoes WHERE id=?", (aid,))
    return r[0] if r else None


def editar_anotacao(b, aid, texto, versao, por):
    texto = _texto(texto)
    with b.lock:
        a = b.conn.execute("SELECT * FROM anotacoes WHERE id=?", (aid,)).fetchone()
        if not a:
            raise ErroAgenda("Anotação não encontrada.")
        if versao is None or int(versao) != a["versao"]:
            raise ConflitoVersao(f"Esta anotação foi alterada por {a['atualizado_por'] or 'outra pessoa'} depois que você a abriu. Recarregue para ver a versão atual.")
        b.conn.execute("UPDATE anotacoes SET texto=?, atualizado_por=?, atualizado_em=?, versao=versao+1 WHERE id=?", (texto, por, agora(), aid))
        b.conn.commit()
        return a["versao"] + 1


def excluir_anotacao(b, aid):
    with b.lock:
        b.conn.execute("DELETE FROM anotacoes WHERE id=?", (aid,))
        b.conn.commit()
