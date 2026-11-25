"""Partes, advogados e dados editáveis do processo (v0.7.4).

De onde vêm os nomes: o DataJud público NÃO informa as partes. Elas vêm das publicações do DJEN, por duas vias:
  1. o campo "destinatários" (nome + polo A/P) — numa "Lista de distribuição" costuma trazer todas as partes;
     numa intimação, só quem foi intimado;
  2. o cabeçalho do texto (TJRJ/eproc): linhas "EXEQUENTE : NOME", "EXECUTADO : NOME", "ADVOGADO(A) : NOME (OAB RJ123)".
Tudo isso é uma LEITURA AUTOMÁTICA e pode ficar incompleta (processo em que só o nosso cliente foi intimado). Por isso o usuário
pode editar tudo: o que ele edita (origem 'manual') nunca é sobrescrito pela leitura automática, e o que ele exclui não volta.

Lado: 'cliente' ou 'contraria' (independe do polo: um litisconsorte do mesmo polo pode ser parte contrária ao nosso cliente).
O lado automático parte do polo do nosso cliente, deduzido (nesta ordem de peso) de: nossa OAB listada sob um polo no cabeçalho,
nossa OAB entre os advogados de uma publicação (os destinatários dela são nossos clientes) e coincidência do nome do campo
"cliente" com o nome de uma parte.
"""
import json
import re
import sqlite3
import unicodedata

from agenda import ConflitoVersao, ErroAgenda, agora

SCHEMA = """
CREATE TABLE IF NOT EXISTS processo_partes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  processo_id INTEGER NOT NULL REFERENCES processos(id) ON DELETE CASCADE,
  nome TEXT NOT NULL,
  chave TEXT NOT NULL,
  polo TEXT NOT NULL DEFAULT '',
  lado TEXT NOT NULL DEFAULT '',
  origem TEXT NOT NULL DEFAULT 'auto',
  excluida INTEGER NOT NULL DEFAULT 0,
  UNIQUE(processo_id, chave)
);
CREATE TABLE IF NOT EXISTS processo_advogados(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  processo_id INTEGER NOT NULL REFERENCES processos(id) ON DELETE CASCADE,
  nome TEXT NOT NULL,
  oab TEXT NOT NULL DEFAULT '',
  chave TEXT NOT NULL,
  polo TEXT NOT NULL DEFAULT '',
  lado TEXT NOT NULL DEFAULT '',
  origem TEXT NOT NULL DEFAULT 'auto',
  excluida INTEGER NOT NULL DEFAULT 0,
  UNIQUE(processo_id, chave)
);
CREATE TABLE IF NOT EXISTS partes_meta(chave TEXT PRIMARY KEY, valor TEXT);
CREATE INDEX IF NOT EXISTS ix_partes_proc ON processo_partes(processo_id);
CREATE INDEX IF NOT EXISTS ix_adv_proc ON processo_advogados(processo_id);
"""

POLOS = ("A", "P", "")
LADOS = ("cliente", "contraria", "")
MAX_ITENS = 40
NOME_MAX = 200
MAX_PUBS = 400          # publicações lidas por processo na leitura automática (as mais recentes)
LINHAS_CABECALHO = 40   # só o começo do texto é examinado

_A = {"EXEQUENTE", "EXEQUENTES", "AUTOR", "AUTORA", "AUTORES", "REQUERENTE", "REQUERENTES", "IMPETRANTE", "RECLAMANTE"}
_P = {"EXECUTADO", "EXECUTADA", "EXECUTADOS", "REU", "RE", "REUS", "REQUERIDO", "REQUERIDA", "REQUERIDOS", "IMPETRADO",
      "IMPETRADA", "RECLAMADO", "RECLAMADA", "RECLAMADOS"}
_LINHA = re.compile(r"^\s*([A-ZÀ-Ý][A-ZÀ-Ý()/ ]{1,28}?)\s*:\s*(\S.*?)\s*$")
_OAB_TXT = re.compile(r"^(.*?)\s*\(\s*OAB\s*[/:\- ]*([A-Z]{2})\s*[/:\- ]*0*(\d{1,7})[A-Z]?\s*\)\s*$", re.I)


