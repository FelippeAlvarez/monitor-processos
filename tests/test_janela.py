"""v0.7.4: janela própria (Chrome/Edge em modo aplicativo)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import janela  # noqa: E402
from config import Config  # noqa: E402

ENV = {"ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)", "LocalAppData": r"C:\Users\x\AppData\Local"}
CH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
ED = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


class TesteJanela(unittest.TestCase):
    def test_prefere_chrome_e_cai_para_edge(self):
        self.assertEqual(janela.localizar_navegador(ENV, existe=lambda c: c in (CH, ED), plataforma="win32"), ("Chrome", CH))
        self.assertEqual(janela.localizar_navegador(ENV, existe=lambda c: c == ED, plataforma="win32"), ("Edge", ED))
        self.assertIsNone(janela.localizar_navegador(ENV, existe=lambda c: False, plataforma="win32"))

    def test_chrome_instalado_so_para_o_usuario(self):
        loc = r"C:\Users\x\AppData\Local\Google\Chrome\Application\chrome.exe"
        self.assertEqual(janela.localizar_navegador(ENV, existe=lambda c: c == loc, plataforma="win32"), ("Chrome", loc))

    def test_comando_usa_modo_app_e_perfil_proprio(self):
        cmd = janela.comando("chrome", "http://127.0.0.1:8765", "/tmp/perfil")
        self.assertIn("--app=http://127.0.0.1:8765", cmd)
        self.assertIn("--user-data-dir=/tmp/perfil", cmd)

    def test_perfil_fica_fora_da_pasta_de_dados_no_windows(self):
        self.assertEqual(str(janela.pasta_perfil(ENV, "win32")).replace("\\", "/"), "C:/Users/x/AppData/Local/MonitorProcessos/janela")

    def test_abrir_devolve_o_navegador_usado(self):
        chamadas = []
        with tempfile.TemporaryDirectory() as t:
            r = janela.abrir("http://127.0.0.1:1", localizar=lambda: ("Edge", "edge.exe"), executar=lambda cmd, **kw: chamadas.append(cmd), perfil=t)
        self.assertEqual(r, "Edge")
        self.assertEqual(chamadas[0][0], "edge.exe")

    def test_sem_navegador_ou_com_erro_devolve_none(self):
        self.assertIsNone(janela.abrir("http://x", localizar=lambda: None))

        def quebra(*a, **k):
            raise OSError("não abriu")
        with tempfile.TemporaryDirectory() as t:
            self.assertIsNone(janela.abrir("http://x", localizar=lambda: ("Chrome", "c"), executar=quebra, perfil=t))

    def test_configuracao_liga_e_desliga(self):
        with tempfile.TemporaryDirectory() as t:
            c = Config(Path(t) / "c.json")
            self.assertTrue(c.dados["janela_propria"])
            c.atualizar({"janela_propria": False})
            self.assertFalse(c.publico()["janela_propria"])


if __name__ == "__main__":
    unittest.main()
