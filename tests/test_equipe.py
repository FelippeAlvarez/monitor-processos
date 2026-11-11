"""Testes da v0.4: usuários, login, perfis, auditoria, backup e trava de instância."""
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import auth  # noqa: E402
import backup  # noqa: E402
from config import Config  # noqa: E402
from db import Banco  # noqa: E402
from monitor import Monitor  # noqa: E402

SENHA = "senha-de-teste-123"


class Cliente:
    """Cliente HTTP mínimo que guarda o cookie de sessão."""

    def __init__(self, porta, host="127.0.0.1"):
        self.porta, self.host, self.cookie = porta, host, ""

    def req(self, metodo, url, corpo=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=10)
        h = {"Host": f"{self.host}:{self.porta}"}
        if corpo is not None:
            h["Content-Type"] = "application/json"
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(headers or {})
        c.request(metodo, url, body=json.dumps(corpo) if corpo is not None else None, headers=h)
        r = c.getresponse()
        bruto = r.read()
        for k, v in r.getheaders():
            if k.lower() == "set-cookie":
                par = v.split(";")[0]
                self.cookie = "" if par.endswith("=") else par
        try:
            dados = json.loads(bruto.decode("utf-8"))
        except ValueError:
            dados = bruto.decode("utf-8", "replace")
        c.close()
        return r.status, dados, dict(r.getheaders())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.dados = base
        app.DADOS = base
        app.CTX.config = Config(base / "config.json")
        app.CTX.config.dados["dje_ativo"] = False
        app.CTX.banco = Banco(base / "p.db")
        app.CTX.auth = auth.Auth(app.CTX.banco)
        app.CTX.monitor = Monitor(app.CTX.banco, app.CTX.config, pausa_entre_consultas=0)
        app.CTX.sobre = {"versao": "teste"}
        app.CTX.rede = False
        app.CTX.proxy = False
        app.CTX.codigo_setup = "COD123"
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.porta = self.srv.server_port

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def novo(self):
        return Cliente(self.porta)

    def admin(self):
        c = self.novo()
        st, d, _ = c.req("POST", "/api/setup", {"codigo": "COD123", "usuario": "chefe", "nome": "Chefe",
                                                "email": "chefe@exemplo.com", "senha": SENHA})
        self.assertEqual(st, 200, d)
        return c

    def comum(self, adm, usuario="maria"):
        st, d, _ = adm.req("POST", "/api/usuarios", {"usuario": usuario, "nome": "Maria", "email": "m@exemplo.com",
                                                     "perfil": "usuario", "senha": "provisoria-123"})
        self.assertEqual(st, 200, d)
        c = self.novo()
        self.assertEqual(c.req("POST", "/api/login", {"usuario": usuario, "senha": "provisoria-123"})[0], 200)
        st, d, _ = c.req("POST", "/api/senha", {"atual": "provisoria-123", "nova": SENHA})
        self.assertEqual(st, 200, d)
        return c


