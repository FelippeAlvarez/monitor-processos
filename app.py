"""Monitor de Processos (DataJud/CNJ + DJEN) — servidor + agendador, com usuários (v0.4).

Uso:  python app.py
Abre o painel em http://127.0.0.1:8765. Todo acesso exige login (perfis: admin e usuário).
"""
import calendar
import json
import logging
import logging.handlers
import os
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import auth
import backup
import datajud
import dje
import mailer
from config import Config
from db import Banco
from monitor import Monitor

BASE_DIR = Path(__file__).resolve().parent


def resolver_pasta_dados():
    """Onde ficam o banco, a configuração e o log. Prioridade: variável MONITOR_DADOS > arquivo local.json
    ({"pasta_dados": "C:\\Users\\...\\Dropbox\\Monitor"}) > pasta 'dados' ao lado do programa."""
    env = os.environ.get("MONITOR_DADOS")
    if env:
        return Path(env).expanduser()
    try:
        cfg = json.loads((BASE_DIR / "local.json").read_text(encoding="utf-8"))
        if str(cfg.get("pasta_dados") or "").strip():
            return Path(str(cfg["pasta_dados"]).strip()).expanduser()
    except (OSError, ValueError):
        pass
    return BASE_DIR / "dados"


DADOS = resolver_pasta_dados()
DOCS = {"manual": "MANUAL.md", "termos": "TERMOS_DE_USO.md", "privacidade": "PRIVACIDADE.md"}
HOSTS_LOCAIS = ("127.0.0.1", "localhost")
COOKIE = "sessao"

CTX = SimpleNamespace(banco=None, config=None, monitor=None, auth=None, sobre={}, rede=False, proxy=False,
                      codigo_setup="", trava=None)
