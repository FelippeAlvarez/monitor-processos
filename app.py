"""Monitor de Processos (DataJud/CNJ) — servidor local + agendador.

Uso:  python app.py
Abre o painel em http://127.0.0.1:8765 (somente nesta máquina).
"""
import calendar
import json
import logging
import logging.handlers
import os
import hmac
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

import datajud
import dje
import mailer
from config import Config
from db import Banco
from monitor import Monitor

BASE_DIR = Path(__file__).resolve().parent
DADOS = Path(os.environ.get("MONITOR_DADOS") or BASE_DIR / "dados")
DOCS = {"manual": "MANUAL.md", "termos": "TERMOS_DE_USO.md", "privacidade": "PRIVACIDADE.md"}
HOSTS_LOCAIS = ("127.0.0.1", "localhost")

CTX = SimpleNamespace(banco=None, config=None, monitor=None, sobre={}, rede=False, codigo="")
LOCAIS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
PAGINA_RESTRITA = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                   "<body style='font:16px system-ui;padding:24px'><h3>Acesso restrito</h3>"
                   "<p>Abra o endereço completo com <b>?codigo=...</b> mostrado na janela do programa (no computador). "
                   "Isso só é necessário uma vez neste aparelho.</p></body>")


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
    h.json({"ok": True, "id": oid})


def r_oab_del(h, oid):
    CTX.banco.remover_oab(int(oid))
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
    h.json(res)


def r_remover(h, pid):
    CTX.banco.remover(int(pid))
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
        CTX.config.atualizar(h.corpo())
    except (ValueError, TypeError) as e:
        return h.json({"erro": str(e)}, 400)
    h.json(CTX.config.publico())


def r_email_teste(h):
    try:
        mailer.enviar(CTX.config.dados["email"], "[Monitor de Processos] Teste de e-mail",
                      "Se você recebeu esta mensagem, o envio de alertas está funcionando.")
    except Exception as e:  # noqa: BLE001
        return h.json({"erro": f"Falha no envio: {e}"}, 400)
    h.json({"ok": True})


ROTAS = [
    ("GET", r"/", r_index),
    ("GET", r"/doc/(\w+)", r_doc),
    ("GET", r"/api/sobre", r_sobre),
    ("GET", r"/api/status", r_status),
    ("GET", r"/api/log", r_log),
    ("GET", r"/api/publicacoes", r_publicacoes),
    ("POST", r"/api/publicacoes/visto", r_pub_visto),
    ("GET", r"/api/oabs", r_oabs),
    ("POST", r"/api/oabs", r_oab_add),
    ("DELETE", r"/api/oabs/(\d+)", r_oab_del),
    ("POST", r"/api/oabs/(\d+)/consultar", r_oab_consultar),
    ("GET", r"/api/oab-publicacoes", r_oab_pubs),
    ("POST", r"/api/oab-publicacoes/visto", r_oab_visto),
    ("GET", r"/api/processos", r_listar),
    ("POST", r"/api/processos", r_adicionar),
    ("DELETE", r"/api/processos/(\d+)", r_remover),
    ("GET", r"/api/processos/(\d+)/movimentos", r_movimentos),
    ("POST", r"/api/processos/(\d+)/visto", r_visto),
    ("POST", r"/api/processos/(\d+)/consultar", r_consultar_um),
    ("POST", r"/api/visto-todos", r_visto_todos),
    ("POST", r"/api/consultar", r_consultar_todos),
    ("GET", r"/api/config", r_config_get),
    ("POST", r"/api/config", r_config_set),
    ("POST", r"/api/email/teste", r_email_teste),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "MonitorProcessos"

    def log_message(self, *args):
        pass

    # respostas
    def enviar(self, dados, tipo, status=200):
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(dados)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
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

    # segurança: só aceita acesso local (Host/Origin) e JSON nos POST
    def origem_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0] if not (self.headers.get("Host") or "").startswith("[") \
            else (self.headers.get("Host") or "")
        origem = self.headers.get("Origin")
        if CTX.rede:  # modo rede: qualquer Host, mas a origem das requisições deve ser a própria página
            return not origem or urlparse(origem).netloc == (self.headers.get("Host") or "")
        if host not in HOSTS_LOCAIS:
            return False
        if origem and urlparse(origem).hostname not in HOSTS_LOCAIS:
            return False
        return True

    def cookie_codigo(self):
        for parte in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = parte.strip().partition("=")
            if k == "acesso":
                return v
        return ""

    def autorizado(self):
        """Fora do modo rede, ou vindo do próprio computador, não há código. Na rede, exige o código de acesso."""
        if not CTX.rede or self.client_address[0] in LOCAIS:
            return True
        return bool(CTX.codigo) and hmac.compare_digest(self.cookie_codigo(), CTX.codigo)

    def despachar(self, metodo):
        if not self.origem_ok():
            return self.json({"erro": "Origem não permitida."}, 403)
        if not self.autorizado():
            q = parse_qs(urlparse(self.path).query)
            informado = (q.get("codigo") or [""])[0]
            if metodo == "GET" and CTX.codigo and hmac.compare_digest(informado, CTX.codigo):
                self.send_response(302)  # código correto: grava o cookie neste aparelho e vai para o painel
                self.send_header("Set-Cookie", f"acesso={CTX.codigo}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict")
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if urlparse(self.path).path == "/":
                return self.enviar(PAGINA_RESTRITA.encode("utf-8"), "text/html; charset=utf-8", 401)
            return self.json({"erro": "Acesso restrito."}, 401)
        if metodo == "POST" and "application/json" not in (self.headers.get("Content-Type") or ""):
            return self.json({"erro": "Content-Type deve ser application/json."}, 415)
        caminho = urlparse(self.path).path
        try:
            for m, padrao, fn in ROTAS:
                if m == metodo:
                    r = re.fullmatch(padrao, caminho)
                    if r:
                        return fn(self, *r.groups())
            self.json({"erro": "Rota não encontrada."}, 404)
        except (ValueError, json.JSONDecodeError) as e:
            self.json({"erro": f"Requisição inválida: {e}"}, 400)
        except Exception as e:  # noqa: BLE001
            print("Erro interno:", repr(e), file=sys.stderr)
            self.json({"erro": "Erro interno."}, 500)

    def do_GET(self):
        self.despachar("GET")

    def do_POST(self):
        self.despachar("POST")

    def do_DELETE(self):
        self.despachar("DELETE")