def sem_acento(s):
    t = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in t if not unicodedata.combining(c))


def chave_nome(s):
    return re.sub(r"[^A-Z0-9]+", " ", sem_acento(s).upper()).strip()


def _papel(rotulo):
    r = re.sub(r"\([^)]*\)", "", sem_acento(rotulo).upper()).strip()
    return r


def _oab_norm(num, uf):
    d = re.sub(r"\D", "", str(num or "")).lstrip("0")
    uf = str(uf or "").strip().upper()
    return (d, uf) if d and len(uf) == 2 else None


def _oab_de_texto(s):
    """'12345/RJ' ou 'RJ12345' -> (12345, RJ) ou None."""
    s = str(s or "").upper()
    m = re.search(r"(\d{1,7})\s*[/\-]?\s*([A-Z]{2})\b", s) or None
    if m:
        return _oab_norm(m.group(1), m.group(2))
    m = re.search(r"\b([A-Z]{2})\s*[/\-]?\s*(\d{1,7})", s)
    return _oab_norm(m.group(2), m.group(1)) if m else None


def titulo_classe(s):
    """Classe vinda do DJEN às vezes tem caixa estranha ('EXECUçãO DE TíTULO'): normaliza para 'Execução de título'."""
    s = str(s or "").strip()
    if not s:
        return ""
    if s == s.upper() or re.search(r"[A-ZÀ-Ý]{3,}", s):
        s = s.lower()
    return s[0].upper() + s[1:]


def migrar(b):
    with b.lock:
        b.conn.executescript(SCHEMA)
        cols = {r["name"] for r in b.conn.execute("PRAGMA table_info(processos)").fetchall()}
        for nome, ddl in (("vara", "TEXT DEFAULT ''"), ("comarca", "TEXT DEFAULT ''"), ("tipo_acao", "TEXT DEFAULT ''"),
                          ("dados_versao", "INTEGER NOT NULL DEFAULT 1")):
            if nome not in cols:
                b.conn.execute(f"ALTER TABLE processos ADD COLUMN {nome} {ddl}")
        b.conn.commit()
        if not b.conn.execute("SELECT 1 FROM partes_meta WHERE chave='inicial_v074'").fetchone():
            ids = [r["processo_id"] for r in b.conn.execute("SELECT DISTINCT processo_id FROM publicacoes").fetchall()]
            for pid in ids:
                try:
                    atualizar(b, pid, commit=False)
                except Exception:   # noqa: BLE001 — uma publicação estranha não pode impedir o programa de abrir
                    pass
            b.conn.execute("INSERT OR REPLACE INTO partes_meta(chave, valor) VALUES('inicial_v074', ?)", (agora(),))
            b.conn.commit()


# ---------------------------------------------------------------- leitura automática
def _json_lista(s):
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def _voto(d, polo, peso):
    if polo in ("A", "P"):
        d[polo] = d.get(polo, 0) + peso


def _add_parte(partes, nome, polo, peso):
    nome = re.sub(r"\s+", " ", str(nome or "")).strip()[:NOME_MAX]
    k = chave_nome(nome)
    if not k:
        return
    e = partes.setdefault(k, {"nome": nome, "votos": {}})
    _voto(e["votos"], polo, peso)


def _add_adv(advs, nome, oab, polo, peso):
    nome = re.sub(r"\s+", " ", str(nome or "")).strip()[:NOME_MAX]
    if not nome:
        return
    k = "OAB%s%s" % (oab[1], oab[0]) if oab else "N:" + chave_nome(nome)
    e = advs.setdefault(k, {"nome": nome, "oab": oab, "votos": {}})
    if oab and not e["oab"]:
        e["oab"] = oab
    _voto(e["votos"], polo, peso)


def _polo(votos):
    a, p = votos.get("A", 0), votos.get("P", 0)
    return "A" if a > p else "P" if p > a else ""