class TesteAcesso(Base):
    def test_sem_login_nada_funciona(self):
        c = self.novo()
        for rota in ("/api/processos", "/api/status", "/api/config", "/api/usuarios", "/api/publicacoes", "/api/log"):
            st, d, _ = c.req("GET", rota)
            self.assertEqual(st, 401, rota)
            self.assertTrue(d["login"])
        self.assertEqual(c.req("POST", "/api/processos", {"numeros": "x"})[0], 401)
        self.assertEqual(c.req("GET", "/")[0], 200)  # a página (com a tela de login) é pública
        st, d, _ = c.req("GET", "/api/sessao")
        self.assertIsNone(d["usuario"])
        self.assertTrue(d["precisa_setup"])

    def test_setup_exige_codigo_e_so_uma_vez(self):
        c = self.novo()
        self.assertEqual(c.req("POST", "/api/setup", {"codigo": "errado", "usuario": "chefe", "nome": "C", "senha": SENHA})[0], 403)
        self.assertFalse(app.CTX.auth.tem_usuarios())
        st, d, hdr = c.req("POST", "/api/setup", {"codigo": "COD123", "usuario": "Chefe", "nome": "Chefe", "senha": SENHA})
        self.assertEqual(st, 200, d)
        self.assertIn("HttpOnly", hdr["Set-Cookie"])
        self.assertIn("SameSite=Strict", hdr["Set-Cookie"])
        self.assertEqual(c.req("GET", "/api/processos")[0], 200)  # já entrou logado
        self.assertEqual(self.novo().req("POST", "/api/setup", {"codigo": "COD123", "usuario": "outro", "nome": "O", "senha": SENHA})[0], 409)

    def test_login_logout_e_senha_errada(self):
        self.admin()
        c = self.novo()
        st, d, _ = c.req("POST", "/api/login", {"usuario": "chefe", "senha": "errada-errada"})
        self.assertEqual(st, 401)
        self.assertEqual(d["erro"], "Usuário ou senha incorretos.")
        st, d, _ = c.req("POST", "/api/login", {"usuario": "naoexiste", "senha": SENHA})
        self.assertEqual(d["erro"], "Usuário ou senha incorretos.")  # mesma mensagem: não revela se o usuário existe
        self.assertEqual(c.req("POST", "/api/login", {"usuario": "CHEFE", "senha": SENHA})[0], 200)  # login sem diferenciar maiúsculas
        self.assertEqual(c.req("GET", "/api/processos")[0], 200)
        c.req("POST", "/api/logout", {})
        self.assertEqual(c.req("GET", "/api/processos")[0], 401)

    def test_bloqueio_apos_cinco_falhas(self):
        self.admin()
        c = self.novo()
        for _ in range(auth.FALHAS_MAX):
            self.assertEqual(c.req("POST", "/api/login", {"usuario": "chefe", "senha": "errada-errada"})[0], 401)
        st, d, _ = c.req("POST", "/api/login", {"usuario": "chefe", "senha": SENHA})  # mesmo com a senha certa
        self.assertEqual(st, 401)
        self.assertIn("bloqueada", d["erro"])

    def test_origem_de_outro_site_e_rejeitada(self):
        c = self.admin()
        st, _, _ = c.req("POST", "/api/visto-todos", {}, headers={"Origin": "https://site-malicioso.example"})
        self.assertEqual(st, 403)
        c2 = Cliente(self.porta, host="exemplo.com")  # Host que não é local (DNS rebinding)
        self.assertEqual(c2.req("GET", "/api/sessao")[0], 403)

    def test_post_exige_json(self):
        c = self.admin()
        conn = http.client.HTTPConnection("127.0.0.1", self.porta)
        conn.request("POST", "/api/visto-todos", body="x=1", headers={"Cookie": c.cookie, "Content-Type": "text/plain",
                                                                    "Host": f"127.0.0.1:{self.porta}"})
        self.assertEqual(conn.getresponse().status, 415)


