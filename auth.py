"""Usuários, login, sessões e registro de ações (auditoria) — v0.4.

Só biblioteca padrão. Senhas com scrypt (hashlib) e sal individual; nunca são guardadas em texto.
As sessões guardam apenas o hash (SHA-256) do token; o token em si só existe no cookie do navegador.
"""
import hashlib
import hmac
import re
import secrets
import threading
import time

PERFIS = ("admin", "usuario")  # "cliente" (só consulta os próprios processos) está previsto para a v0.9
SENHA_MIN = 10
FALHAS_MAX = 5                 # após 5 senhas erradas seguidas, a conta fica bloqueada por 15 minutos
BLOQUEIO_SEG = 15 * 60
SESSAO_OCIOSA_SEG = 14 * 24 * 3600   # a sessão expira após 14 dias sem uso
SESSAO_MAX_SEG = 60 * 24 * 3600      # e, de qualquer forma, após 60 dias
IP_JANELA_SEG = 15 * 60
IP_FALHAS_MAX = 30             # falhas de login por IP na janela (protege contra tentativas em muitas contas)
RE_USUARIO = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}")
RE_EMAIL = re.compile(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+")

SCHEMA_AUTH = """
CREATE TABLE IF NOT EXISTS usuarios(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  usuario TEXT NOT NULL UNIQUE,
  nome TEXT NOT NULL,
  email TEXT NOT NULL DEFAULT '',
  perfil TEXT NOT NULL DEFAULT 'usuario',
  senha_hash TEXT NOT NULL,
  ativo INTEGER NOT NULL DEFAULT 1,
  trocar_senha INTEGER NOT NULL DEFAULT 0,
  falhas INTEGER NOT NULL DEFAULT 0,
  bloqueado_ate REAL NOT NULL DEFAULT 0,
  criado_em TEXT NOT NULL,
  ultimo_acesso TEXT
);
CREATE TABLE IF NOT EXISTS sessoes(
  token_hash TEXT PRIMARY KEY,
  usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
  criada REAL NOT NULL,
  ultimo_uso REAL NOT NULL,
  ip TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS auditoria(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  quando TEXT NOT NULL,
  usuario_id INTEGER,
  usuario TEXT NOT NULL DEFAULT '',
  acao TEXT NOT NULL,
  detalhe TEXT NOT NULL DEFAULT '',
  ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_auditoria_id ON auditoria(id DESC);
"""


class ErroAuth(ValueError):
    """Erro de validação com mensagem própria para o usuário."""


def _agora_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def gerar_hash(senha, n=2 ** 14, r=8, p=1):
    sal = secrets.token_bytes(16)
    h = hashlib.scrypt(senha.encode("utf-8"), salt=sal, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${sal.hex()}${h.hex()}"


def conferir_hash(senha, guardado):
    try:
        alg, n, r, p, sal, h = guardado.split("$")
        if alg != "scrypt":
            return False
        calc = hashlib.scrypt(senha.encode("utf-8"), salt=bytes.fromhex(sal), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, bytes.fromhex(h))
    except (ValueError, TypeError):
        return False


_HASH_FALSO = None


def validar_senha(senha, usuario=""):
    if not isinstance(senha, str) or len(senha) < SENHA_MIN:
        raise ErroAuth(f"A senha deve ter pelo menos {SENHA_MIN} caracteres.")
    if len(senha) > 200:
        raise ErroAuth("Senha longa demais.")
    if usuario and senha.lower() == usuario.lower():
        raise ErroAuth("A senha não pode ser igual ao nome de usuário.")
    if len(set(senha)) < 4:
        raise ErroAuth("Senha fraca demais: use caracteres variados.")


def _publico(u):
    return {k: u[k] for k in ("id", "usuario", "nome", "email", "perfil", "ativo", "trocar_senha", "criado_em",
                              "ultimo_acesso")} | {"bloqueado": u["bloqueado_ate"] > time.time()}


class Auth:
    def __init__(self, banco):
        self.banco = banco
        self.lock = banco.lock
        self._ip_falhas = {}
        self._ip_lock = threading.Lock()
        with self.lock:
            banco.conn.executescript(SCHEMA_AUTH)
            banco.conn.commit()

    # ------------------------------------------------------------ consultas
    def _um(self, sql, args=()):
        with self.lock:
            r = self.banco.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    def tem_usuarios(self):
        return self._um("SELECT 1 AS x FROM usuarios LIMIT 1") is not None

    def listar(self):
        with self.lock:
            return [_publico(dict(r)) for r in self.banco.conn.execute("SELECT * FROM usuarios ORDER BY usuario").fetchall()]

    def obter(self, uid):
        u = self._um("SELECT * FROM usuarios WHERE id=?", (uid,))
        return _publico(u) if u else None

    def emails_ativos(self):
        """E-mails dos usuários ativos (para os avisos da equipe nas próximas versões)."""
        with self.lock:
            return [r["email"] for r in self.banco.conn.execute(
                "SELECT email FROM usuarios WHERE ativo=1 AND email<>''").fetchall()]

    def _admins_ativos(self, exceto=None):
        sql = "SELECT COUNT(*) AS n FROM usuarios WHERE perfil='admin' AND ativo=1"
        args = ()
        if exceto is not None:
            sql += " AND id<>?"
            args = (exceto,)
        return self._um(sql, args)["n"]

    # ------------------------------------------------------------ cadastro
    def _validar_dados(self, usuario, nome, email, perfil):
        usuario = str(usuario or "").strip().lower()
        nome = " ".join(str(nome or "").split())[:80]
        email = str(email or "").strip()
        if not RE_USUARIO.fullmatch(usuario):
            raise ErroAuth("Usuário inválido: use 3 a 32 caracteres (letras minúsculas, números, ponto, hífen ou sublinhado).")
        if not nome:
            raise ErroAuth("Informe o nome.")
        if email and not RE_EMAIL.fullmatch(email):
            raise ErroAuth("E-mail inválido.")
        if perfil not in PERFIS:
            raise ErroAuth("Perfil inválido.")
        return usuario, nome, email

    def criar(self, usuario, nome, email, perfil, senha, trocar_senha=True):
        usuario, nome, email = self._validar_dados(usuario, nome, email, perfil)
        validar_senha(senha, usuario)
        h = gerar_hash(senha)
        with self.lock:
            if self._um("SELECT 1 AS x FROM usuarios WHERE usuario=?", (usuario,)):
                raise ErroAuth("Já existe um usuário com esse nome de login.")
            cur = self.banco.conn.execute(
                "INSERT INTO usuarios(usuario,nome,email,perfil,senha_hash,trocar_senha,criado_em) VALUES(?,?,?,?,?,?,?)",
                (usuario, nome, email, perfil, h, 1 if trocar_senha else 0, _agora_iso()))
            self.banco.conn.commit()
            return cur.lastrowid

    def atualizar(self, uid, nome=None, email=None, perfil=None, ativo=None):
        with self.lock:
            atual = self._um("SELECT * FROM usuarios WHERE id=?", (uid,))
            if not atual:
                raise ErroAuth("Usuário não encontrado.")
            novo_nome = atual["nome"] if nome is None else nome
            novo_email = atual["email"] if email is None else email
            novo_perfil = atual["perfil"] if perfil is None else perfil
            novo_ativo = atual["ativo"] if ativo is None else (1 if ativo else 0)
            _, novo_nome, novo_email = self._validar_dados(atual["usuario"], novo_nome, novo_email, novo_perfil)
            era_admin_ativo = atual["perfil"] == "admin" and atual["ativo"]
            segue_admin_ativo = novo_perfil == "admin" and novo_ativo
            if era_admin_ativo and not segue_admin_ativo and self._admins_ativos(exceto=uid) == 0:
                raise ErroAuth("Não é possível remover o último administrador ativo.")
            self.banco.conn.execute("UPDATE usuarios SET nome=?,email=?,perfil=?,ativo=? WHERE id=?",
                                    (novo_nome, novo_email, novo_perfil, novo_ativo, uid))
            if not novo_ativo:
                self.banco.conn.execute("DELETE FROM sessoes WHERE usuario_id=?", (uid,))
            self.banco.conn.commit()

    def redefinir_senha(self, uid, senha, trocar_senha=True):
        u = self._um("SELECT * FROM usuarios WHERE id=?", (uid,))
        if not u:
            raise ErroAuth("Usuário não encontrado.")
        validar_senha(senha, u["usuario"])
        with self.lock:
            self.banco.conn.execute("UPDATE usuarios SET senha_hash=?, trocar_senha=?, falhas=0, bloqueado_ate=0 WHERE id=?",
                                    (gerar_hash(senha), 1 if trocar_senha else 0, uid))
            self.banco.conn.execute("DELETE FROM sessoes WHERE usuario_id=?", (uid,))
            self.banco.conn.commit()

    def alterar_propria_senha(self, uid, atual, nova, token_atual=None):
        u = self._um("SELECT * FROM usuarios WHERE id=?", (uid,))
        if not u or not conferir_hash(atual or "", u["senha_hash"]):
            raise ErroAuth("A senha atual não confere.")
        validar_senha(nova, u["usuario"])
        if nova == atual:
            raise ErroAuth("A nova senha deve ser diferente da atual.")
        with self.lock:
            self.banco.conn.execute("UPDATE usuarios SET senha_hash=?, trocar_senha=0 WHERE id=?", (gerar_hash(nova), uid))
            # encerra as outras sessões (mantém a atual)
            if token_atual:
                self.banco.conn.execute("DELETE FROM sessoes WHERE usuario_id=? AND token_hash<>?",
                                        (uid, self._h(token_atual)))
            self.banco.conn.commit()

    # ------------------------------------------------------------ login
    @staticmethod
    def _h(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _ip_bloqueado(self, ip):
        agora = time.time()
        with self._ip_lock:
            lista = [t for t in self._ip_falhas.get(ip, []) if agora - t < IP_JANELA_SEG]
            self._ip_falhas[ip] = lista
            return len(lista) >= IP_FALHAS_MAX

    def _ip_falhou(self, ip):
        with self._ip_lock:
            self._ip_falhas.setdefault(ip, []).append(time.time())

    def autenticar(self, usuario, senha, ip=""):
        """Devolve o usuário (dict público) ou levanta ErroAuth. Mensagem única para usuário/senha errados."""
        global _HASH_FALSO
        if self._ip_bloqueado(ip):
            raise ErroAuth("Muitas tentativas deste endereço. Aguarde alguns minutos.")
        usuario = str(usuario or "").strip().lower()
        senha = str(senha or "")
        u = self._um("SELECT * FROM usuarios WHERE usuario=?", (usuario,))
        if not u:
            if _HASH_FALSO is None:
                _HASH_FALSO = gerar_hash("x-senha-falsa-x")
            conferir_hash(senha, _HASH_FALSO)  # gasta o mesmo tempo, para não revelar se o usuário existe
            self._ip_falhou(ip)
            raise ErroAuth("Usuário ou senha incorretos.")
        if u["bloqueado_ate"] > time.time():
            raise ErroAuth("Conta temporariamente bloqueada por tentativas erradas. Tente de novo em alguns minutos.")
        if not conferir_hash(senha, u["senha_hash"]):
            self._ip_falhou(ip)
            falhas = u["falhas"] + 1
            bloqueio = time.time() + BLOQUEIO_SEG if falhas >= FALHAS_MAX else 0
            with self.lock:
                self.banco.conn.execute("UPDATE usuarios SET falhas=?, bloqueado_ate=? WHERE id=?",
                                        (0 if bloqueio else falhas, bloqueio, u["id"]))
                self.banco.conn.commit()
            raise ErroAuth("Usuário ou senha incorretos.")
        if not u["ativo"]:
            raise ErroAuth("Usuário desativado. Procure o administrador.")
        with self.lock:
            self.banco.conn.execute("UPDATE usuarios SET falhas=0, bloqueado_ate=0, ultimo_acesso=? WHERE id=?",
                                    (_agora_iso(), u["id"]))
            self.banco.conn.commit()
        return _publico(self._um("SELECT * FROM usuarios WHERE id=?", (u["id"],)))

    # ------------------------------------------------------------ sessões
    def criar_sessao(self, uid, ip=""):
        token = secrets.token_urlsafe(32)
        agora = time.time()
        with self.lock:
            self.banco.conn.execute("DELETE FROM sessoes WHERE ultimo_uso<? OR criada<?",
                                    (agora - SESSAO_OCIOSA_SEG, agora - SESSAO_MAX_SEG))
            self.banco.conn.execute("INSERT INTO sessoes(token_hash,usuario_id,criada,ultimo_uso,ip) VALUES(?,?,?,?,?)",
                                    (self._h(token), uid, agora, agora, ip))
            self.banco.conn.commit()
        return token

    def validar_sessao(self, token):
        """Devolve o usuário da sessão, ou None (inexistente, expirada ou usuário desativado)."""
        if not token or len(token) > 100:
            return None
        agora = time.time()
        with self.lock:
            s = self._um("SELECT * FROM sessoes WHERE token_hash=?", (self._h(token),))
            if not s:
                return None
            if agora - s["ultimo_uso"] > SESSAO_OCIOSA_SEG or agora - s["criada"] > SESSAO_MAX_SEG:
                self.banco.conn.execute("DELETE FROM sessoes WHERE token_hash=?", (s["token_hash"],))
                self.banco.conn.commit()
                return None
            u = self._um("SELECT * FROM usuarios WHERE id=?", (s["usuario_id"],))
            if not u or not u["ativo"]:
                return None
            if agora - s["ultimo_uso"] > 3600:  # renova no máximo 1x por hora (evita gravar a cada clique)
                self.banco.conn.execute("UPDATE sessoes SET ultimo_uso=? WHERE token_hash=?", (agora, s["token_hash"]))
                self.banco.conn.commit()
            return _publico(u)

    def encerrar_sessao(self, token):
        if token:
            with self.lock:
                self.banco.conn.execute("DELETE FROM sessoes WHERE token_hash=?", (self._h(token),))
                self.banco.conn.commit()

    # ------------------------------------------------------------ auditoria
    def auditar(self, usuario, acao, detalhe="", ip=""):
        """usuario: dict público (ou None para ações sem login, ex.: falha de login — nesse caso, o nome tentado vai no detalhe)."""
        with self.lock:
            self.banco.conn.execute(
                "INSERT INTO auditoria(quando,usuario_id,usuario,acao,detalhe,ip) VALUES(?,?,?,?,?,?)",
                (_agora_iso(), usuario["id"] if usuario else None, usuario["usuario"] if usuario else "",
                 acao, str(detalhe)[:500], ip))
            self.banco.conn.commit()

    def listar_auditoria(self, limite=200, usuario=None, antes_id=None):
        sql, args = "SELECT * FROM auditoria", []
        cond = []
        if usuario:
            cond.append("usuario=?")
            args.append(usuario)
        if antes_id:
            cond.append("id<?")
            args.append(int(antes_id))
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limite), 1000)))
        with self.lock:
            return [dict(r) for r in self.banco.conn.execute(sql, args).fetchall()]