def _coletar(rows, proprias):
    partes, advs, cliente_votos = {}, {}, {"A": 0, "P": 0}
    for r in rows:
        dest = [d for d in _json_lista(r["destinatarios"]) if isinstance(d, dict)]
        advj = [a for a in _json_lista(r["advogados"]) if isinstance(a, dict)]
        nossa = False
        for a in advj:
            o = _oab_de_texto(a.get("oab"))
            nossa = nossa or bool(o and o in proprias)
            _add_adv(advs, a.get("nome"), o, "", 0)
        polos_dest = set()
        for d in dest:
            polo = d.get("polo") if d.get("polo") in ("A", "P") else ""
            _add_parte(partes, d.get("nome"), polo, 2)
            if polo:
                polos_dest.add(polo)
        if nossa:
            for p in polos_dest:
                cliente_votos[p] += 1
        polo_atual = ""
        for linha in str(r["texto"] or "").splitlines()[:LINHAS_CABECALHO]:
            m = _LINHA.match(linha)
            if not m:
                continue
            papel = _papel(m.group(1))
            valor = m.group(2)
            if papel in _A or papel in _P:
                polo_atual = "A" if papel in _A else "P"
                _add_parte(partes, valor, polo_atual, 1)
            elif papel.startswith("ADVOGAD"):
                mo = _OAB_TXT.match(valor)
                if mo:
                    o = _oab_norm(mo.group(3), mo.group(2))
                    _add_adv(advs, mo.group(1), o, polo_atual, 2)
                    if o and o in proprias and polo_atual:
                        cliente_votos[polo_atual] += 2
                else:
                    _add_adv(advs, valor, None, polo_atual, 1)
    return partes, advs, cliente_votos


def _polo_do_cliente(votos, partes, cliente_txt):
    v = dict(votos)
    ck = chave_nome(cliente_txt)
    if len(ck) >= 6:
        for p in partes.values():
            pk = chave_nome(p["nome"])
            polo = _polo(p["votos"])
            if polo and pk and (ck in pk or pk in ck):
                v[polo] += 3
    return _polo(v)


def _lado_auto(polo, polo_cliente):
    if not polo_cliente or not polo:
        return ""
    return "cliente" if polo == polo_cliente else "contraria"


def atualizar(b, pid, commit=True):
    """Relê as publicações do processo e completa partes/advogados AUTOMÁTICOS. Nunca altera nem recria o que foi editado
    ou excluído pelo usuário. Devolve True se leu algo."""
    with b.lock:
        p = b.obter(pid)
        if not p:
            return False
        rows = b.conn.execute("SELECT texto, destinatarios, advogados FROM publicacoes WHERE processo_id=? AND cancelada=0 "
                              "ORDER BY data_disp DESC, id DESC LIMIT ?", (pid, MAX_PUBS)).fetchall()
        proprias = b.oabs_proprias()
        partes, advs, votos = _coletar(rows, proprias)
        polo_cli = _polo_do_cliente(votos, partes, p.get("cliente"))
        for k, e in partes.items():
            polo = _polo(e["votos"])
            lado = _lado_auto(polo, polo_cli)
            ex = b.conn.execute("SELECT id, origem, excluida FROM processo_partes WHERE processo_id=? AND chave=?", (pid, k)).fetchone()
            if ex is None:
                b.conn.execute("INSERT INTO processo_partes(processo_id,nome,chave,polo,lado,origem) VALUES(?,?,?,?,?,'auto')",
                               (pid, e["nome"], k, polo, lado))
            elif ex["origem"] == "auto" and not ex["excluida"]:
                b.conn.execute("UPDATE processo_partes SET polo=?, lado=? WHERE id=?", (polo, lado, ex["id"]))
        for k, e in advs.items():
            polo = _polo(e["votos"])
            nosso = bool(e["oab"] and e["oab"] in proprias)
            lado = "cliente" if nosso else _lado_auto(polo, polo_cli)
            oab = f"{e['oab'][0]}/{e['oab'][1]}" if e["oab"] else ""
            ex = b.conn.execute("SELECT id, origem, excluida FROM processo_advogados WHERE processo_id=? AND chave=?", (pid, k)).fetchone()
            if ex is None:
                b.conn.execute("INSERT INTO processo_advogados(processo_id,nome,oab,chave,polo,lado,origem) VALUES(?,?,?,?,?,?,'auto')",
                               (pid, e["nome"], oab, k, polo, lado))
            elif ex["origem"] == "auto" and not ex["excluida"]:
                b.conn.execute("UPDATE processo_advogados SET polo=?, lado=?, oab=? WHERE id=?", (polo, lado, oab, ex["id"]))
        if commit:
            b.conn.commit()
        return bool(rows)