class TestePerfis(Base):
    def test_usuario_comum_nao_acessa_area_admin(self):
        adm = self.admin()
        maria = self.comum(adm)
        for m, rota, corpo in (("GET", "/api/usuarios", None), ("POST", "/api/usuarios", {}), ("GET", "/api/config", None),
                               ("POST", "/api/config", {"intervalo_horas": 3}), ("GET", "/api/auditoria", None),
                               ("GET", "/api/backup", None), ("POST", "/api/backup", {}), ("GET", "/api/log", None),
                               ("POST", "/api/oabs", {"tipo": "oab", "numero": "123456", "uf": "RJ"}),
                               ("POST", "/api/email/teste", {})):
            st, _, _ = maria.req(m, rota, corpo)
            self.assertEqual(st, 403, rota)
        for rota in ("/api/processos", "/api/status", "/api/oabs", "/api/publicacoes"):
            self.assertEqual(maria.req("GET", rota)[0], 200, rota)
        # e o admin acessa tudo isso
        for rota in ("/api/usuarios", "/api/config", "/api/auditoria", "/api/backup"):
            self.assertEqual(adm.req("GET", rota)[0], 200, rota)

    def test_senha_provisoria_obriga_troca(self):
        adm = self.admin()
        adm.req("POST", "/api/usuarios", {"usuario": "joao", "nome": "João", "perfil": "usuario", "senha": "provisoria-123"})
        j = self.novo()
        j.req("POST", "/api/login", {"usuario": "joao", "senha": "provisoria-123"})
        st, d, _ = j.req("GET", "/api/processos")
        self.assertEqual(st, 403)
        self.assertTrue(d["trocar_senha"])
        self.assertEqual(j.req("GET", "/api/sessao")[0], 200)  # a tela ainda consegue saber quem é
        self.assertEqual(j.req("POST", "/api/senha", {"atual": "errada", "nova": SENHA})[0], 400)
        self.assertEqual(j.req("POST", "/api/senha", {"atual": "provisoria-123", "nova": "provisoria-123"})[0], 400)
        self.assertEqual(j.req("POST", "/api/senha", {"atual": "provisoria-123", "nova": SENHA})[0], 200)
        self.assertEqual(j.req("GET", "/api/processos")[0], 200)

    def test_desativar_derruba_sessao_e_impede_login(self):
        adm = self.admin()
        maria = self.comum(adm)
        uid = [u for u in adm.req("GET", "/api/usuarios")[1] if u["usuario"] == "maria"][0]["id"]
        self.assertEqual(adm.req("POST", f"/api/usuarios/{uid}", {"ativo": False})[0], 200)
        self.assertEqual(maria.req("GET", "/api/processos")[0], 401)
        st, d, _ = self.novo().req("POST", "/api/login", {"usuario": "maria", "senha": SENHA})
        self.assertEqual(st, 401)
        self.assertIn("desativado", d["erro"])

    def test_redefinir_senha_derruba_sessoes_e_obriga_troca(self):
        adm = self.admin()
        maria = self.comum(adm)
        uid = [u for u in adm.req("GET", "/api/usuarios")[1] if u["usuario"] == "maria"][0]["id"]
        self.assertEqual(adm.req("POST", f"/api/usuarios/{uid}/senha", {"senha": "outra-provisoria-9"})[0], 200)
        self.assertEqual(maria.req("GET", "/api/processos")[0], 401)
        m2 = self.novo()
        self.assertEqual(m2.req("POST", "/api/login", {"usuario": "maria", "senha": "outra-provisoria-9"})[0], 200)
        self.assertEqual(m2.req("GET", "/api/processos")[0], 403)

    def test_admin_nao_derruba_a_propria_conta_nem_o_ultimo_admin(self):
        adm = self.admin()
        eu = adm.req("GET", "/api/sessao")[1]["usuario"]["id"]
        self.assertEqual(adm.req("POST", f"/api/usuarios/{eu}", {"ativo": False})[0], 400)
        self.assertEqual(adm.req("POST", f"/api/usuarios/{eu}", {"perfil": "usuario"})[0], 400)
        with self.assertRaises(auth.ErroAuth):  # mesmo pela camada de dados
            app.CTX.auth.atualizar(eu, perfil="usuario")
        # com um segundo admin, aí pode
        adm.req("POST", "/api/usuarios", {"usuario": "socio", "nome": "Sócio", "perfil": "admin", "senha": "provisoria-123"})
        outro = [u for u in adm.req("GET", "/api/usuarios")[1] if u["usuario"] == "socio"][0]["id"]
        self.assertEqual(adm.req("POST", f"/api/usuarios/{outro}", {"perfil": "usuario"})[0], 200)

    def test_cliente_ainda_nao_existe_como_perfil(self):
        adm = self.admin()
        st, _, _ = adm.req("POST", "/api/usuarios", {"usuario": "cli", "nome": "Cliente", "perfil": "cliente", "senha": "provisoria-123"})
        self.assertEqual(st, 400)


