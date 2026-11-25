"""v0.7.5: pesquisa avulsa no DJEN (OAB, número do processo, nome da parte, período)."""
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dje  # noqa: E402

N1, N2 = "32194991920268190001", "00000020020248190002"


def it(i, numero, data, tx, dest, advs=(), trib="TJRJ"):
    return {"id": i, "numero_processo": numero, "data_disponibilizacao": data, "texto": tx, "tipoComunicacao": "Intimação",
            "nomeOrgao": "7ª Vara Cível", "siglaTribunal": trib, "tipoDocumento": "Despacho", "nomeClasse": "EXECUçãO",
            "link": "https://tribunal.example/doc/%s" % i,
            "destinatarios": [{"nome": n, "polo": p} for n, p in dest],
            "destinatarioadvogados": [{"advogado": {"nome": n, "numero_oab": o, "uf_oab": u}} for n, o, u in advs]}


BASE = [
    it(1, N1, "2026-11-25", "Intime-se.", [("SICOOB EMPRESAS RJ", "A"), ("EQUILIBRIO ESPACO TERAPEUTICO LTDA", "P")], [("FELIPPE", "125449", "RJ")]),
    it(2, N1, "2026-11-24", "Sentença.", [("SICOOB EMPRESAS RJ", "A")], [("FELIPPE", "125449", "RJ")]),
    it(3, N2, "2026-11-25", "Manifeste-se o autor.", [("CONDOMINIO VIVAE", "A")], [("OUTRO ADV", "999", "RJ")]),
]