# ---------------------------------------------------------------- leitura para a interface
def _carregar(b, ids=None):
    onde, args = "", []
    if ids is not None:
        ids = list(ids)
        if not ids:
            return {}
        if len(ids) <= 500:
            onde = " AND processo_id IN (%s)" % ",".join("?" * len(ids))
            args = ids
    res = {}
    for tabela, campo in (("processo_partes", "partes"), ("processo_advogados", "advogados")):
        cols = "id, processo_id, nome, polo, lado, origem" + (", oab" if campo == "advogados" else "")
        for r in b.conn.execute(f"SELECT {cols} FROM {tabela} WHERE excluida=0{onde} ORDER BY id", args).fetchall():
            d = res.setdefault(r["processo_id"], {"partes": [], "advogados": []})
            item = {k: r[k] for k in r.keys() if k != "processo_id"}
            d[campo].append(item)
    return res


def resumo(partes):
    """Agrupa os nomes para exibição: por lado (cliente/contrária) quando já definido; senão por polo."""
    g = {"cliente": [], "contraria": [], "ativo": [], "passivo": [], "outros": []}
    for p in partes:
        if p["lado"] == "cliente":
            g["cliente"].append(p["nome"])
        elif p["lado"] == "contraria":
            g["contraria"].append(p["nome"])
        elif p["polo"] == "A":
            g["ativo"].append(p["nome"])
        elif p["polo"] == "P":
            g["passivo"].append(p["nome"])
        else:
            g["outros"].append(p["nome"])
    return g


def _extras_pub(b):
    """Vara e classe vistas nas publicações (usadas quando o DataJud ainda não trouxe)."""
    return {r["processo_id"]: (r["o"] or "", r["c"] or "") for r in
            b.conn.execute("SELECT processo_id, MAX(orgao) AS o, MAX(classe) AS c FROM publicacoes GROUP BY processo_id").fetchall()}


def _campos_exibicao(p, extra):
    o, c = extra
    vara_auto = p.get("orgao") or o
    tipo_auto = p.get("classe") or titulo_classe(c)
    return {"vara_auto": vara_auto, "tipo_auto": tipo_auto,
            "vara_exib": (p.get("vara") or "").strip() or vara_auto,
            "tipo_exib": (p.get("tipo_acao") or "").strip() or tipo_auto}


def enriquecer(b, lista):
    """Acrescenta a cada processo (dict da listagem): partes, advogados, resumo_partes, vara/tipo/comarca de exibição."""
    with b.lock:
        mapa = _carregar(b)
        extras = _extras_pub(b)
        for p in lista:
            d = mapa.get(p["id"], {"partes": [], "advogados": []})
            p["partes"], p["advogados"] = d["partes"], d["advogados"]
            p["resumo_partes"] = resumo(d["partes"])
            p.update(_campos_exibicao(p, extras.get(p["id"], ("", ""))))
            p["comarca_exib"] = (p.get("comarca") or "").strip()
            p["dados_editados"] = bool(any(x["origem"] == "manual" for x in d["partes"] + d["advogados"])
                                       or p.get("vara") or p.get("comarca") or p.get("tipo_acao"))
    return lista


def enriquecer_publicacoes(b, pubs):
    """Acrescenta a cada publicação o resumo das partes do PROCESSO e a vara/tipo de ação (para identificar a parte contrária)."""
    if not pubs:
        return pubs
    with b.lock:
        ids = sorted({p["processo_id"] for p in pubs})
        mapa = _carregar(b, ids)
        extras = _extras_pub(b)
        procs = {r["id"]: dict(r) for r in b.conn.execute("SELECT id, classe, orgao, vara, tipo_acao, comarca FROM processos").fetchall()}
        for pb in pubs:
            d = mapa.get(pb["processo_id"], {"partes": [], "advogados": []})
            pb["resumo_partes"] = resumo(d["partes"])
            pr = procs.get(pb["processo_id"], {})
            pb.update({k: v for k, v in _campos_exibicao(pr, extras.get(pb["processo_id"], ("", ""))).items()
                       if k in ("vara_exib", "tipo_exib")})
            pb["comarca_exib"] = (pr.get("comarca") or "").strip()
    return pubs


