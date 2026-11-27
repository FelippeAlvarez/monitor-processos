"""v0.8.3.1: registro (log) maior, com download completo em .txt e quantidade de linhas escolhida."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import app  # noqa: E402
from test_equipe import Base  # noqa: E402


class LogDownload(Base):
    def setUp(self):
        super().setUp()
        self.adm = self.admin()
        (app.DADOS / "monitor.log.1").write_text("antigo 1\nantigo 2\n", encoding="utf-8")
        (app.DADOS / "monitor.log").write_text("\n".join(f"linha {i}" for i in range(400)) + "\n", encoding="utf-8")

    def test_baixar_completo_em_ordem_com_nome_de_arquivo(self):
        st, txt, cab = self.adm.req("GET", "/api/log?baixar=1")
        self.assertEqual(st, 200)
        self.assertIn("attachment; filename=\"monitor-log-", cab["Content-Disposition"])
        self.assertTrue(cab["Content-Disposition"].rstrip('"').endswith(".txt"))
        self.assertTrue(txt.startswith("antigo 1\nantigo 2\nlinha 0\n"))
        self.assertTrue(txt.rstrip().endswith("linha 399"))

    def test_linhas_padrao_e_limites(self):
        self.assertEqual(len(self.adm.req("GET", "/api/log")[1].splitlines()), 300)
        self.assertEqual(len(self.adm.req("GET", "/api/log?linhas=5000")[1].splitlines()), 402)
        self.assertEqual(len(self.adm.req("GET", "/api/log?linhas=1")[1].splitlines()), 50)      # mínimo 50
        self.assertEqual(len(self.adm.req("GET", "/api/log?linhas=abc")[1].splitlines()), 300)

    def test_so_administrador(self):
        comum = self.comum(self.adm)
        self.assertEqual(comum.req("GET", "/api/log?baixar=1")[0], 403)
        self.assertEqual(self.novo().req("GET", "/api/log?baixar=1")[0], 401)


if __name__ == "__main__":
    unittest.main()
