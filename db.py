"""Banco SQLite local."""
import datetime as dt
import json
import re
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS processos(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  numero TEXT NOT NULL UNIQUE,
  numero_limpo TEXT NOT NULL,
  alias TEXT NOT NULL,
  apelido TEXT DEFAULT '',
  cliente TEXT DEFAULT '',
  pasta TEXT DEFAULT '',
  ativo INTEGER NOT NULL DEFAULT 1,
  criado_em TEXT NOT NULL,
  ultima_consulta TEXT,
  status TEXT NOT NULL DEFAULT 'pendente',
  mensagem TEXT DEFAULT '',
  classe TEXT DEFAULT '',
  orgao TEXT DEFAULT '',
  ultima_atualizacao TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS movimentos(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  processo_id INTEGER NOT NULL REFERENCES processos(id) ON DELETE CASCADE,
  chave TEXT NOT NULL,
  grau TEXT DEFAULT '',
  codigo TEXT DEFAULT '',
  nome TEXT DEFAULT '',
  data_hora TEXT DEFAULT '',
  complementos TEXT DEFAULT '',
  visto INTEGER NOT NULL DEFAULT 0,
  detectado_em TEXT NOT NULL,
  UNIQUE(processo_id, chave)
);
"""


TOLERANCIA_DIAS = 7
MOTIVOS = ("estrategico", "alto_valor", "tribunal_raro", "sem_oab", "outro")
TRIAGEM_DIAS = 7   # processo não monitorado e sem OAB identificada: confere no DJEN, no máximo, 1 vez por semana
BASELINE_PUB_DIAS = 15  # na 1ª consulta do DJEN, publicações dos últimos 15 dias contam como novas (prazo pode estar correndo)

SCHEMA_PUB = """
CREATE TABLE IF NOT EXISTS publicacoes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  processo_id INTEGER NOT NULL REFERENCES processos(id) ON DELETE CASCADE,
  pub_id TEXT NOT NULL,
  data_disp TEXT DEFAULT '',
  tipo_com TEXT DEFAULT '',
  orgao TEXT DEFAULT '',
  tipo_doc TEXT DEFAULT '',
  classe TEXT DEFAULT '',
  texto TEXT DEFAULT '',
  meio TEXT DEFAULT '',
  hash TEXT DEFAULT '',
  destinatarios TEXT DEFAULT '[]',
  advogados TEXT DEFAULT '[]',
  cancelada INTEGER NOT NULL DEFAULT 0,
  visto INTEGER NOT NULL DEFAULT 0,
  detectado_em TEXT NOT NULL,
  UNIQUE(processo_id, pub_id)
);
"""


OAB_JANELA_INICIAL_DIAS = 2   # ao cadastrar uma OAB, só contam publicações dos últimos 2 dias em diante
OAB_SOBREPOSICAO_DIAS = 3     # cada consulta volta 3 dias (cobre publicações que chegam com atraso; dedupe por id)

SCHEMA_OAB = """
CREATE TABLE IF NOT EXISTS oabs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  numero TEXT NOT NULL,
  uf TEXT NOT NULL,
  tipo TEXT NOT NULL DEFAULT 'oab',
  rotulo TEXT DEFAULT '',
  ativo INTEGER NOT NULL DEFAULT 1,
  criado_em TEXT NOT NULL,
  desde TEXT NOT NULL,
  ultima_consulta TEXT,
  status TEXT NOT NULL DEFAULT 'pendente',
  mensagem TEXT DEFAULT '',
  UNIQUE(numero, uf)
);
CREATE TABLE IF NOT EXISTS oab_pubs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  oab_id INTEGER NOT NULL REFERENCES oabs(id) ON DELETE CASCADE,
  pub_id TEXT NOT NULL,
  numero_processo TEXT DEFAULT '',
  tribunal TEXT DEFAULT '',
  data_disp TEXT DEFAULT '',
  tipo_com TEXT DEFAULT '',
  orgao TEXT DEFAULT '',
  tipo_doc TEXT DEFAULT '',
  classe TEXT DEFAULT '',
  texto TEXT DEFAULT '',
  meio TEXT DEFAULT '',
  destinatarios TEXT DEFAULT '[]',
  advogados TEXT DEFAULT '[]',
  cancelada INTEGER NOT NULL DEFAULT 0,
  visto INTEGER NOT NULL DEFAULT 0,
  detectado_em TEXT NOT NULL,
  UNIQUE(oab_id, pub_id)
);
"""

SCHEMA_DESC = """
CREATE TABLE IF NOT EXISTS descoberta_exec(
  oab_id INTEGER PRIMARY KEY REFERENCES oabs(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT '',
  quando TEXT,
  mensagem TEXT DEFAULT '',
  desde TEXT DEFAULT '',
  truncado INTEGER NOT NULL DEFAULT 0,
  ignorados INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS descobertos(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  oab_id INTEGER NOT NULL REFERENCES oabs(id) ON DELETE CASCADE,
  numero TEXT NOT NULL,
  numero_limpo TEXT NOT NULL,
  tribunal TEXT DEFAULT '',
  classe TEXT DEFAULT '',
  orgao TEXT DEFAULT '',
  n_pubs INTEGER NOT NULL DEFAULT 0,
  ultima_data TEXT DEFAULT '',
  UNIQUE(oab_id, numero_limpo)
);
"""


def inicio_do_dia_utc(agora_local=None):
    """Início do dia local (00:00), no mesmo formato UTC usado em 'ultima_consulta' (comparável como texto)."""
    a = agora_local or dt.datetime.now()
    ini = a.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.datetime.fromtimestamp(ini.timestamp(), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def agora():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _data(texto):
    try:
        return dt.date.fromisoformat(str(texto)[:10])
    except ValueError:
        return None


class Banco:
    def __init__(self, caminho):
        self.lock = threading.RLock()
        self.politica_fn = None  # função que devolve {"auto_sem_oab": bool, "auto_dias": int} (a configuração)
        self.conn = sqlite3.connect(str(caminho), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.executescript(SCHEMA_PUB)
        self.conn.executescript(SCHEMA_OAB)
        self.conn.executescript(SCHEMA_DESC)
        self._migrar()
        self.conn.commit()

    def _migrar(self):
        """v0.1.1: 'baseline_feito' registra se a 1ª consulta bem-sucedida já ocorreu.
        (Na v0.1.0 isso era deduzido do status, e uma falha passageira fazia a consulta
        seguinte tratar movimentações novas como histórico antigo, sem alertar.)"""
        colunas = {r["name"] for r in self.conn.execute("PRAGMA table_info(processos)").fetchall()}
        for nome, ddl in (("dje_status", "TEXT NOT NULL DEFAULT 'pendente'"), ("dje_consulta", "TEXT"),
                          ("dje_mensagem", "TEXT DEFAULT ''"), ("dje_baseline", "INTEGER NOT NULL DEFAULT 0")):
            if nome not in colunas:
                self.conn.execute(f"ALTER TABLE processos ADD COLUMN {nome} {ddl}")
        # v0.5.2: monitoramento por processo. Quem já estava cadastrado continua monitorado (DEFAULT 1).
        for nome, ddl in (("monitorar", "INTEGER NOT NULL DEFAULT 1"), ("motivo", "TEXT DEFAULT ''"),
                          ("motivo_obs", "TEXT DEFAULT ''"), ("monitorar_origem", "TEXT DEFAULT ''"),
                          ("oab_status", "TEXT DEFAULT ''"), ("oab_data", "TEXT DEFAULT ''"),
                          ("aviso", "TEXT DEFAULT ''"), ("aviso_em", "TEXT")):
            if nome not in colunas:
                self.conn.execute(f"ALTER TABLE processos ADD COLUMN {nome} {ddl}")
        cols_oab = {r["name"] for r in self.conn.execute("PRAGMA table_info(oabs)").fetchall()}
        if "tipo" not in cols_oab:
            self.conn.execute("ALTER TABLE oabs ADD COLUMN tipo TEXT NOT NULL DEFAULT 'oab'")
        if "baseline_feito" not in colunas:
            self.conn.execute("ALTER TABLE processos ADD COLUMN baseline_feito INTEGER NOT NULL DEFAULT 0")
            self.conn.execute("UPDATE processos SET baseline_feito=1 "
                              "WHERE status='ok' OR id IN (SELECT DISTINCT processo_id FROM movimentos)")

    def _linhas(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def adicionar(self, numero, limpo, alias, apelido="", cliente="", pasta="", monitorar=True, motivo="", motivo_obs=""):
        """Retorna o id, ou None se o processo já estiver cadastrado.
        monitorar=True: entra na consulta diária (cadastro manual). False: fica só no cadastro (importação, descobertos)."""
        motivo = motivo if motivo in MOTIVOS else ""
        with self.lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO processos(numero,numero_limpo,alias,apelido,cliente,pasta,criado_em,monitorar,motivo,"
                    "motivo_obs,monitorar_origem) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (numero, limpo, alias, apelido, cliente, pasta, agora(), 1 if monitorar else 0,
                     motivo if monitorar else "", str(motivo_obs or "")[:120] if monitorar else "",
                     "manual" if monitorar else ""))
                self.conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

    def remover(self, pid):
        with self.lock:
            self.conn.execute("DELETE FROM processos WHERE id=?", (pid,))
            self.conn.commit()

    def obter(self, pid):
        linhas = self._linhas("SELECT * FROM processos WHERE id=?", (pid,))
        return linhas[0] if linhas else None

    def ativos(self, ids=None, so_monitorados=False):
        linhas = self._linhas("SELECT * FROM processos WHERE ativo=1" + (" AND monitorar=1" if so_monitorados else "")
                              + " ORDER BY id")
        if ids is not None:
            linhas = [p for p in linhas if p["id"] in ids]
        return linhas

    def listar(self):
        return self._linhas("""
            SELECT p.*,
              (SELECT COUNT(*) FROM movimentos m WHERE m.processo_id=p.id AND m.visto=0) AS novos,
              (SELECT m.nome FROM movimentos m WHERE m.processo_id=p.id
                 ORDER BY m.data_hora DESC, m.id DESC LIMIT 1) AS ultimo_mov,
              (SELECT m.data_hora FROM movimentos m WHERE m.processo_id=p.id
                 ORDER BY m.data_hora DESC, m.id DESC LIMIT 1) AS ultimo_mov_data,
              (SELECT COUNT(*) FROM publicacoes b WHERE b.processo_id=p.id) AS pub_total,
              (SELECT COUNT(*) FROM publicacoes b WHERE b.processo_id=p.id AND b.visto=0 AND b.cancelada=0) AS pub_novas,
              (SELECT MAX(b.data_disp) FROM publicacoes b WHERE b.processo_id=p.id) AS pub_ultima
            FROM processos p ORDER BY novos DESC, p.apelido, p.numero""")

    def movimentos(self, pid):
        return self._linhas("SELECT * FROM movimentos WHERE processo_id=? "
                            "ORDER BY data_hora DESC, id DESC", (pid,))

    def marcar_visto(self, pid=None):
        with self.lock:
            if pid is None:
                self.conn.execute("UPDATE movimentos SET visto=1 WHERE visto=0")
            else:
                self.conn.execute("UPDATE movimentos SET visto=1 WHERE processo_id=? AND visto=0", (pid,))
            self.conn.commit()

    def registrar_resultado(self, pid, status, mensagem="", dados=None):
        """Grava o resultado de uma consulta. Retorna a lista de movimentos NOVOS.

        Na primeira consulta bem-sucedida de um processo, o histórico existente vira
        'linha de base' (já visto), para não gerar alerta de tudo que já aconteceu.
        """
        novos = []
        with self.lock:
            p = self.obter(pid)
            if p is None:
                return novos
            baseline = not p["baseline_feito"]
            if dados:
                # Referência para detectar "histórico que apareceu tarde": movimento cuja data é bem
                # anterior ao mais recente já conhecido não é novidade (ex.: a base devolveu só parte
                # do histórico antes). Fica registrado, mas já como visto e sem alerta.
                linha = self.conn.execute("SELECT MAX(data_hora) AS d FROM movimentos WHERE processo_id=?",
                                          (pid,)).fetchone()
                ref = _data(linha["d"]) if linha and linha["d"] else None
                limite = (ref - dt.timedelta(days=TOLERANCIA_DIAS)) if ref else None
                for m in dados["movimentos"]:
                    d = _data(m["data_hora"])
                    antigo = (not baseline) and limite is not None and d is not None and d < limite
                    cur = self.conn.execute(
                        "INSERT OR IGNORE INTO movimentos(processo_id,chave,grau,codigo,nome,data_hora,"
                        "complementos,visto,detectado_em) VALUES(?,?,?,?,?,?,?,?,?)",
                        (pid, m["chave"], m["grau"], m["codigo"], m["nome"], m["data_hora"],
                         m["complementos"], 1 if (baseline or antigo) else 0, agora()))
                    if cur.rowcount and not baseline and not antigo:
                        novos.append(m)
                self.conn.execute(
                    "UPDATE processos SET classe=?, orgao=?, ultima_atualizacao=?, baseline_feito=1 WHERE id=?",
                    (dados["classe"], dados["orgao"], dados["ultima_atualizacao"], pid))
            self.conn.execute(
                "UPDATE processos SET ultima_consulta=?, status=?, mensagem=? WHERE id=?",
                (agora(), status, mensagem, pid))
            self.conn.commit()
        return novos

    def ultima_consulta_geral(self):
        linhas = self._linhas("SELECT MAX(ultima_consulta) AS u FROM processos")
        return linhas[0]["u"] if linhas else None

    # ------------------------------------------------------------ DJEN
    def registrar_publicacoes(self, pid, itens):
        """Grava as publicações do DJEN de um processo. Devolve as NOVAS (para alerta).

        Na 1ª consulta bem-sucedida, publicações com mais de BASELINE_PUB_DIAS dias entram como
        já vistas; as mais recentes contam como novas (podem ter prazo em curso)."""
        novas = []
        with self.lock:
            p = self.obter(pid)
            if p is None:
                return novas
            baseline = not p["dje_baseline"]
            hoje = dt.date.today()
            for it in itens:
                existe = self.conn.execute("SELECT 1 FROM publicacoes WHERE processo_id=? AND pub_id=?",
                                           (pid, it["pub_id"])).fetchone()
                if existe:
                    self.conn.execute("UPDATE publicacoes SET cancelada=?, texto=?, tipo_doc=?, orgao=?, hash=? "
                                      "WHERE processo_id=? AND pub_id=?",
                                      (it["cancelada"], it["texto"], it["tipo_doc"], it["orgao"], it["hash"],
                                       pid, it["pub_id"]))
                    continue
                d = _data(it["data_disp"])
                antiga = baseline and (d is None or (hoje - d).days > BASELINE_PUB_DIAS)
                self.conn.execute(
                    "INSERT INTO publicacoes(processo_id,pub_id,data_disp,tipo_com,orgao,tipo_doc,classe,texto,meio,"
                    "hash,destinatarios,advogados,cancelada,visto,detectado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, it["pub_id"], it["data_disp"], it["tipo_com"], it["orgao"], it["tipo_doc"], it["classe"],
                     it["texto"], it["meio"], it["hash"], it["destinatarios"], it["advogados"], it["cancelada"],
                     1 if antiga else 0, agora()))
                if not antiga and not it["cancelada"]:
                    novas.append(it)
            self.conn.execute("UPDATE processos SET dje_baseline=1 WHERE id=?", (pid,))
            self.conn.commit()
        return novas

    def registrar_dje_status(self, pid, status, mensagem=""):
        with self.lock:
            self.conn.execute("UPDATE processos SET dje_status=?, dje_consulta=?, dje_mensagem=? WHERE id=?",
                              (status, agora(), mensagem, pid))
            self.conn.commit()

    def publicacoes(self, pid=None, so_novas=False, limite=300):
        sql = ("SELECT b.*, p.numero, p.apelido, p.cliente FROM publicacoes b "
               "JOIN processos p ON p.id=b.processo_id WHERE 1=1")
        args = []
        if pid is not None:
            sql += " AND b.processo_id=?"
            args.append(pid)
        if so_novas:
            sql += " AND b.visto=0 AND b.cancelada=0"
        sql += " ORDER BY b.data_disp DESC, b.id DESC LIMIT ?"
        args.append(int(limite))
        return self._linhas(sql, tuple(args))

    def marcar_pub_vistas(self, pid=None):
        with self.lock:
            if pid is None:
                self.conn.execute("UPDATE publicacoes SET visto=1 WHERE visto=0")
            else:
                self.conn.execute("UPDATE publicacoes SET visto=1 WHERE processo_id=? AND visto=0", (pid,))
            self.conn.commit()

    # ------------------------------------------------------------ OAB
    def adicionar_oab(self, numero, uf, rotulo="", tipo="oab"):
        """Filtro de pesquisa (OAB, CPF/CNPJ ou texto). Devolve o id, ou None se já estiver cadastrado."""
        desde = (dt.date.today() - dt.timedelta(days=OAB_JANELA_INICIAL_DIAS)).isoformat()
        with self.lock:
            try:
                cur = self.conn.execute("INSERT INTO oabs(numero,uf,tipo,rotulo,criado_em,desde) VALUES(?,?,?,?,?,?)",
                                        (numero, uf, tipo, rotulo, agora(), desde))
            except sqlite3.IntegrityError:
                return None
            self.conn.commit()
            return cur.lastrowid

    def remover_oab(self, oid):
        with self.lock:
            self.conn.execute("DELETE FROM oabs WHERE id=?", (oid,))
            self.conn.commit()

    def obter_oab(self, oid):
        r = self._linhas("SELECT * FROM oabs WHERE id=?", (oid,))
        return r[0] if r else None

    def oabs_ativas(self, ids=None):
        r = self._linhas("SELECT * FROM oabs WHERE ativo=1 ORDER BY id")
        return [o for o in r if ids is None or o["id"] in ids]

    def oabs(self):
        return self._linhas("""
            SELECT o.*,
              (SELECT COUNT(*) FROM oab_pubs b WHERE b.oab_id=o.id) AS pub_total,
              (SELECT COUNT(*) FROM oab_pubs b WHERE b.oab_id=o.id AND b.visto=0 AND b.cancelada=0) AS pub_novas
            FROM oabs o ORDER BY o.id""")

    def registrar_oab_status(self, oid, status, mensagem="", avancar_desde=False):
        with self.lock:
            self.conn.execute("UPDATE oabs SET status=?, mensagem=?, ultima_consulta=? WHERE id=?",
                              (status, mensagem, agora(), oid))
            if avancar_desde:
                novo = (dt.date.today() - dt.timedelta(days=OAB_SOBREPOSICAO_DIAS)).isoformat()
                self.conn.execute("UPDATE oabs SET desde=? WHERE id=? AND desde<?", (novo, oid, novo))
            self.conn.commit()

    def registrar_oab_pubs(self, oid, itens, cadastradas_novas=None):
        """Grava as publicações da OAB. Devolve as NOVAS de processos NÃO cadastrados (para alerta).

        Processo cadastrado e monitorado: a publicação é tratada na consulta por processo (aqui só se registra que a OAB
        apareceu nele). Processo cadastrado mas NÃO monitorado: a publicação entra no próprio processo e, se for nova, é
        acrescentada em `cadastradas_novas` como (processo, item) — é assim que ele é acompanhado sem consulta diária.
        Comunicações repetidas (mesmo processo, data e texto, para destinatários diferentes) não alertam de novo."""
        novas = []
        with self.lock:
            cadastrados = {r["numero_limpo"]: dict(r) for r in self.conn.execute(
                "SELECT id, numero, apelido, cliente, numero_limpo, monitorar FROM processos WHERE ativo=1")}
            proprias = self.oabs_proprias()
            vistos_lote = set()
            for it in itens:
                reg = cadastrados.get(it["numero"])
                if reg:
                    if proprias and _cita_oab(it.get("advogados"), proprias):
                        self._aplicar_oab(reg["id"], "com", it["data_disp"])
                    if not reg["monitorar"] and self._inserir_pub_processo(reg["id"], it) and not it["cancelada"]:
                        if cadastradas_novas is not None:
                            cadastradas_novas.append((reg, it))
                    continue
                if self.conn.execute("SELECT 1 FROM oab_pubs WHERE oab_id=? AND pub_id=?",
                                     (oid, it["pub_id"])).fetchone():
                    self.conn.execute("UPDATE oab_pubs SET cancelada=?, texto=? WHERE oab_id=? AND pub_id=?",
                                      (it["cancelada"], it["texto"], oid, it["pub_id"]))
                    continue
                chave = (it["numero"], it["data_disp"], " ".join(it["texto"].split()))
                duplicada = chave in vistos_lote or self.conn.execute(
                    "SELECT 1 FROM oab_pubs WHERE oab_id=? AND numero_processo=? AND data_disp=? AND texto=?",
                    (oid, it["numero"], it["data_disp"], it["texto"])).fetchone() is not None
                vistos_lote.add(chave)
                self.conn.execute(
                    "INSERT INTO oab_pubs(oab_id,pub_id,numero_processo,tribunal,data_disp,tipo_com,orgao,tipo_doc,"
                    "classe,texto,meio,destinatarios,advogados,cancelada,visto,detectado_em) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (oid, it["pub_id"], it["numero"], it["tribunal"], it["data_disp"], it["tipo_com"], it["orgao"],
                     it["tipo_doc"], it["classe"], it["texto"], it["meio"], it["destinatarios"], it["advogados"],
                     it["cancelada"], 1 if duplicada else 0, agora()))
                if not duplicada and not it["cancelada"]:
                    novas.append(it)
            self.conn.commit()
        return novas

    def _inserir_pub_processo(self, pid, it):
        """Grava a publicação (vinda do filtro de OAB) no próprio processo. Não mexe na 'linha de base' do DJEN do processo.
        Devolve True se era inédita."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO publicacoes(processo_id,pub_id,data_disp,tipo_com,orgao,tipo_doc,classe,texto,meio,hash,"
            "destinatarios,advogados,cancelada,visto,detectado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (pid, it["pub_id"], it["data_disp"], it["tipo_com"], it["orgao"], it["tipo_doc"], it["classe"], it["texto"],
             it["meio"], it.get("hash", ""), it["destinatarios"], it.get("advogados", "[]"), it["cancelada"], agora()))
        return cur.rowcount > 0

    def oab_publicacoes(self, oid=None, so_novas=False, limite=300):
        sql = ("SELECT b.*, o.numero AS oab_numero, o.uf AS oab_uf, o.tipo AS filtro_tipo, o.rotulo AS oab_rotulo, "
               "EXISTS(SELECT 1 FROM processos p WHERE p.numero_limpo=b.numero_processo) AS cadastrado "
               "FROM oab_pubs b JOIN oabs o ON o.id=b.oab_id WHERE 1=1")
        args = []
        if oid is not None:
            sql += " AND b.oab_id=?"
            args.append(oid)
        if so_novas:
            sql += " AND b.visto=0 AND b.cancelada=0"
        sql += " ORDER BY b.data_disp DESC, b.id DESC LIMIT ?"
        args.append(int(limite))
        return self._linhas(sql, tuple(args))

    def marcar_oab_vistas(self, oid=None):
        with self.lock:
            if oid is None:
                self.conn.execute("UPDATE oab_pubs SET visto=1 WHERE visto=0")
            else:
                self.conn.execute("UPDATE oab_pubs SET visto=1 WHERE oab_id=? AND visto=0", (oid,))
            self.conn.commit()


    # ------------------------------------------------------------ v0.5: arquivar, contagens, varredura, descoberta
    def definir_ativo(self, ids, ativo):
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        with self.lock:
            n = 0
            for i in range(0, len(ids), 500):
                lote = ids[i:i + 500]
                cur = self.conn.execute(
                    f"UPDATE processos SET ativo=? WHERE id IN ({','.join('?' * len(lote))}) AND ativo<>?",
                    [1 if ativo else 0, *lote, 1 if ativo else 0])
                n += cur.rowcount
            self.conn.commit()
        return n

    def total_pub_novas(self):
        return self._linhas("SELECT COUNT(*) AS n FROM publicacoes WHERE visto=0 AND cancelada=0")[0]["n"]

    def total_mov_novos(self):
        return self._linhas("SELECT COUNT(DISTINCT processo_id) AS n FROM movimentos WHERE visto=0")[0]["n"]

    def sem_andamento(self, desde_iso):
        """Processos ativos já consultados com sucesso cuja última movimentação E última publicação são anteriores a `desde_iso`
        (AAAA-MM-DD). Quem não tem nenhuma data conhecida fica de fora (não há como afirmar que está parado)."""
        return self._linhas("""
            SELECT id, numero, apelido, cliente, ult FROM (
              SELECT p.id, p.numero, p.apelido, p.cliente,
                MAX(COALESCE((SELECT MAX(substr(m.data_hora,1,10)) FROM movimentos m WHERE m.processo_id=p.id), ''),
                    COALESCE((SELECT MAX(substr(b.data_disp,1,10)) FROM publicacoes b WHERE b.processo_id=p.id), '')) AS ult
              FROM processos p WHERE p.ativo=1 AND p.baseline_feito=1)
            WHERE ult<>'' AND ult<? ORDER BY ult""", (desde_iso,))

    def candidatos_varredura(self, inicio_dia_utc):
        """Processos ativos ainda não consultados hoje: nunca consultados primeiro, depois os de consulta mais antiga."""
        return self._linhas(
            "SELECT id, ultima_consulta, status FROM processos WHERE ativo=1 AND monitorar=1 AND COALESCE(ultima_consulta,'')<? "
            "ORDER BY COALESCE(ultima_consulta,'') , id", (inicio_dia_utc,))

    def consultados_desde(self, inicio_dia_utc):
        return self._linhas("SELECT COUNT(*) AS n FROM processos WHERE ativo=1 AND monitorar=1 AND COALESCE(ultima_consulta,'')>=?",
                            (inicio_dia_utc,))[0]["n"]

    def registrar_descoberta_status(self, oid, status, mensagem="", desde="", truncado=False, ignorados=0):
        with self.lock:
            self.conn.execute(
                "INSERT INTO descoberta_exec(oab_id,status,quando,mensagem,desde,truncado,ignorados) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(oab_id) DO UPDATE SET status=excluded.status, quando=excluded.quando, mensagem=excluded.mensagem, "
                "desde=excluded.desde, truncado=excluded.truncado, ignorados=excluded.ignorados",
                (oid, status, agora(), mensagem, desde, 1 if truncado else 0, ignorados))
            self.conn.commit()

    def registrar_descobertos(self, oid, itens):
        """Substitui a lista de processos descobertos por este filtro. itens: dicts com numero, numero_limpo, ..."""
        with self.lock:
            self.conn.execute("DELETE FROM descobertos WHERE oab_id=?", (oid,))
            self.conn.executemany(
                "INSERT INTO descobertos(oab_id,numero,numero_limpo,tribunal,classe,orgao,n_pubs,ultima_data) VALUES(?,?,?,?,?,?,?,?)",
                [(oid, i["numero"], i["numero_limpo"], i["tribunal"], i["classe"], i["orgao"], i["n_pubs"], i["ultima_data"])
                 for i in itens])
            self.conn.commit()

    def descobertos(self, oid):
        info = self._linhas("SELECT * FROM descoberta_exec WHERE oab_id=?", (oid,))
        itens = self._linhas("""
            SELECT d.*, CASE WHEN p.id IS NULL THEN 'novo' WHEN p.ativo=1 THEN 'cadastrado' ELSE 'arquivado' END AS situacao
            FROM descobertos d LEFT JOIN processos p ON p.numero_limpo=d.numero_limpo
            WHERE d.oab_id=? ORDER BY d.ultima_data DESC, d.numero""", (oid,))
        return {"info": info[0] if info else None, "itens": itens}

    def listar_basico(self):
        """Só id/numero/numero_limpo/ativo (rápido; sem as contagens da listagem completa)."""
        return self._linhas("SELECT id, numero, numero_limpo, ativo FROM processos")

    # ------------------------------------------------------------ v0.5.2: monitoramento por processo e OAB automática
    def definir_monitorar(self, ids, monitorar, motivo="", obs=""):
        """Liga/desliga o monitoramento diário (ação do usuário). Fica registrado como 'manual': a automação não desfaz."""
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        motivo = motivo if motivo in MOTIVOS else ""
        with self.lock:
            n = 0
            for i in range(0, len(ids), 500):
                lote = ids[i:i + 500]
                cur = self.conn.execute(
                    f"UPDATE processos SET monitorar=?, motivo=?, motivo_obs=?, monitorar_origem='manual', aviso='', aviso_em=NULL "
                    f"WHERE id IN ({','.join('?' * len(lote))})",
                    [1 if monitorar else 0, motivo if monitorar else "", str(obs or "")[:120] if monitorar else "", *lote])
                n += cur.rowcount
            self.conn.commit()
        return n

    def dispensar_avisos(self, ids):
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        with self.lock:
            cur = self.conn.execute(f"UPDATE processos SET aviso='', aviso_em=NULL WHERE id IN ({','.join('?' * len(ids))})", ids)
            self.conn.commit()
            return cur.rowcount

    def _politica(self):
        pol = {"auto_sem_oab": True, "auto_dias": 180}
        try:
            pol.update(self.politica_fn() or {} if self.politica_fn else {})
        except Exception:  # noqa: BLE001
            pass
        return pol

    def oabs_proprias(self):
        """OABs cadastradas como filtro (tipo 'oab'): conjunto de (número só com dígitos, UF)."""
        res = set()
        for o in self.conn.execute("SELECT numero, uf FROM oabs WHERE tipo='oab'"):
            d = re.sub(r"\D", "", o["numero"]).lstrip("0")
            if d:
                res.add((d, str(o["uf"]).upper()))
        return res

    def avaliar_oab(self, pid):
        """Confere, nas publicações já gravadas do processo, se alguma cita uma das OABs cadastradas.
        'com' = a OAB aparece; 'sem' = há publicações e nenhuma cita a OAB; '' = nenhuma publicação ainda.
        Aplica a política automática (ver _aplicar_oab). Não faz nada se não houver OAB cadastrada como filtro."""
        with self.lock:
            proprias = self.oabs_proprias()
            p = self.obter(pid)
            if not proprias or not p:
                return
            rows = self.conn.execute("SELECT data_disp, advogados FROM publicacoes WHERE processo_id=? AND cancelada=0",
                                     (pid,)).fetchall()
            if not rows:
                return
            datas = [r["data_disp"] for r in rows if _cita_oab(r["advogados"], proprias)]
            if datas:
                self._aplicar_oab(pid, "com", max(datas))
            elif p["oab_status"] != "com":
                self._aplicar_oab(pid, "sem", "")
            self.conn.commit()

    def _aplicar_oab(self, pid, novo, data):
        """Grava a situação da OAB no processo e aplica a política:
        - 'sem' + publicação recente + não monitorado + sem decisão manual -> entra no monitoramento (motivo 'sem_oab', origem 'auto');
        - 'com' num processo monitorado por 'sem_oab': se a entrada foi automática, sai do monitoramento (com aviso);
          se foi decisão manual, só aparece o aviso. Em ambos o aviso fica no painel até ser dispensado."""
        p = self.obter(pid)
        if not p:
            return
        antigo = p["oab_status"] or ""
        data_ant = p["oab_data"] or ""
        if novo == "com":
            data = max(data or "", data_ant)
        if not (novo == antigo and (novo != "com" or data == data_ant)):
            self.conn.execute("UPDATE processos SET oab_status=?, oab_data=? WHERE id=?", (novo, data if novo == "com" else "", pid))
        pol = self._politica()
        if novo == "com" and antigo != "com" and p["monitorar"] and p["motivo"] == "sem_oab":
            quando = _br(data)
            if p["monitorar_origem"] == "auto" and pol.get("auto_sem_oab", True):
                self.conn.execute("UPDATE processos SET monitorar=0, aviso=?, aviso_em=? WHERE id=?",
                                  (f"Saiu do monitoramento diário: a OAB apareceu em publicação de {quando}. "
                                   "As publicações passam a chegar pelo filtro de OAB (movimentos do DataJud deixam de ser acompanhados).",
                                   agora(), pid))
            else:
                self.conn.execute("UPDATE processos SET aviso=?, aviso_em=? WHERE id=?",
                                  (f"A OAB apareceu em publicação de {quando}. Este processo foi marcado como “sem OAB”: "
                                   "considere parar de monitorá-lo diariamente.", agora(), pid))
        elif novo == "sem" and pol.get("auto_sem_oab", True) and p["ativo"] and not p["monitorar"] and p["monitorar_origem"] != "manual":
            ult = self.conn.execute("SELECT MAX(data_disp) AS d FROM publicacoes WHERE processo_id=? AND cancelada=0",
                                    (pid,)).fetchone()["d"]
            d = _data(ult)
            if d and (dt.date.today() - d).days <= int(pol.get("auto_dias", 180)):
                self.conn.execute(
                    "UPDATE processos SET monitorar=1, motivo='sem_oab', monitorar_origem='auto', aviso=?, aviso_em=? WHERE id=?",
                    (f"Entrou no monitoramento diário: há publicação recente ({_br(ult)}) e nenhuma cita a OAB cadastrada. "
                     "Se você não quiser acompanhar este processo, desmarque “monitorar”.", agora(), pid))

    def candidatos_triagem(self, corte_utc):
        """Processos ativos, NÃO monitorados e sem OAB identificada, cuja última conferência no DJEN é anterior ao corte
        (nunca conferidos primeiro). A conferência é só no DJEN (rápida) e serve para descobrir se a OAB aparece e se
        há atividade recente."""
        return self._linhas(
            "SELECT id, dje_consulta FROM processos WHERE ativo=1 AND monitorar=0 AND COALESCE(oab_status,'')<>'com' "
            "AND COALESCE(dje_consulta,'')<? ORDER BY COALESCE(dje_consulta,''), id", (corte_utc,))

    def total_monitorados(self):
        return self._linhas("SELECT COUNT(*) AS n FROM processos WHERE ativo=1 AND monitorar=1")[0]["n"]

    def total_avisos(self):
        return self._linhas("SELECT COUNT(*) AS n FROM processos WHERE COALESCE(aviso,'')<>''")[0]["n"]


def _br(data):
    d = str(data or "")[:10]
    return f"{d[8:10]}/{d[5:7]}/{d[0:4]}" if len(d) == 10 else d


def _cita_oab(advogados_json, proprias):
    """True se a lista de advogados da publicação (JSON) inclui alguma das OABs (número só com dígitos + UF)."""
    try:
        lista = json.loads(advogados_json or "[]")
    except (TypeError, ValueError):
        return False
    for a in lista if isinstance(lista, list) else []:
        num, _, uf = str((a or {}).get("oab", "")).partition("/")
        if (re.sub(r"\D", "", num).lstrip("0"), uf.strip().upper()) in proprias:
            return True
    return False