# ---------------------------------------------------------------- edição
def obter_dados(b, pid):
    with b.lock:
        p = b.obter(pid)
        if not p:
            return None
        d = _carregar(b, [pid]).get(pid, {"partes": [], "advogados": []})
        pe = dict(p)
        pe.update(_campos_exibicao(pe, _extras_pub(b).get(pid, ("", ""))))
        return {"processo": {"id": p["id"], "numero": p["numero"], "apelido": p["apelido"] or "", "cliente": p["cliente"] or "",
                             "pasta": p["pasta"] or "", "vara": p.get("vara") or "", "comarca": p.get("comarca") or "",
                             "tipo_acao": p.get("tipo_acao") or "", "vara_auto": pe["vara_auto"], "tipo_auto": pe["tipo_auto"],
                             "versao": p["dados_versao"]},
                "partes": d["partes"], "advogados": d["advogados"]}


def _texto(v, maximo, obrigatorio=False, rotulo="Campo"):
    v = re.sub(r"\s+", " ", str(v or "")).strip()
    if obrigatorio and not v:
        raise ErroAgenda(f"{rotulo}: informe o nome.")
    if len(v) > maximo:
        raise ErroAgenda(f"{rotulo}: no máximo {maximo} caracteres.")
    return v


def _validar_itens(lista, rotulo, adv=False):
    if not isinstance(lista, list):
        raise ErroAgenda(f"{rotulo}: lista inválida.")
    if len(lista) > MAX_ITENS:
        raise ErroAgenda(f"{rotulo}: no máximo {MAX_ITENS}.")
    saida, vistos = [], set()
    for it in lista:
        if not isinstance(it, dict):
            raise ErroAgenda(f"{rotulo}: item inválido.")
        nome = _texto(it.get("nome"), NOME_MAX, True, rotulo)
        polo = str(it.get("polo") or "")
        lado = str(it.get("lado") or "")
        if polo not in POLOS or lado not in LADOS:
            raise ErroAgenda(f"{rotulo}: polo ou lado inválido.")
        oab = ""
        if adv:
            bruto = _texto(it.get("oab"), 20, rotulo=rotulo)
            o = _oab_de_texto(bruto) if bruto else None
            if bruto and not o:
                raise ErroAgenda(f"{rotulo}: OAB '{bruto}' inválida. Use o formato 12345/RJ.")
            oab = f"{o[0]}/{o[1]}" if o else ""
            k = "OAB%s%s" % (o[1], o[0]) if o else "N:" + chave_nome(nome)
        else:
            k = chave_nome(nome)
        if not k:
            raise ErroAgenda(f"{rotulo}: nome inválido.")
        if k in vistos:
            raise ErroAgenda(f"{rotulo}: '{nome}' aparece repetido.")
        vistos.add(k)
        try:
            iid = int(it.get("id")) if it.get("id") not in (None, "") else None
        except (TypeError, ValueError):
            iid = None
        saida.append({"id": iid, "nome": nome, "polo": polo, "lado": lado, "oab": oab, "chave": k})
    return saida