def agendador():
    """Dispara um ciclo quando passar o intervalo configurado desde o último."""
    time.sleep(20)
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
        time.sleep(30)


def iniciar():
    DADOS.mkdir(parents=True, exist_ok=True)
    manipulador = logging.handlers.RotatingFileHandler(DADOS / "monitor.log", maxBytes=1_000_000,
                                                       backupCount=2, encoding="utf-8")
    manipulador.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("monitor").addHandler(manipulador)
    logging.getLogger("monitor").setLevel(logging.INFO)
    CTX.config = Config(DADOS / "config.json")
    if not (DADOS / "config.json").exists():
        CTX.config.salvar()
    CTX.banco = Banco(DADOS / "processos.db")
    CTX.monitor = Monitor(CTX.banco, CTX.config)
    CTX.sobre = carregar_sobre()
    porta = int(os.environ.get("MONITOR_PORTA") or CTX.config.dados["porta"])
    CTX.rede = bool(os.environ.get("MONITOR_REDE"))
    if CTX.rede:
        if not CTX.config.dados.get("codigo_acesso"):
            CTX.config.dados["codigo_acesso"] = secrets.token_urlsafe(9)
            CTX.config.salvar()
        CTX.codigo = CTX.config.dados["codigo_acesso"]
    try:
        servidor = ThreadingHTTPServer(("0.0.0.0" if CTX.rede else "127.0.0.1", porta), Handler)
    except OSError:
        print(f"A porta {porta} já está em uso. Feche outra instância do Monitor e tente de novo.")
        sys.exit(1)
    threading.Thread(target=agendador, daemon=True).start()
    url = f"http://127.0.0.1:{porta}"
    print(f"Monitor de Processos {CTX.sobre.get('versao', '')} rodando em {url}")
    if CTX.rede:
        print()
        print("MODO REDE ATIVO. No celular (mesmo Wi-Fi do computador), abra uma vez este endereço:")
        print(f"   http://{ip_local()}:{porta}/?codigo={CTX.codigo}")
        print("Depois o celular fica autorizado. Não compartilhe o endereço com o código.")
        print("Se o Windows perguntar sobre o Firewall, permita em 'Redes privadas'.")
        print()
    print("Para encerrar, feche esta janela ou pressione Ctrl+C.")
    if not os.environ.get("MONITOR_NAO_ABRIR"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    iniciar()