LOCAIS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def ip_local():
    """Endereço do computador na rede local (não envia nenhum pacote)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def carregar_sobre():
    try:
        return json.loads((BASE_DIR / "sobre.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------- rotas
def r_index(h):
    h.enviar((BASE_DIR / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")


def r_doc(h, nome):
    arq = DOCS.get(nome)
    if not arq or not (BASE_DIR / arq).exists():
        return h.json({"erro": "documento não encontrado"}, 404)
    h.enviar((BASE_DIR / arq).read_bytes(), "text/plain; charset=utf-8")


def r_sobre(h):
    h.json(CTX.sobre)


def r_status(h):
    m = CTX.monitor
    intervalo = CTX.config.dados["intervalo_horas"]
    proximo = (m.ultimo_ciclo + intervalo * 3600) if (m.ultimo_ciclo and intervalo) else None
    ativos = CTX.banco.ativos()
    pausado = m.pausado_ate if m.pausado_ate > time.time() else None
    h.json({"rodando": m.rodando, "progresso": m.progresso, "resumo": m.resumo, "fila": len(m.fila),
            "pendentes": sum(1 for p in ativos if p["status"] in ("pendente", "erro")),
            "pub_novas": sum(p["pub_novas"] for p in CTX.banco.listar()),
            "oab_novas": sum(o["pub_novas"] for o in CTX.banco.oabs()),
            "oab_resumo": m.resumo_oab,
            "pausado_ate": pausado,
            "dje_pausado_ate": m.dje_pausado_ate if m.dje_pausado_ate > time.time() else None,
            "dje_ativo": CTX.config.dados.get("dje_ativo", True),
            "ultimo_ciclo": m.ultimo_ciclo, "proximo_ciclo": proximo,
            "ultima_consulta": CTX.banco.ultima_consulta_geral(), "intervalo_horas": intervalo})


def r_publicacoes(h):
    q = parse_qs(urlparse(h.path).query)
    pid = int(q["processo"][0]) if q.get("processo") else None
    h.json(CTX.banco.publicacoes(pid, so_novas=q.get("novas", ["0"])[0] == "1"))


def r_pub_visto(h):
    corpo = h.corpo()
    pid = corpo.get("processo_id")
    CTX.banco.marcar_pub_vistas(int(pid) if pid else None)
    h.json({"ok": True})


UFS = {"AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA", "PB", "PE", "PI", "PR",
       "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO"}


def r_oabs(h):
    h.json(CTX.banco.oabs())


def r_oab_add(h):
    c = h.corpo()
    tipo = str(c.get("tipo") or "oab").strip().lower()
    bruto = str(c.get("numero") or "").strip()
    rotulo = str(c.get("rotulo") or "").strip()[:80]
    uf = ""
    if tipo == "oab":
        termo = re.sub(r"[.\s]", "", bruto).upper()
        uf = str(c.get("uf") or "").strip().upper()
        if not re.fullmatch(r"\d{1,7}[A-Z]?", termo):
            return h.json({"erro": "Número da OAB inválido (use só dígitos, com letra final opcional, ex.: 123456)."}, 400)
        if uf not in UFS:
            return h.json({"erro": "UF inválida."}, 400)
    elif tipo == "doc":
        termo = dje.so_digitos(bruto)
        if not (dje.cpf_valido(termo) or dje.cnpj_valido(termo)):
            return h.json({"erro": "CPF/CNPJ inválido (dígitos verificadores não conferem)."}, 400)
    elif tipo == "texto":
        termo = " ".join(bruto.split())
        if not 6 <= len(termo) <= 80:
            return h.json({"erro": "O texto deve ter entre 6 e 80 caracteres."}, 400)
    else:
        return h.json({"erro": "Tipo de filtro inválido."}, 400)
    oid = CTX.banco.adicionar_oab(termo, uf, rotulo, tipo)
    if oid is None:
        return h.json({"erro": "Esse filtro já está cadastrado."}, 409)
    CTX.monitor.solicitar_oab({oid}, forcar=True)
    h.auditar("filtro_criado", dje.descrever_filtro(tipo, termo, uf))
    h.json({"ok": True, "id": oid})


def r_oab_del(h, oid):
    o = CTX.banco.obter_oab(int(oid))
    CTX.banco.remover_oab(int(oid))
    if o:
        h.auditar("filtro_removido", dje.descrever_filtro(o.get("tipo", "oab"), o["numero"], o["uf"]))
    h.json({"ok": True})


def r_oab_consultar(h, oid):
    h.json({"ok": True, "enfileiradas": CTX.monitor.solicitar_oab({int(oid)}, forcar=True)})


def r_oab_pubs(h):
    q = parse_qs(urlparse(h.path).query)
    oid = int(q["oab"][0]) if q.get("oab") else None
    h.json(CTX.banco.oab_publicacoes(oid, so_novas=q.get("novas", ["0"])[0] == "1"))


def r_oab_visto(h):
    oid = h.corpo().get("oab_id")
    CTX.banco.marcar_oab_vistas(int(oid) if oid else None)
    h.json({"ok": True})


def r_log(h):
    """Últimas linhas do registro de diagnóstico (não contém chave da API nem senha)."""
    arq = DADOS / "monitor.log"
    texto = ""
    if arq.exists():
        texto = "\n".join(arq.read_text(encoding="utf-8", errors="replace").splitlines()[-300:])
    h.enviar(texto.encode("utf-8"), "text/plain; charset=utf-8")


def r_listar(h):
    h.json(CTX.banco.listar())


def _disparar(ids=None):
    """Põe na fila (nunca descarta o pedido, mesmo com consulta em andamento)."""
    return CTX.monitor.solicitar(ids, forcar=True)


def r_adicionar(h):
    corpo = h.corpo()
    texto = str(corpo.get("numeros") or "")
    apelido = str(corpo.get("apelido") or "").strip()[:120]
    cliente = str(corpo.get("cliente") or "").strip()[:120]
    pasta = str(corpo.get("pasta") or "").strip()[:60]
    numeros = datajud.RE_CNJ_BUSCA.findall(texto)
    if not numeros:
        return h.json({"erro": "Nenhum número CNJ encontrado no texto."}, 400)
    if len(numeros) > 1:
        apelido = ""  # apelido só faz sentido para um processo
    res = {"adicionados": [], "repetidos": [], "invalidos": []}
    novos_ids = []
    for n in numeros:
        try:
            info = datajud.interpretar_numero(n)
            alias = datajud.alias_tribunal(info["j"], info["tr"])
        except (datajud.ErroNumero, datajud.TribunalNaoSuportado) as e:
            res["invalidos"].append({"numero": n, "motivo": str(e)})
            continue
        pid = CTX.banco.adicionar(info["numero"], info["limpo"], alias, apelido, cliente, pasta)
        if pid is None:
            res["repetidos"].append(info["numero"])
        else:
            res["adicionados"].append(info["numero"])
            novos_ids.append(pid)
    if novos_ids:
        _disparar(set(novos_ids))
    if res["adicionados"]:
        h.auditar("processo_adicionado", f"{len(res['adicionados'])} processo(s): " + ", ".join(res["adicionados"][:5])
                  + ("…" if len(res["adicionados"]) > 5 else ""))
    h.json(res)


def r_remover(h, pid):
    p = CTX.banco.obter(int(pid))
    CTX.banco.remover(int(pid))
    if p:
        h.auditar("processo_removido", p["numero"])
    h.json({"ok": True})


def r_movimentos(h, pid):
    h.json(CTX.banco.movimentos(int(pid)))


def r_visto(h, pid):
    CTX.banco.marcar_visto(int(pid))
    h.json({"ok": True})


def r_visto_todos(h):
    CTX.banco.marcar_visto(None)
    h.json({"ok": True})


def r_consultar_todos(h):
    CTX.monitor.solicitar_oab(None, forcar=True)
    h.json({"ok": True, "enfileirados": _disparar()})


def r_consultar_um(h, pid):
    h.json({"ok": True, "enfileirados": _disparar({int(pid)})})


def r_config_get(h):
    h.json(CTX.config.publico())


def r_config_set(h):
    try:
        h._corpo_cache = h.corpo()
        CTX.config.atualizar(h._corpo_cache)
    except (ValueError, TypeError) as e:
        return h.json({"erro": str(e)}, 400)
    h.auditar("config_alterada", "campos: " + ", ".join(sorted(k for k in h._corpo_cache if k != "email"))
              + (" e-mail" if "email" in h._corpo_cache else ""))
    h.json(CTX.config.publico())


def r_email_teste(h):
    try:
        mailer.enviar(CTX.config.dados["email"], "[Monitor de Processos] Teste de e-mail",
                      "Se você recebeu esta mensagem, o envio de alertas está funcionando.")
    except Exception as e:  # noqa: BLE001
        return h.json({"erro": f"Falha no envio: {e}"}, 400)
    h.json({"ok": True})


# ---------------------------------------------------------------- sessão, usuários, auditoria, backup
def _usuario_json(u):
    return {"id": u["id"], "usuario": u["usuario"], "nome": u["nome"], "email": u["email"], "perfil": u["perfil"],
            "trocar_senha": bool(u["trocar_senha"])}


def r_sessao(h):
    """Público: diz se há alguém logado e se o sistema ainda precisa do primeiro administrador."""
    u = h.usuario
    h.json({"usuario": _usuario_json(u) if u else None, "precisa_setup": not CTX.auth.tem_usuarios(),
            "versao": CTX.sobre.get("versao", "")})


def r_setup(h):
    """Cria o primeiro administrador. Exige o código mostrado na janela do programa (ou no log do servidor)."""
    if CTX.auth.tem_usuarios():
        return h.json({"erro": "O sistema já foi configurado."}, 409)
    c = h.corpo()
    if not CTX.codigo_setup or not secrets.compare_digest(str(c.get("codigo") or ""), CTX.codigo_setup):
        return h.json({"erro": "Código de configuração incorreto."}, 403)
    try:
        uid = CTX.auth.criar(c.get("usuario"), c.get("nome"), c.get("email"), "admin", c.get("senha"), trocar_senha=False)
    except auth.ErroAuth as e:
        return h.json({"erro": str(e)}, 400)
    u = CTX.auth.obter(uid)
    CTX.auth.auditar(u, "primeiro_admin", "administrador inicial criado", h.ip())
    CTX.codigo_setup = ""
    h.abrir_sessao(u)
    h.json({"ok": True, "usuario": _usuario_json(u)})


def r_login(h):
    c = h.corpo()
    try:
        u = CTX.auth.autenticar(c.get("usuario"), c.get("senha"), h.ip())
    except auth.ErroAuth as e:
        CTX.auth.auditar(None, "login_falhou", f"usuário informado: {str(c.get('usuario') or '')[:40]}", h.ip())
        return h.json({"erro": str(e)}, 401)
    CTX.auth.auditar(u, "login", "", h.ip())
    h.abrir_sessao(u)
    h.json({"ok": True, "usuario": _usuario_json(u)})


def r_logout(h):
    CTX.auth.auditar(h.usuario, "logout", "", h.ip())
    CTX.auth.encerrar_sessao(h.token())
    h.cookies_extra.append(f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict" + h.sufixo_seguro())
    h.json({"ok": True})


def r_senha_propria(h):
    c = h.corpo()
    try:
        CTX.auth.alterar_propria_senha(h.usuario["id"], c.get("atual"), c.get("nova"), h.token())
    except auth.ErroAuth as e:
        return h.json({"erro": str(e)}, 400)
    h.auditar("senha_alterada", "pelo próprio usuário")
    h.json({"ok": True})


def r_usuarios(h):
    h.json(CTX.auth.listar())


def r_usuario_add(h):
    c = h.corpo()
    try:
        uid = CTX.auth.criar(c.get("usuario"), c.get("nome"), c.get("email"), str(c.get("perfil") or "usuario"),
                             c.get("senha"), trocar_senha=True)
    except auth.ErroAuth as e:
        return h.json({"erro": str(e)}, 400)
    u = CTX.auth.obter(uid)
    h.auditar("usuario_criado", f"{u['usuario']} ({u['perfil']})")
    h.json({"ok": True, "id": uid})


def r_usuario_edit(h, uid):
    c = h.corpo()
    uid = int(uid)
    antes = CTX.auth.obter(uid)
    if not antes:
        return h.json({"erro": "Usuário não encontrado."}, 404)
    if uid == h.usuario["id"] and (c.get("ativo") is False or (c.get("perfil") not in (None, h.usuario["perfil"]))):
        return h.json({"erro": "Você não pode desativar nem rebaixar a própria conta; peça a outro administrador."}, 400)
    try:
        CTX.auth.atualizar(uid, c.get("nome"), c.get("email"), c.get("perfil"), c.get("ativo"))
    except auth.ErroAuth as e:
        return h.json({"erro": str(e)}, 400)
    depois = CTX.auth.obter(uid)
    mud = [k for k in ("nome", "email", "perfil", "ativo") if antes[k] != depois[k]]
    h.auditar("usuario_alterado", f"{antes['usuario']}: " + (", ".join(mud) or "sem mudanças"))
    h.json({"ok": True})


def r_usuario_senha(h, uid):
    c = h.corpo()
    u = CTX.auth.obter(int(uid))
    if not u:
        return h.json({"erro": "Usuário não encontrado."}, 404)
    try:
        CTX.auth.redefinir_senha(int(uid), c.get("senha"), trocar_senha=True)
    except auth.ErroAuth as e:
        return h.json({"erro": str(e)}, 400)
    h.auditar("senha_redefinida", f"{u['usuario']} (deve trocar no próximo acesso)")
    h.json({"ok": True})


def r_auditoria(h):
    q = parse_qs(urlparse(h.path).query)
    h.json(CTX.auth.listar_auditoria(int((q.get("limite") or ["200"])[0]), (q.get("usuario") or [None])[0],
                                      (q.get("antes") or [None])[0]))


def _pasta_backup():
    return backup.pasta_backup(CTX.config.dados, DADOS)


def r_backup_get(h):
    d = CTX.config.dados
    h.json({"pasta": str(_pasta_backup()), "ativo": d.get("backup_ativo", True), "hora": d.get("backup_hora", "02:00"),
            "manter": d.get("backup_manter", 30), "pasta_dados": str(DADOS), "pasta_configurada": d.get("pasta_backup", ""),
            "backups": backup.listar_backups(_pasta_backup())[:30]})


def r_backup_agora(h):
    try:
        arq = backup.fazer_backup(CTX.banco, _pasta_backup(), CTX.config.dados.get("backup_manter", 30))
    except OSError as e:
        return h.json({"erro": f"Não foi possível gravar o backup: {e}"}, 400)
    h.auditar("backup_manual", arq.name)
    h.json({"ok": True, "arquivo": arq.name})


# nível: "pub" = sem login; "usu" = qualquer usuário logado; "adm" = só administrador
ROTAS = [
    ("GET", r"/", r_index, "pub"),
    ("GET", r"/doc/(\w+)", r_doc, "pub"),
    ("GET", r"/api/sobre", r_sobre, "pub"),
    ("GET", r"/api/sessao", r_sessao, "pub"),
    ("POST", r"/api/setup", r_setup, "pub"),
    ("POST", r"/api/login", r_login, "pub"),
    ("POST", r"/api/logout", r_logout, "usu"),
    ("POST", r"/api/senha", r_senha_propria, "usu"),
    ("GET", r"/api/status", r_status, "usu"),
    ("GET", r"/api/publicacoes", r_publicacoes, "usu"),
    ("POST", r"/api/publicacoes/visto", r_pub_visto, "usu"),
    ("GET", r"/api/oabs", r_oabs, "usu"),
    ("POST", r"/api/oabs", r_oab_add, "adm"),
    ("DELETE", r"/api/oabs/(\d+)", r_oab_del, "adm"),
    ("POST", r"/api/oabs/(\d+)/consultar", r_oab_consultar, "usu"),
    ("GET", r"/api/oab-publicacoes", r_oab_pubs, "usu"),
    ("POST", r"/api/oab-publicacoes/visto", r_oab_visto, "usu"),
    ("GET", r"/api/processos", r_listar, "usu"),
    ("POST", r"/api/processos", r_adicionar, "usu"),
    ("DELETE", r"/api/processos/(\d+)", r_remover, "usu"),
    ("GET", r"/api/processos/(\d+)/movimentos", r_movimentos, "usu"),
    ("POST", r"/api/processos/(\d+)/visto", r_visto, "usu"),
    ("POST", r"/api/processos/(\d+)/consultar", r_consultar_um, "usu"),
    ("POST", r"/api/visto-todos", r_visto_todos, "usu"),
    ("POST", r"/api/consultar", r_consultar_todos, "usu"),
    ("GET", r"/api/log", r_log, "adm"),
    ("GET", r"/api/config", r_config_get, "adm"),
    ("POST", r"/api/config", r_config_set, "adm"),
    ("POST", r"/api/email/teste", r_email_teste, "adm"),
    ("GET", r"/api/usuarios", r_usuarios, "adm"),
    ("POST", r"/api/usuarios", r_usuario_add, "adm"),
    ("POST", r"/api/usuarios/(\d+)", r_usuario_edit, "adm"),
    ("POST", r"/api/usuarios/(\d+)/senha", r_usuario_senha, "adm"),
    ("GET", r"/api/auditoria", r_auditoria, "adm"),
    ("GET", r"/api/backup", r_backup_get, "adm"),
    ("POST", r"/api/backup", r_backup_agora, "adm"),
]
# quem precisa trocar a senha só pode usar estas rotas
ROTAS_TROCA_SENHA = (r_sessao, r_senha_propria, r_logout, r_index, r_doc, r_sobre)


class Handler(BaseHTTPRequestHandler):
    server_version = "MonitorProcessos"
    usuario = None
    _corpo_cache = {}

    def log_message(self, *args):
        pass

    # ---- utilidades
    def ip(self):
        if CTX.proxy:  # atrás de um proxy confiável (Caddy/nginx): usa o último endereço do X-Forwarded-For
            xff = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            if xff:
                return xff[:45]
        return self.client_address[0]

    def https(self):
        return CTX.proxy and (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def sufixo_seguro(self):
        return "; Secure" if self.https() else ""

    def token(self):
        for parte in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = parte.strip().partition("=")
            if k == COOKIE:
                return v
        return ""

    def auditar(self, acao, detalhe=""):
        CTX.auth.auditar(self.usuario, acao, detalhe, self.ip())

    def abrir_sessao(self, u):
        t = CTX.auth.criar_sessao(u["id"], self.ip())
        self.cookies_extra.append(f"{COOKIE}={t}; Max-Age={auth.SESSAO_MAX_SEG}; Path=/; HttpOnly; SameSite=Strict"
                                  + self.sufixo_seguro())

    # ---- respostas
    def enviar(self, dados, tipo, status=200):
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(dados)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for c in getattr(self, "cookies_extra", []):
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(dados)

    def json(self, obj, status=200):
        self.enviar(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def corpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise ValueError("Corpo muito grande.")
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # ---- segurança
    def origem_ok(self):
        """Contra ataques vindos de outros sites. Sem modo rede: só aceita Host local (evita DNS rebinding).
        Com modo rede/servidor: qualquer Host, mas a origem das requisições deve ser a própria página."""
        host_completo = self.headers.get("Host") or ""
        host = host_completo.rsplit(":", 1)[0] if not host_completo.startswith("[") else host_completo
        origem = self.headers.get("Origin")
        if CTX.rede:
            return not origem or urlparse(origem).netloc == host_completo
        if host not in HOSTS_LOCAIS:
            return False
        if origem and urlparse(origem).hostname not in HOSTS_LOCAIS:
            return False
        return True

    def despachar(self, metodo):
        self.cookies_extra = []
        self.usuario = None
        if not self.origem_ok():
            return self.json({"erro": "Origem não permitida."}, 403)
        if metodo == "POST" and "application/json" not in (self.headers.get("Content-Type") or ""):
            return self.json({"erro": "Content-Type deve ser application/json."}, 415)
        caminho = urlparse(self.path).path
        try:
            rota = None
            for m, padrao, fn, nivel in ROTAS:
                if m == metodo:
                    r = re.fullmatch(padrao, caminho)
                    if r:
                        rota = (fn, r.groups(), nivel)
                        break
            if not rota:
                return self.json({"erro": "Rota não encontrada."}, 404)
            fn, args, nivel = rota
            self.usuario = CTX.auth.validar_sessao(self.token())
            if nivel != "pub" and not self.usuario:
                return self.json({"erro": "Faça login para continuar.", "login": True}, 401)
            if self.usuario and self.usuario["trocar_senha"] and fn not in ROTAS_TROCA_SENHA:
                return self.json({"erro": "Defina uma nova senha antes de continuar.", "trocar_senha": True}, 403)
            if nivel == "adm" and self.usuario["perfil"] != "admin":
                return self.json({"erro": "Ação restrita ao administrador."}, 403)
            return fn(self, *args)
        except (ValueError, json.JSONDecodeError) as e:
            self.json({"erro": f"Requisição inválida: {e}"}, 400)
        except Exception as e:  # noqa: BLE001
            print("Erro interno:", repr(e), file=sys.stderr)
            logging.getLogger("monitor").exception("Erro interno: %r", e)
            self.json({"erro": "Erro interno."}, 500)

    def do_GET(self):
        self.despachar("GET")

    def do_POST(self):
        self.despachar("POST")

    def do_DELETE(self):
        self.despachar("DELETE")


def agendador():
    """Dispara um ciclo quando passar o intervalo configurado desde o último; faz o backup diário e o batimento da trava."""
    time.sleep(20)
    voltas = 0
    while True:
        try:
            m = CTX.monitor
            horas = CTX.config.dados["intervalo_horas"]
            if horas and not m.rodando:
                if m.ultimo_ciclo is None:
                    ultima = CTX.banco.ultima_consulta_geral()
                    devido = True
                    if ultima:
                        t = calendar.timegm(time.strptime(ultima, "%Y-%m-%dT%H:%M:%SZ"))  # UTC -> epoch
                        devido = (time.time() - t) >= horas * 3600
                else:
                    devido = (time.time() - m.ultimo_ciclo) >= horas * 3600
                if devido and (CTX.banco.ativos() or CTX.banco.oabs_ativas()):
                    m.solicitar()
                    m.solicitar_oab()
            m.agendar_pendentes()  # reenvia à fila o que ficou pendente/com erro (com espera progressiva)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("monitor").exception("Erro no agendador: %r", e)
        try:
            verificar_backup()
        except Exception as e:  # noqa: BLE001
            logging.getLogger("monitor").exception("Falha no backup automático: %r", e)
        if CTX.trava:
            CTX.trava.batimento()
        voltas += 1
        time.sleep(30)


_ultimo_backup_tentado = {"dia": None}


def verificar_backup():
    d = CTX.config.dados
    if not d.get("backup_ativo", True):
        return
    pasta = _pasta_backup()
    hoje = time.strftime("%Y%m%d")
    if _ultimo_backup_tentado["dia"] == hoje:
        return
    if backup.backup_devido(pasta, d.get("backup_hora", "02:00")):
        _ultimo_backup_tentado["dia"] = hoje  # uma tentativa por dia (se falhar, o erro fica no log)
        arq = backup.fazer_backup(CTX.banco, pasta, d.get("backup_manter", 30))
        CTX.auth.auditar(None, "backup_automatico", arq.name)
        logging.getLogger("monitor").info("Backup automático gravado: %s", arq)


def iniciar():
    try:
        DADOS.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"Não foi possível usar a pasta de dados '{DADOS}': {e}")
        sys.exit(1)
    CTX.trava = backup.TravaInstancia(DADOS)
    motivo = CTX.trava.adquirir(ignorar=bool(os.environ.get("MONITOR_IGNORAR_TRAVA")))
    if motivo:
        print(motivo)
        sys.exit(1)
    manipulador = logging.handlers.RotatingFileHandler(DADOS / "monitor.log", maxBytes=1_000_000,
                                                       backupCount=2, encoding="utf-8")
    manipulador.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("monitor").addHandler(manipulador)
    logging.getLogger("monitor").setLevel(logging.INFO)
    CTX.config = Config(DADOS / "config.json")
    if not (DADOS / "config.json").exists():
        CTX.config.salvar()
    if "codigo_acesso" in CTX.config.dados:  # código de acesso da v0.3.3: substituído pelo login de usuário
        CTX.config.dados.pop("codigo_acesso")
        CTX.config.salvar()
    CTX.banco = Banco(DADOS / "processos.db")
    CTX.auth = auth.Auth(CTX.banco)
    CTX.monitor = Monitor(CTX.banco, CTX.config)
    CTX.sobre = carregar_sobre()
    porta = int(os.environ.get("MONITOR_PORTA") or CTX.config.dados["porta"])
    CTX.rede = bool(os.environ.get("MONITOR_REDE"))
    CTX.proxy = bool(os.environ.get("MONITOR_PROXY"))
    endereco = os.environ.get("MONITOR_ENDERECO") or ("0.0.0.0" if CTX.rede else "127.0.0.1")
    if not CTX.auth.tem_usuarios():
        CTX.codigo_setup = secrets.token_urlsafe(8)
        logging.getLogger("monitor").info("Sistema sem usuários: aguardando criação do administrador.")
    try:
        servidor = ThreadingHTTPServer((endereco, porta), Handler)
    except OSError:
        CTX.trava.liberar()
        print(f"A porta {porta} já está em uso. Feche outra instância do Monitor e tente de novo.")
        sys.exit(1)
    threading.Thread(target=agendador, daemon=True).start()
    url = f"http://127.0.0.1:{porta}"
    print(f"Monitor de Processos {CTX.sobre.get('versao', '')} rodando em {url}")
    print(f"Pasta de dados: {DADOS}")
    if CTX.rede:
        print()
        print("MODO REDE ATIVO: outros aparelhos podem acessar, com login e senha de usuário.")
        print(f"   Na mesma rede (Wi-Fi/cabo): http://{ip_local()}:{porta}")
        print("Se o Windows perguntar sobre o Firewall, permita em 'Redes privadas'.")
        print("Para acesso pela internet, use HTTPS (veja o Manual, seção 18).")
    if CTX.codigo_setup:
        print()
        print("PRIMEIRO ACESSO: abra o endereço acima e crie o administrador.")
        print(f"   Código de configuração (use uma vez): {CTX.codigo_setup}")
    print()
    print("Para encerrar, feche esta janela ou pressione Ctrl+C.")
    if not os.environ.get("MONITOR_NAO_ABRIR"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        CTX.trava.liberar()


if __name__ == "__main__":
    iniciar()