class TesteSenhasEAuditoria(Base):
    def test_senha_nunca_fica_em_texto(self):
        self.admin()
        con = sqlite3.connect(str(self.dados / "p.db"))
        h = con.execute("SELECT senha_hash FROM usuarios").fetchone()[0]
        con.close()
        self.assertTrue(h.startswith("scrypt$"))
        self.assertNotIn(SENHA, h)
        self.assertTrue(auth.conferir_hash(SENHA, h))
        self.assertFalse(auth.conferir_hash("outra", h))
        self.assertNotEqual(auth.gerar_hash(SENHA), auth.gerar_hash(SENHA))  # sal individual

    def test_regras_de_senha(self):
        for ruim in ("curta", "aaaaaaaaaaaa", "", None):
            with self.assertRaises(auth.ErroAuth):
                auth.validar_senha(ruim)
        with self.assertRaises(auth.ErroAuth):
            auth.validar_senha("usuario-teste", "Usuario-Teste")
        auth.validar_senha("uma-senha-boa-1")

    def test_validacao_de_cadastro(self):
        a = app.CTX.auth
        for usuario, email in (("a", ""), ("com espaço", ""), ("ok-user", "sem-arroba"), ("Ok.User", "")):
            if usuario == "Ok.User":
                a.criar(usuario, "Fulano", email, "usuario", SENHA)  # é normalizado para minúsculas
                self.assertIsNotNone(a._um("SELECT 1 AS x FROM usuarios WHERE usuario='ok.user'"))
                with self.assertRaises(auth.ErroAuth):  # duplicado
                    a.criar("OK.USER", "Outro", "", "usuario", SENHA)
            else:
                with self.assertRaises(auth.ErroAuth):
                    a.criar(usuario, "Fulano", email, "usuario", SENHA)

    def test_auditoria_registra_acoes(self):
        adm = self.admin()
        self.novo().req("POST", "/api/login", {"usuario": "chefe", "senha": "errada-errada"})
        self.comum(adm)
        adm.req("POST", "/api/config", {"intervalo_horas": 3})
        st, aud, _ = adm.req("GET", "/api/auditoria")
        acoes = [a["acao"] for a in aud]
        for esperado in ("primeiro_admin", "login_falhou", "usuario_criado", "login", "senha_alterada", "config_alterada"):
            self.assertIn(esperado, acoes)
        falha = [a for a in aud if a["acao"] == "login_falhou"][0]
        self.assertIn("chefe", falha["detalhe"])
        self.assertNotIn("errada-errada", json.dumps(aud))  # a senha tentada nunca é registrada
        self.assertNotIn(SENHA, json.dumps(aud))
        cfg_aud = [a for a in aud if a["acao"] == "config_alterada"][0]
        self.assertNotIn("3", cfg_aud["detalhe"].replace("intervalo_horas", ""))  # só os nomes dos campos, não os valores
        self.assertEqual(adm.req("GET", "/api/auditoria?usuario=maria")[1][0]["usuario"], "maria")

    def test_config_publica_nao_vaza_segredos(self):
        adm = self.admin()
        adm.req("POST", "/api/config", {"email": {"senha": "segredo-smtp"}})
        st, cfg, _ = adm.req("GET", "/api/config")
        self.assertNotIn("segredo-smtp", json.dumps(cfg))
        self.assertTrue(cfg["email"]["senha_definida"])

    def test_cookie_secure_atras_de_proxy_https(self):
        app.CTX.proxy = True
        app.CTX.rede = True
        c = self.novo()
        st, d, hdr = c.req("POST", "/api/setup", {"codigo": "COD123", "usuario": "chefe", "nome": "C", "senha": SENHA},
                           headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(st, 200, d)
        self.assertIn("Secure", hdr["Set-Cookie"])
        aud = app.CTX.auth.listar_auditoria()
        self.assertEqual(aud[0]["ip"], "203.0.113.9")


class TesteBackup(Base):
    def test_backup_consistente_e_rotacao(self):
        app.CTX.banco.adicionar("0000832-35.2018.4.01.3202", "00008323520184013202", "trf1", "meu")
        pasta = self.dados / "bk"
        nomes = []
        for i in range(4):
            arq = backup.fazer_backup(app.CTX.banco, pasta, manter=3)
            nomes.append(arq.name)
            time.sleep(1.05)  # nomes têm resolução de segundos
        itens = backup.listar_backups(pasta)
        self.assertEqual(len(itens), 3)
        self.assertNotIn(nomes[0], [i["arquivo"] for i in itens])  # o mais antigo foi apagado
        con = sqlite3.connect(str(pasta / itens[0]["arquivo"]))
        self.assertEqual(con.execute("SELECT apelido FROM processos").fetchone()[0], "meu")
        con.close()
        self.assertEqual([p.name for p in pasta.iterdir() if p.suffix == ".tmp"], [])

    def test_backup_devido(self):
        import datetime as dt
        pasta = self.dados / "bk2"
        manha = dt.datetime(2026, 11, 11, 1, 0)
        tarde = dt.datetime(2026, 11, 11, 15, 0)
        self.assertFalse(backup.backup_devido(pasta, "02:00", manha))   # ainda não deu a hora
        self.assertTrue(backup.backup_devido(pasta, "02:00", tarde))    # passou da hora e não há backup
        pasta.mkdir()
        (pasta / "processos-20261111-020001.db").write_bytes(b"x")
        self.assertFalse(backup.backup_devido(pasta, "02:00", tarde))   # já existe o de hoje
        self.assertTrue(backup.backup_devido(pasta, "02:00", dt.datetime(2026, 11, 12, 3, 0)))  # amanhã é devido de novo

    def test_rota_de_backup_e_config(self):
        adm = self.admin()
        app.CTX.config.dados["pasta_backup"] = str(self.dados / "meus-backups")
        st, d, _ = adm.req("POST", "/api/backup", {})
        self.assertEqual(st, 200, d)
        st, info, _ = adm.req("GET", "/api/backup")
        self.assertEqual(len(info["backups"]), 1)
        self.assertEqual(info["pasta"], str(self.dados / "meus-backups"))
        self.assertEqual(adm.req("POST", "/api/config", {"backup_hora": "25:99"})[0], 400)
        self.assertEqual(adm.req("POST", "/api/config", {"backup_manter": 0})[0], 400)
        self.assertEqual(adm.req("POST", "/api/config", {"backup_hora": "03:30", "backup_manter": 7})[0], 200)

    def test_nao_copia_config_com_senha_do_email(self):
        app.CTX.config.dados["email"]["senha"] = "segredo-smtp"
        app.CTX.config.salvar()
        pasta = self.dados / "bk3"
        backup.fazer_backup(app.CTX.banco, pasta)
        for p in pasta.iterdir():
            self.assertNotIn(b"segredo-smtp", p.read_bytes())


class TesteTrava(unittest.TestCase):
    def test_segunda_instancia_e_barrada(self):
        with tempfile.TemporaryDirectory() as d:
            a = backup.TravaInstancia(d)
            self.assertIsNone(a.adquirir())
            # outro computador (host diferente) com batimento recente
            b = backup.TravaInstancia(d)
            b.meu = {"host": "OUTRO-PC", "pid": 999999}
            motivo = b.adquirir()
            self.assertIsNotNone(motivo)
            self.assertIn(a.meu["host"], motivo)
            self.assertIsNone(b.adquirir(ignorar=True))  # válvula de escape explícita

    def test_trava_antiga_nao_bloqueia(self):
        with tempfile.TemporaryDirectory() as d:
            a = backup.TravaInstancia(d)
            a.arq.write_text(json.dumps({"host": "OUTRO-PC", "pid": 1, "quando": time.time() - 3600}))
            self.assertIsNone(a.adquirir())  # batimento de uma hora atrás: o outro programa foi encerrado

    def test_liberar_remove_so_a_propria(self):
        with tempfile.TemporaryDirectory() as d:
            a = backup.TravaInstancia(d)
            a.adquirir()
            outro = backup.TravaInstancia(d)
            outro.meu = {"host": "OUTRO-PC", "pid": 5}
            outro.liberar()
            self.assertTrue(a.arq.exists())
            a.liberar()
            self.assertFalse(a.arq.exists())


class TestePastaDeDados(unittest.TestCase):
    def test_prioridade_env_local_json_padrao(self):
        antigo_env = os.environ.pop("MONITOR_DADOS", None)
        local = app.BASE_DIR / "local.json"
        existia = local.exists()
        try:
            if not existia:
                self.assertEqual(app.resolver_pasta_dados(), app.BASE_DIR / "dados")
                local.write_text(json.dumps({"pasta_dados": "/tmp/pasta-do-dropbox"}), encoding="utf-8")
                self.assertEqual(app.resolver_pasta_dados(), Path("/tmp/pasta-do-dropbox"))
            os.environ["MONITOR_DADOS"] = "/tmp/via-env"
            self.assertEqual(app.resolver_pasta_dados(), Path("/tmp/via-env"))
        finally:
            os.environ.pop("MONITOR_DADOS", None)
            if antigo_env:
                os.environ["MONITOR_DADOS"] = antigo_env
            if not existia and local.exists():
                local.unlink()


if __name__ == "__main__":
    unittest.main()
