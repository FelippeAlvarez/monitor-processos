"""Testes da v0.7.1: histórico do processo (linha do tempo única, filtros, paginação, anotações)."""
import datetime as dt
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import app  # noqa: E402
from test_equipe import Base  # noqa: E402

HOJE = dt.date.today()


def dia(n):
    return (HOJE + dt.timedelta(days=n)).isoformat()


class Historico(Base):
    def setUp(self):
        super().setUp()
        self.adm = self.admin()
        self.maria = self.comum(self.adm)
        self.b = app.CTX.banco
        self.pid = self.b.adicionar("0000001-00.2024.8.19.0001", "00000010020248190001", "tjrj", apelido="Caso A", cliente="Vivae")
        self.outro = self.b.adicionar("0000002-00.2024.8.19.0002", "00000020020248190002", "tjrj", apelido="Caso B")
        for i in range(5):
            self.b.conn.execute("INSERT INTO movimentos(processo_id,chave,nome,data_hora,complementos,visto,detectado_em) VALUES(?,?,?,?,?,?,?)",
                                (self.pid, f"m{i}", f"Andamento {i}", dia(-10 + i) + "T10:00:00", "", 0, "2026-11-20T00:00:00Z"))
        self.b.conn.execute("INSERT INTO movimentos(processo_id,chave,nome,data_hora,visto,detectado_em) VALUES(?,?,?,?,?,?)",
                            (self.outro, "x", "Alheio", dia(-1) + "T10:00:00", 0, "2026-11-20T00:00:00Z"))
        for i, g in enumerate(("g1", "g1", "g2")):   # duas cópias do mesmo grupo = uma linha
            self.b.conn.execute("INSERT INTO publicacoes(processo_id,pub_id,data_disp,tipo_com,orgao,texto,grupo,situacao,detectado_em) VALUES(?,?,?,?,?,?,?,?,?)",
                                (self.pid, f"p{i}", dia(-3 - (g == "g2")), "Intimação", "5ª Vara", "Manifeste-se sobre a contestação" if g == "g1" else "Cite-se o réu", g, "nova", "2026-11-20T00:00:00Z"))
        self.b.conn.commit()
        self.tipo = self.adm.req("GET", "/api/tipos-evento")[1][0]["id"]

    def hist(self, cli=None, pid=None, **q):
        from urllib.parse import urlencode
        st, d, _ = (cli or self.maria).req("GET", f"/api/processos/{pid or self.pid}/historico?" + urlencode(q))
        self.assertEqual(st, 200, d)
        return d

    def test_linha_unica_e_isolamento(self):
        st, d, _ = self.maria.req("POST", "/api/eventos", {"titulo": "Audiência X", "tipo_id": self.tipo, "data": dia(5), "processo_id": self.pid})
        self.assertEqual(st, 200, d)
        self.maria.req("POST", f"/api/processos/{self.pid}/anotacoes", {"texto": "Cliente pediu acordo."})
        d = self.hist(limite=100)
        fontes = [x["fonte"] for x in d["itens"]]
        self.assertEqual(d["contagens"], {"andamento": 5, "publicacao": 2, "evento": 1, "anotacao": 1})
        self.assertEqual(sorted(set(fontes)), ["andamento", "anotacao", "evento", "publicacao"])
        self.assertEqual(len(d["itens"]), 9)
        self.assertNotIn("Alheio", [x["titulo"] for x in d["itens"]])
        q = [x["quando"] for x in d["itens"]]
        self.assertEqual(q, sorted(q, reverse=True))
        pub = next(x for x in d["itens"] if x["fonte"] == "publicacao" and "contestação" in x["texto"])
        self.assertEqual(pub["copias"], 2)
        self.assertEqual(d["processo"]["numero"], "0000001-00.2024.8.19.0001")

    def test_paginacao_sem_repetir_nem_perder(self):
        for i in range(7):
            self.maria.req("POST", "/api/eventos", {"titulo": f"Ev{i}", "tipo_id": self.tipo, "data": dia(-2), "processo_id": self.pid})   # empate de data
        todos = self.hist(limite=200)["itens"]
        vistos, cur = [], None
        while True:
            d = self.hist(limite=4, **({"antes": cur} if cur else {}))
            vistos += [(x["fonte"], x["id"]) for x in d["itens"]]
            cur = d["proximo"]
            if not cur:
                break
        self.assertEqual(len(vistos), len(set(vistos)))
        self.assertEqual(set(vistos), {(x["fonte"], x["id"]) for x in todos})
        self.assertEqual(len(vistos), len(todos))

    def test_filtros(self):
        self.maria.req("POST", f"/api/processos/{self.pid}/anotacoes", {"texto": "Ligar para o síndico"})
        self.assertEqual({x["fonte"] for x in self.hist(fonte="andamento")["itens"]}, {"andamento"})
        self.assertEqual({x["fonte"] for x in self.hist(fonte="anotacao,publicacao")["itens"]}, {"anotacao", "publicacao"})
        self.assertEqual([x["titulo"] for x in self.hist(q="síndico")["itens"]], ["Anotação interna"])
        self.assertEqual(len(self.hist(q="cite-se")["itens"]), 1)
        r = self.hist(de=dia(-10), ate=dia(-9), fonte="andamento")["itens"]
        self.assertEqual(len(r), 2)
        self.assertEqual(self.maria.req("GET", f"/api/processos/{self.pid}/historico?antes=lixo")[0], 400)
        self.assertEqual(self.maria.req("GET", "/api/processos/9999/historico")[0], 404)

    def test_anotacoes_permissoes_e_conflito(self):
        st, d, _ = self.maria.req("POST", f"/api/processos/{self.pid}/anotacoes", {"texto": "   "})
        self.assertEqual(st, 400)
        st, d, _ = self.maria.req("POST", f"/api/processos/{self.pid}/anotacoes", {"texto": "Primeira"})
        aid = d["id"]
        self.assertEqual(self.maria.req("POST", f"/api/anotacoes/{aid}", {"texto": "Editada", "versao": 1})[0], 200)
        st, d, _ = self.maria.req("POST", f"/api/anotacoes/{aid}", {"texto": "Velha", "versao": 1})
        self.assertEqual(st, 409)
        self.assertEqual(self.adm.req("POST", f"/api/anotacoes/{aid}", {"texto": "Do admin", "versao": 2})[0], 200)
        item = next(x for x in self.hist()["itens"] if x["fonte"] == "anotacao")
        self.assertTrue(item["editada"])
        self.assertEqual(self.maria.req("DELETE", f"/api/anotacoes/{aid}")[0], 200)
        self.assertEqual(self.hist()["contagens"]["anotacao"], 0)
        self.assertEqual(self.adm.req("DELETE", f"/api/anotacoes/{aid}")[0], 404)

    def test_remover_processo_leva_anotacoes(self):
        self.maria.req("POST", f"/api/processos/{self.pid}/anotacoes", {"texto": "x"})
        self.b.remover(self.pid)
        self.assertEqual(self.b.conn.execute("SELECT COUNT(*) FROM anotacoes").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