class Falso(BaseHTTPRequestHandler):
    modo = "normal"       # normal | ignora_filtros | recusa_nome
    chamadas = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        Falso.chamadas.append(q)
        if Falso.modo == "recusa_nome" and "nomeParte" in q:
            self.send_response(400)
            self.end_headers()
            return
        itens = list(BASE)
        if Falso.modo != "ignora_filtros":
            if "numeroProcesso" in q:
                itens = [i for i in itens if i["numero_processo"] == q["numeroProcesso"]]
            if "numeroOab" in q:
                itens = [i for i in itens if any(a["advogado"]["numero_oab"] == q["numeroOab"] for a in i["destinatarioadvogados"])]
            if "nomeParte" in q:
                itens = [i for i in itens if any(q["nomeParte"].upper() in d["nome"] for d in i["destinatarios"])]
            if "texto" in q:
                itens = [i for i in itens if q["texto"].upper() in (i["texto"] + " ".join(d["nome"] for d in i["destinatarios"])).upper()]
            itens = [i for i in itens if q.get("dataDisponibilizacaoInicio", "") <= i["data_disponibilizacao"] <= q.get("dataDisponibilizacaoFim", "9")]
        pag = int(q.get("pagina", 1))
        corpo = json.dumps({"status": "success", "count": len(itens), "items": itens if pag == 1 else []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(corpo)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Falso)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Falso.modo, Falso.chamadas = "normal", []

    def pesq(self, **kw):
        kw.setdefault("inicio", "2026-11-24")
        kw.setdefault("fim", "2026-11-25")
        f = dje.validar_pesquisa(**kw)
        return dje.pesquisar(f, base_url=self.url)


class TesteValidacao(unittest.TestCase):
    def v(self, **kw):
        kw.setdefault("inicio", "2026-11-25")
        kw.setdefault("fim", "2026-11-25")
        return dje.validar_pesquisa(hoje=__import__("datetime").date(2026, 11, 25), **kw)

    def test_exige_ao_menos_um_filtro(self):
        with self.assertRaises(dje.ErroPesquisa):
            self.v()

    def test_oab_exige_uf_e_formato(self):
        with self.assertRaises(dje.ErroPesquisa):
            self.v(oab="125449")
        with self.assertRaises(dje.ErroPesquisa):
            self.v(oab="12AB", uf="RJ")
        self.assertEqual(self.v(oab="0125449", uf="rj")["oab"], "125449")

    def test_numero_processo_20_digitos_aceita_mascara(self):
        self.assertEqual(self.v(numero="3219499-19.2026.8.19.0001")["numero"], N1)
        with self.assertRaises(dje.ErroPesquisa):
            self.v(numero="123")

    def test_nome_minimo_tres_letras(self):
        with self.assertRaises(dje.ErroPesquisa):
            self.v(nome="Jo")

    def test_periodo(self):
        with self.assertRaises(dje.ErroPesquisa):
            self.v(nome="Silva", inicio="2026-11-25", fim="2026-11-20")           # invertido
        with self.assertRaises(dje.ErroPesquisa):
            self.v(nome="Silva", inicio="2025-01-01", fim="2026-11-25")           # mais de 366 dias
        with self.assertRaises(dje.ErroPesquisa):
            self.v(nome="Silva", inicio="25/11/2026", fim="2026-11-25")           # formato
        f = self.v(nome="Silva", inicio="2026-11-20", fim="2026-12-30")           # fim no futuro: limita a hoje
        self.assertEqual(f["fim"], "2026-11-25")
        with self.assertRaises(dje.ErroPesquisa):
            self.v(nome="Silva", inicio="2026-12-01", fim="2026-12-30")           # tudo no futuro


class TesteBusca(Base):
    def test_por_nome_da_parte_acha_a_parte_contraria(self):
        r = self.pesq(nome="equilibrio espaco")
        self.assertEqual([i["pub_id"] for i in r["itens"]], ["1"])
        self.assertEqual(Falso.chamadas[0]["nomeParte"], "equilibrio espaco")
        self.assertEqual(r["itens"][0]["link"], "https://tribunal.example/doc/1")

    def test_por_oab_e_periodo(self):
        r = self.pesq(oab="125449", uf="RJ", inicio="2026-11-25", fim="2026-11-25")
        self.assertEqual([i["pub_id"] for i in r["itens"]], ["1"])
        self.assertEqual(Falso.chamadas[0]["numeroOab"], "125449")
        self.assertEqual(Falso.chamadas[0]["ufOab"], "RJ")

    def test_por_numero_do_processo_ordena_do_mais_novo(self):
        r = self.pesq(numero=N1)
        self.assertEqual([i["pub_id"] for i in r["itens"]], ["1", "2"])

    def test_filtros_combinados_valem_juntos(self):
        r = self.pesq(oab="125449", uf="RJ", nome="SICOOB")
        self.assertEqual({i["pub_id"] for i in r["itens"]}, {"1", "2"})
        self.assertEqual(self.pesq(oab="999", uf="RJ", nome="SICOOB")["itens"], [])

    def test_sem_resultado(self):
        r = self.pesq(nome="NINGUEM COM ESSE NOME")
        self.assertEqual((r["itens"], r["descartados"]), ([], 0))

    def test_api_que_ignora_filtros_nao_vira_resultado_falso(self):
        Falso.modo = "ignora_filtros"
        r = self.pesq(nome="EQUILIBRIO")
        self.assertEqual([i["pub_id"] for i in r["itens"]], ["1"])      # só o que contém o nome
        self.assertEqual(r["descartados"], 2)
        r = self.pesq(oab="125449", uf="RJ")
        self.assertEqual({i["pub_id"] for i in r["itens"]}, {"1", "2"})  # o item da OAB 999 é descartado
        r = self.pesq(numero=N2, inicio="2026-11-25", fim="2026-11-25")
        self.assertEqual([i["pub_id"] for i in r["itens"]], ["3"])

    def test_api_que_recusa_nomeparte_cai_para_busca_no_texto(self):
        Falso.modo = "recusa_nome"
        r = self.pesq(nome="EQUILIBRIO")
        self.assertEqual(r["modo"], "nome_no_texto")
        self.assertEqual([i["pub_id"] for i in r["itens"]], ["1"])
        self.assertIn("texto", Falso.chamadas[-1])
        self.assertNotIn("nomeParte", Falso.chamadas[-1])

    def test_tribunal_filtra_localmente(self):
        Falso.modo = "ignora_filtros"
        self.assertEqual(self.pesq(nome="VIVAE", tribunal="TRT1")["itens"], [])
        self.assertEqual([i["pub_id"] for i in self.pesq(nome="VIVAE", tribunal="tjrj")["itens"]], ["3"])


class TesteRota(Base):
    def setUp(self):
        super().setUp()
        import app
        import auth as auth_mod
        import test_equipe
        from config import Config
        from db import Banco
        from monitor import Monitor
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.app = app
        app.DADOS = base
        app.CTX.config = Config(base / "c.json")
        app.CTX.config.dados["dje_base_url"] = self.url
        app.CTX.banco = Banco(base / "p.db")
        app.CTX.auth = auth_mod.Auth(app.CTX.banco)
        app.CTX.monitor = Monitor(app.CTX.banco, app.CTX.config, pausa_entre_consultas=0)
        app.CTX.sobre = {"versao": "t"}
        app.CTX.rede = app.CTX.proxy = False
        self.srv_app = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.srv_app.serve_forever, daemon=True).start()
        app.CTX.auth.criar("chefe", "Chefe", "", "admin", "senha-de-teste-123", trocar_senha=False)
        self.cli = test_equipe.Cliente(self.srv_app.server_port)
        self.cli.req("POST", "/api/login", {"usuario": "chefe", "senha": "senha-de-teste-123"})
        self.banco = app.CTX.banco

    def tearDown(self):
        self.srv_app.shutdown()
        self.srv_app.server_close()
        self.banco.conn.close()
        self.tmp.cleanup()

    def test_rota_marca_processo_cadastrado_nao_grava_publicacoes_e_audita(self):
        pid = self.banco.adicionar("3219499-19.2026.8.19.0001", N1, "tjrj", "", "Sicoob", "")
        st, r, _ = self.cli.req("POST", "/api/dje/pesquisar", {"nome": "SICOOB", "inicio": "2026-11-24", "fim": "2026-11-25"})
        self.assertEqual(st, 200)
        por_id = {i["pub_id"]: i for i in r["itens"]}
        self.assertEqual(por_id["1"]["processo_id"], pid)
        self.assertEqual(por_id["1"]["numero_fmt"], "3219499-19.2026.8.19.0001")
        self.assertEqual(r["processos"], 1)
        self.assertEqual(self.banco.conn.execute("SELECT COUNT(*) FROM publicacoes").fetchone()[0], 0)   # nada gravado
        log = self.banco.conn.execute("SELECT * FROM auditoria").fetchall() if self._tem("auditoria") else []
        if log:
            self.assertIn("SICOOB", " ".join(str(dict(x)) for x in log))

    def _tem(self, nome):
        return bool(self.banco.conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (nome,)).fetchone())

    def test_rota_recusa_filtro_invalido_e_exige_login(self):
        st, r, _ = self.cli.req("POST", "/api/dje/pesquisar", {"inicio": "2026-11-25", "fim": "2026-11-25"})
        self.assertEqual(st, 400)
        import test_equipe
        anon = test_equipe.Cliente(self.srv_app.server_port)
        self.assertEqual(anon.req("POST", "/api/dje/pesquisar", {"nome": "X Y Z", "inicio": "2026-11-25", "fim": "2026-11-25"})[0], 401)

    def test_so_uma_pesquisa_por_vez(self):
        self.assertTrue(self.app._PESQ_LOCK.acquire(blocking=False))
        try:
            st, r, _ = self.cli.req("POST", "/api/dje/pesquisar", {"nome": "SICOOB", "inicio": "2026-11-25", "fim": "2026-11-25"})
            self.assertEqual(st, 409)
        finally:
            self.app._PESQ_LOCK.release()


if __name__ == "__main__":
    unittest.main()
