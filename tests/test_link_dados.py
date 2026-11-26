"""v0.8.2: link do ato no tribunal guardado nas publicações e quadro 'Dados do processo' no Histórico."""
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402
import auth as auth_mod  # noqa: E402
import dje  # noqa: E402
import historico  # noqa: E402
import test_equipe  # noqa: E402
import test_partes as tp  # noqa: E402
from config import Config  # noqa: E402
from db import Banco  # noqa: E402
from monitor import Monitor  # noqa: E402

URL = "https://eproc1g.tjrj.jus.br/eproc/controlador.php?acao=acessar_documento&doc=123"


def bruto(i, link, data="2026-11-24", texto="Intime-se.", hash_="HASH%d"):
    return {"id": i, "data_disponibilizacao": data, "tipoComunicacao": "Intimação", "nomeOrgao": "7ª Vara Cível", "texto": texto,
            "numero_processo": tp.NUM, "tipoDocumento": "Despacho", "nomeClasse": "EXECUçãO", "link": link, "hash": hash_ % i,
            "destinatarios": [{"nome": tp.SICOOB, "polo": "A"}], "destinatarioadvogados": tp.ADVS}


class TesteNormalizar(unittest.TestCase):
    def test_aceita_so_http_e_https(self):
        self.assertEqual(dje.normalizar_item(bruto(1, URL))["link"], URL)
        for ruim in ("javascript:alert(1)", "data:text/html,x", "ftp://x.com/a", "//x.com", "https://x.com/a b", 'https://x.com/"onclick=', None, "", 5):
            self.assertEqual(dje.normalizar_item(bruto(1, ruim))["link"], "", repr(ruim))

    def test_limita_o_tamanho(self):
        self.assertLessEqual(len(dje.normalizar_item(bruto(1, "https://x.com/" + "a" * 900))["link"]), 500)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = Banco(Path(self.tmp.name) / "p.db")
        self.pid = self.b.adicionar("3219499-19.2026.8.19.0001", tp.NUM, "api_publica_tjrj", "", "Sicoob Empresas RJ", "")

    def tearDown(self):
        self.b.conn.close()
        self.tmp.cleanup()


class TesteGravacao(Base):
    def test_grava_o_link_e_preenche_publicacao_antiga_na_proxima_consulta(self):
        self.b.registrar_publicacoes(self.pid, [dje.normalizar_item(bruto(1, "")), dje.normalizar_item(bruto(2, URL))])
        links = {r["pub_id"]: r["link"] for r in self.b._linhas("SELECT pub_id, link FROM publicacoes")}
        self.assertEqual(links, {"1": "", "2": URL})
        self.b.registrar_publicacoes(self.pid, [dje.normalizar_item(bruto(1, URL + "1"))])      # a API passou a informar o link
        self.b.registrar_publicacoes(self.pid, [dje.normalizar_item(bruto(2, ""))])             # e depois deixou de informar: não apaga
        links = {r["pub_id"]: r["link"] for r in self.b._linhas("SELECT pub_id, link FROM publicacoes")}
        self.assertEqual(links, {"1": URL + "1", "2": URL})

    def test_grupo_de_copias_usa_o_link_de_qualquer_copia(self):
        longo = "Intime-se a parte executada para pagar o débito em quinze dias, sob pena de multa de dez por cento."
        a, c = bruto(1, "", texto=longo), bruto(2, URL, texto=longo)
        self.b.registrar_publicacoes(self.pid, [dje.normalizar_item(a), dje.normalizar_item(c)])
        pubs = self.b.publicacoes(self.pid)
        self.assertEqual(len(pubs), 1)
        self.assertEqual(pubs[0]["copias"], 2)
        self.assertEqual(pubs[0]["link"], URL)
        self.assertTrue(pubs[0]["hash"])

    def test_linha_do_tempo_traz_link_e_hash(self):
        self.b.registrar_publicacoes(self.pid, [dje.normalizar_item(bruto(5, URL))])
        itens, _ = historico.linha(self.b, self.pid, ["publicacao"])
        self.assertEqual((itens[0]["link"], itens[0]["hash"]), (URL, "HASH5"))

    def test_publicacao_de_filtro_oab(self):
        self.b.conn.execute("INSERT INTO oabs(numero,uf,tipo,criado_em,desde) VALUES('125449','RJ','oab','x','2026-01-01')")
        oid = self.b.conn.execute("SELECT id FROM oabs").fetchone()[0]
        self.b.conn.commit()
        outro = "00000020020248190002"
        it = dje.normalizar_item(dict(bruto(9, URL), numero_processo=outro))
        self.b.registrar_oab_pubs(oid, [it])
        self.assertEqual(self.b.oab_publicacoes()[0]["link"], URL)


class TesteQuadro(Base):
    def test_historico_traz_dados_com_partes_advogados_e_vara(self):
        self.b.registrar_publicacoes(self.pid, [dje.normalizar_item(dict(
            bruto(1, URL), texto=tp.CAB, destinatarios=[{"nome": tp.SICOOB, "polo": "A"}, {"nome": "EQUILIBRIO ESPACO TERAPEUTICO LTDA", "polo": "P"}]))])
        tmp = tempfile.TemporaryDirectory()
        base = Path(tmp.name)
        try:
            app.DADOS = base
            app.CTX.config = Config(base / "c.json")
            app.CTX.config.dados["dje_ativo"] = False
            app.CTX.banco = self.b
            app.CTX.auth = auth_mod.Auth(self.b)
            app.CTX.monitor = Monitor(self.b, app.CTX.config, pausa_entre_consultas=0)
            app.CTX.sobre = {"versao": "t"}
            app.CTX.rede = app.CTX.proxy = False
            app.CTX.auth.criar("chefe", "Chefe", "", "admin", test_equipe.SENHA, trocar_senha=False)
            srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            c = test_equipe.Cliente(srv.server_port)
            c.req("POST", "/api/login", {"usuario": "chefe", "senha": test_equipe.SENHA})
            st, r, _ = c.req("GET", "/api/processos/%d/historico" % self.pid)
            self.assertEqual(st, 200)
            d = r["dados"]
            self.assertEqual(d["numero"], "3219499-19.2026.8.19.0001")
            self.assertTrue(any("EQUILIBRIO" in x["nome"] for x in d["partes"]))
            self.assertIn("EQUILIBRIO ESPACO TERAPEUTICO LTDA", d["resumo_partes"]["contraria"])
            self.assertTrue(any("FELIPPE" in a["nome"] for a in d["advogados"]))
            self.assertIn("vara_exib", d)
            self.assertEqual(d["responsavel_nome"], "")
            st, r2, _ = c.req("GET", "/api/processos/%d/historico?antes=%s" % (self.pid, "2026-01-01T00:00:00~andamento~0"))
            self.assertNotIn("dados", r2)     # só na 1ª página
            srv.shutdown()
            srv.server_close()
        finally:
            tmp.cleanup()


class TesteFrontend(unittest.TestCase):
    def setUp(self):
        self.html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")

    def test_botoes_e_quadro(self):
        for t in ("function linkAto(", "function quadroDados(", 'id="h-dados"', "data-hdados", "Abrir no tribunal", "linkAto(b)", "linkAto(x)"):
            self.assertIn(t, self.html, t)

    def test_link_so_http_na_tela(self):
        i = self.html.index("function linkAto(")
        self.assertIn("^https?:", self.html[i:i + 400])


if __name__ == "__main__":
    unittest.main()
