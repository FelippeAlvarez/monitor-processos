"""v0.8.1: números de processo viram link para o Histórico — as listas precisam trazer o id do processo."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Banco  # noqa: E402

NUM = "3219499-19.2026.8.19.0001"
LIMPO = "32194991920268190001"


class TesteIds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = Banco(Path(self.tmp.name) / "p.db")
        self.b.conn.execute("INSERT INTO oabs(numero,uf,tipo,criado_em,desde) VALUES('1','RJ','oab','x','x')")
        self.oid = self.b.conn.execute("SELECT id FROM oabs").fetchone()[0]
        self.b.conn.commit()

    def tearDown(self):
        self.b.conn.close()
        self.tmp.cleanup()

    def oab_pub(self, numero_limpo):
        self.b.conn.execute("INSERT INTO oab_pubs(oab_id,pub_id,numero_processo,detectado_em) VALUES(?,?,?,'x')", (self.oid, "p" + numero_limpo, numero_limpo))
        self.b.conn.execute("INSERT OR IGNORE INTO descobertos(oab_id,numero,numero_limpo) VALUES(?,?,?)", (self.oid, numero_limpo, numero_limpo))
        self.b.conn.commit()

    def test_filtros_e_descobertos_trazem_id_do_processo_cadastrado(self):
        pid = self.b.adicionar(NUM, LIMPO, "tjrj")
        self.oab_pub(LIMPO)
        self.oab_pub("99999999999999999999")
        pubs = {x["numero_processo"]: x for x in self.b.oab_publicacoes()}
        self.assertEqual(pubs[LIMPO]["processo_id"], pid)
        self.assertTrue(pubs[LIMPO]["cadastrado"])
        self.assertIsNone(pubs["99999999999999999999"]["processo_id"])
        desc = {x["numero_limpo"]: x for x in self.b.descobertos(self.oid)["itens"]}
        self.assertEqual(desc[LIMPO]["processo_id"], pid)
        self.assertEqual(desc[LIMPO]["situacao"], "cadastrado")
        self.assertIsNone(desc["99999999999999999999"]["processo_id"])

    def test_publicacoes_e_listagem_trazem_id(self):
        pid = self.b.adicionar(NUM, LIMPO, "tjrj")
        self.b.conn.execute("INSERT INTO publicacoes(processo_id,pub_id,data_disp,detectado_em,grupo) VALUES(?,?,?,?,?)", (pid, "a", "2026-11-25", "x", "g"))
        self.b.conn.commit()
        self.assertEqual(self.b.publicacoes()[0]["processo_id"], pid)
        self.assertEqual(self.b.listar()[0]["id"], pid)


class TesteFrontend(unittest.TestCase):
    def test_helper_e_pontos_de_uso(self):
        html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function linkProc(", html)
        self.assertIn('data-plink="${pid}"', html)
        for trecho in ("linkProc(p.id, p.numero)", "linkProc(b.processo_id, b.numero)", "linkProc(x.pid, x.numero)",
                       "linkProc(e.processo_id, e.processo_numero)", "linkProc(x.processo_id, x.numero)",
                       "linkProc(b.processo_id, b.numero_fmt)", "linkProc(x.id, x.numero)"):
            self.assertIn(trecho, html, trecho)

    def test_helper_escapa_o_numero(self):
        html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
        i = html.index("function linkProc(")
        self.assertIn("esc(numero)", html[i:i + 260])


if __name__ == "__main__":
    unittest.main()
