"""Testes da v0.5.3: preferência de aparência (estilo, modo, tamanho do texto) guardada por usuário."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth  # noqa: E402
from test_equipe import Base  # noqa: E402


class Preferencia(Base):
    def test_usuario_novo_nao_tem_preferencia(self):
        c = self.admin()
        _, d, _ = c.req("GET", "/api/sessao")
        self.assertEqual(d["usuario"]["aparencia"], {})

    def test_salva_e_volta_na_sessao(self):
        c = self.admin()
        st, d, _ = c.req("POST", "/api/aparencia", {"estilo": "classico", "modo": "dark", "tam": "grande"})
        self.assertEqual(st, 200, d)
        _, d, _ = c.req("GET", "/api/sessao")
        self.assertEqual(d["usuario"]["aparencia"], {"estilo": "classico", "modo": "dark", "tam": "grande"})

    def test_cada_usuario_tem_a_sua(self):
        adm = self.admin()
        maria = self.comum(adm)
        adm.req("POST", "/api/aparencia", {"estilo": "classico", "modo": "light", "tam": "normal"})
        maria.req("POST", "/api/aparencia", {"estilo": "claro", "modo": "auto", "tam": "grande"})
        self.assertEqual(adm.req("GET", "/api/sessao")[1]["usuario"]["aparencia"]["estilo"], "classico")
        self.assertEqual(maria.req("GET", "/api/sessao")[1]["usuario"]["aparencia"],
                         {"estilo": "claro", "modo": "auto", "tam": "grande"})

    def test_valores_invalidos_sao_recusados(self):
        c = self.admin()
        for corpo in ({"estilo": "neon", "modo": "dark", "tam": "normal"},
                      {"estilo": "claro", "modo": "roxo", "tam": "normal"},
                      {"estilo": "claro", "modo": "dark", "tam": "gigante"},
                      {"estilo": "claro"}, {}, {"estilo": "<script>", "modo": "dark", "tam": "normal"}):
            st, _, _ = c.req("POST", "/api/aparencia", corpo)
            self.assertEqual(st, 400, corpo)
        self.assertEqual(c.req("GET", "/api/sessao")[1]["usuario"]["aparencia"], {})

    def test_exige_login(self):
        self.assertEqual(self.novo().req("POST", "/api/aparencia", {"estilo": "claro", "modo": "auto", "tam": "normal"})[0], 401)

    def test_valor_corrompido_no_banco_vira_padrao(self):
        self.assertEqual(auth.aparencia_de_texto("isto não é json"), {})
        self.assertEqual(auth.aparencia_de_texto('{"estilo":"x","modo":"dark","tam":"normal"}'), {})
        self.assertEqual(auth.aparencia_de_texto(""), {})
        self.assertEqual(auth.aparencia_de_texto(None), {})

    def test_banco_antigo_ganha_a_coluna(self):
        # simula um banco da v0.5.2 (sem a coluna) e confirma a migração automática
        con = sqlite3.connect(self.dados / "antigo.db")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE usuarios(id INTEGER PRIMARY KEY AUTOINCREMENT, usuario TEXT NOT NULL UNIQUE, nome TEXT NOT NULL, "
                    "email TEXT NOT NULL DEFAULT '', perfil TEXT NOT NULL DEFAULT 'usuario', senha_hash TEXT NOT NULL, "
                    "ativo INTEGER NOT NULL DEFAULT 1, trocar_senha INTEGER NOT NULL DEFAULT 0, falhas INTEGER NOT NULL DEFAULT 0, "
                    "bloqueado_ate REAL NOT NULL DEFAULT 0, criado_em TEXT NOT NULL, ultimo_acesso TEXT)")
        con.execute("INSERT INTO usuarios(usuario,nome,senha_hash,criado_em) VALUES('velho','Velho','x','2026-01-01')")
        con.commit()

        class BancoFalso:
            conn = con
            import threading
            lock = threading.RLock()
        auth.Auth(BancoFalso())
        cols = {r["name"] for r in con.execute("PRAGMA table_info(usuarios)").fetchall()}
        self.assertIn("aparencia", cols)
        self.assertEqual(con.execute("SELECT aparencia FROM usuarios").fetchone()["aparencia"], "")
        con.close()

    def test_pagina_traz_as_opcoes_e_o_logotipo(self):
        c = self.novo()
        _, html, _ = c.req("GET", "/")
        for trecho in ('id="t-aparencia"', 'data-estilo="claro"', 'data-estilo="classico"', "data:image/jpeg;base64,",
                       'id="d-conf"', "Alvarez de Sá"):
            self.assertIn(trecho, html)
        self.assertNotIn("confirm(", html.replace("function confirmar", ""))  # sem caixas do navegador
        self.assertNotIn("prompt(", html.replace("perguntar(", "").replace("dialogo(", ""))


if __name__ == "__main__":
    unittest.main()