def _gravar_lista(b, pid, tabela, itens, adv):
    existentes = {r["id"]: dict(r) for r in b.conn.execute(
        f"SELECT * FROM {tabela} WHERE processo_id=?", (pid,)).fetchall()}
    por_chave = {r["chave"]: r for r in existentes.values()}
    usados = set()
    for it in itens:
        ex = existentes.get(it["id"]) if it["id"] in existentes else None
        outro = por_chave.get(it["chave"])
        if ex is None:
            ex = outro
        elif outro is not None and outro["id"] != ex["id"]:
            # o nome editado coincide com outra linha (inclusive uma excluída): a outra é reaproveitada, esta sai
            if ex["id"] not in usados:
                b.conn.execute(f"DELETE FROM {tabela} WHERE id=?", (ex["id"],))
                usados.add(ex["id"])
            ex = outro
        if ex is not None:
            campos = "nome=?, chave=?, polo=?, lado=?, origem='manual', excluida=0"
            args = [it["nome"], it["chave"], it["polo"], it["lado"]]
            if adv:
                campos += ", oab=?"
                args.append(it["oab"])
            b.conn.execute(f"UPDATE {tabela} SET {campos} WHERE id=?", args + [ex["id"]])
            usados.add(ex["id"])
        else:
            if adv:
                b.conn.execute(f"INSERT INTO {tabela}(processo_id,nome,oab,chave,polo,lado,origem) VALUES(?,?,?,?,?,?,'manual')",
                               (pid, it["nome"], it["oab"], it["chave"], it["polo"], it["lado"]))
            else:
                b.conn.execute(f"INSERT INTO {tabela}(processo_id,nome,chave,polo,lado,origem) VALUES(?,?,?,?,?,'manual')",
                               (pid, it["nome"], it["chave"], it["polo"], it["lado"]))
    for r in existentes.values():
        if r["id"] in usados or r["excluida"]:
            continue
        if r["origem"] == "auto":   # some da tela mas fica registrada como excluída, para a leitura automática não recriar
            b.conn.execute(f"UPDATE {tabela} SET excluida=1, origem='manual' WHERE id=?", (r["id"],))
        else:
            b.conn.execute(f"DELETE FROM {tabela} WHERE id=?", (r["id"],))


def salvar(b, pid, corpo):
    """Grava os dados editados do processo. `corpo['versao']` deve ser a versão que o usuário viu (senão ConflitoVersao)."""
    apelido = _texto(corpo.get("apelido"), 120, rotulo="Apelido")
    cliente = _texto(corpo.get("cliente"), 120, rotulo="Cliente")
    pasta = _texto(corpo.get("pasta"), 60, rotulo="Pasta")
    vara = _texto(corpo.get("vara"), 160, rotulo="Vara")
    comarca = _texto(corpo.get("comarca"), 80, rotulo="Comarca")
    tipo = _texto(corpo.get("tipo_acao"), 80, rotulo="Tipo de ação")
    partes = _validar_itens(corpo.get("partes", []), "Partes")
    advs = _validar_itens(corpo.get("advogados", []), "Advogados", adv=True)
    try:
        versao = int(corpo.get("versao"))
    except (TypeError, ValueError):
        raise ErroAgenda("Versão do processo ausente. Feche e abra a edição de novo.")
    with b.lock:
        p = b.obter(pid)
        if not p:
            raise ErroAgenda("Processo não encontrado.")
        if p["dados_versao"] != versao:
            raise ConflitoVersao("Outra pessoa alterou os dados deste processo enquanto você editava. Feche e abra a edição de novo.")
        try:
            b.conn.execute("UPDATE processos SET apelido=?, cliente=?, pasta=?, vara=?, comarca=?, tipo_acao=?, "
                           "dados_versao=dados_versao+1 WHERE id=?", (apelido, cliente, pasta, vara, comarca, tipo, pid))
            _gravar_lista(b, pid, "processo_partes", partes, False)
            _gravar_lista(b, pid, "processo_advogados", advs, True)
            atualizar(b, pid, commit=False)   # completa o que for novo, sem mexer no que foi editado
            b.conn.commit()
        except sqlite3.IntegrityError:
            b.conn.rollback()
            raise ErroAgenda("Há nomes repetidos entre as partes ou os advogados. Confira a lista.")
        return p["dados_versao"] + 1


def restaurar_automatico(b, pid):
    """Descarta as edições de partes, advogados, vara, comarca e tipo de ação, e refaz a leitura automática."""
    with b.lock:
        p = b.obter(pid)
        if not p:
            raise ErroAgenda("Processo não encontrado.")
        b.conn.execute("DELETE FROM processo_partes WHERE processo_id=?", (pid,))
        b.conn.execute("DELETE FROM processo_advogados WHERE processo_id=?", (pid,))
        b.conn.execute("UPDATE processos SET vara='', comarca='', tipo_acao='', dados_versao=dados_versao+1 WHERE id=?", (pid,))
        atualizar(b, pid, commit=False)
        b.conn.commit()
        return p["dados_versao"] + 1
