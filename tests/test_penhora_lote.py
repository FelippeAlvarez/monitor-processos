"""v0.8.3: marcação de penhora em lote, por números colados, sugestões por polo do cliente e desfazer."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import app  # noqa: E402
import risco  # noqa: E402
from test_equipe import Base  # noqa: E402

N1, N2, N3 = "0000001-00.2024.8.19.0001", "0000002-00.2024.8.19.0001", "0000003-00.2024.8.19.0001"


def limpo(n):
    return "".join(c for c in n if c.isdigit())


class Lote(Base):
    def setUp(self):
        super().setUp()
        self.adm = self.admin()
        self.maria = self.comum(self.adm)
        self.b = app.CTX.banco
        self.p1 = self.b.adicionar(N1, limpo(N1), "tjrj", apelido="A", cliente="Banco")
        self.p2 = self.b.adicionar(N2, limpo(N2), "tjrj", apelido="B", cliente="Banco")
        self.p3 = self.b.adicionar(N3, limpo(N3), "tjrj", apelido="C", cliente="Devedor")

    def pos(self, pid):
        return self.b.obter(pid)["posicao"]

    def parte(self, pid, nome, polo, lado="cliente"):
        self.b.conn.execute("INSERT INTO processo_partes(processo_id,nome,chave,polo,lado,origem) VALUES(?,?,?,?,?,'auto')",
                            (pid, nome, nome.lower(), polo, lado))
        self.b.conn.commit()

    def classe(self, pid, c):
        self.b.conn.execute("UPDATE processos SET classe=? WHERE id=?", (c, pid))
        self.b.conn.commit()

    def test_numeros_do_texto(self):
        t = f"{N1}\n{limpo(N2)}, {N1} lixo 123"
        self.assertEqual(risco.numeros_do_texto(t), [limpo(N1), limpo(N2)])
        self.assertEqual(risco.numeros_do_texto(None), [])

    def test_lote_por_ids_e_numeros_com_nao_encontrados_e_desfazer(self):
        c = self.maria
        st, d, _ = c.req("POST", "/api/penhora/lote", {"ids": [self.p1], "numeros": f"{N2}\n9999999-99.2024.8.19.0001", "posicao": "exequente"})
        self.assertEqual(st, 200, d)
        self.assertEqual(d["alterados"], 2)
        self.assertEqual(d["nao_encontrados"], ["99999999920248190001"])
        self.assertEqual((self.pos(self.p1), self.pos(self.p2), self.pos(self.p3)), ("exequente", "exequente", ""))
        # repetir não altera nada e não oferece desfazer
        d = c.req("POST", "/api/penhora/lote", {"ids": [self.p1], "posicao": "exequente"})[1]
        self.assertEqual((d["alterados"], d["ja_estavam"], d["anteriores"]), (0, 1, {}))
        # remover em lote e desfazer
        d = c.req("POST", "/api/penhora/lote", {"ids": [self.p1, self.p2], "posicao": ""})[1]
        self.assertEqual(d["alterados"], 2)
        self.assertEqual(self.pos(self.p1), "")
        st, r, _ = c.req("POST", "/api/penhora/lote", {"restaurar": d["anteriores"]})
        self.assertEqual((st, r["alterados"]), (200, 2))
        self.assertEqual((self.pos(self.p1), self.pos(self.p2)), ("exequente", "exequente"))

    def test_validacoes(self):
        c = self.maria
        self.assertEqual(c.req("POST", "/api/penhora/lote", {"ids": [self.p1], "posicao": "ninguem"})[0], 400)
        self.assertEqual(c.req("POST", "/api/penhora/lote", {"restaurar": {str(self.p1): "x"}})[0], 400)
        self.assertEqual(c.req("POST", "/api/penhora/lote", {"ids": ["abc"], "posicao": "executado"})[0], 400)
        self.assertEqual(c.req("POST", "/api/penhora/lote", {"ids": [999999], "posicao": "executado"})[1]["alterados"], 0)
        self.assertEqual(self.novo().req("POST", "/api/penhora/lote", {"ids": [self.p1], "posicao": "executado"})[0], 401)
        self.assertEqual(self.novo().req("GET", "/api/penhora/sugestoes")[0], 401)

    def test_remover_limpa_estado_de_aviso(self):
        self.b.conn.execute("INSERT INTO risco_estado(processo_id,grau,avisado,atualizado_em) VALUES(?,?,?,?)", (self.p1, "alto", "alto", "x"))
        self.b.conn.commit()
        risco.definir_lote(self.b, [self.p1], "", "executado")
        risco.definir_lote(self.b, [self.p1], "", "")
        self.assertIsNone(self.b.conn.execute("SELECT 1 FROM risco_estado WHERE processo_id=?", (self.p1,)).fetchone())

    def test_sugestoes_por_polo_do_cliente(self):
        self.classe(self.p1, "Execução de Título Extrajudicial")
        self.parte(self.p1, "Banco SA", "A")
        self.classe(self.p2, "Cumprimento de Sentença")
        self.parte(self.p2, "Banco SA", "P")
        self.parte(self.p2, "Fulano", "A", "contraria")
        self.classe(self.p3, "Execução Fiscal")                     # cliente não identificado
        p4 = self.b.adicionar("0000004-00.2024.8.19.0001", "00000040020248190001", "tjrj")
        self.classe(p4, "Embargos à Execução")                       # não é execução
        p5 = self.b.adicionar("0000005-00.2024.8.19.0001", "00000050020248190001", "tjrj")
        self.classe(p5, "Procedimento Comum Cível")
        p6 = self.b.adicionar("0000006-00.2024.8.19.0001", "00000060020248190001", "tjrj")
        self.classe(p6, "Cumprimento de Sentença")
        self.parte(p6, "Banco SA", "A")
        self.parte(p6, "Banco SA 2", "P")                            # nos dois polos
        d = self.maria.req("GET", "/api/penhora/sugestoes")[1]
        por_id = {x["id"]: x for x in d["itens"]}
        self.assertEqual(set(por_id), {self.p1, self.p2, self.p3, p6})
        self.assertEqual(por_id[self.p1]["sugerida"], "exequente")
        self.assertEqual(por_id[self.p2]["sugerida"], "executado")
        self.assertEqual(por_id[self.p3]["sugerida"], "")
        self.assertEqual(por_id[p6]["sugerida"], "")
        self.assertEqual((d["total"], d["com_sugestao"]), (4, 2))
        self.assertEqual([x["id"] for x in d["itens"][:2]], [self.p1, self.p2])  # com sugestão primeiro
        # o que já foi marcado sai da lista; nada foi gravado pela consulta
        self.assertEqual(self.pos(self.p1), "")
        risco.definir_lote(self.b, [self.p1], "", "exequente")
        self.assertNotIn(self.p1, {x["id"] for x in risco.sugerir(self.b)["itens"]})

    def test_parte_excluida_nao_conta_e_arquivado_fica_fora(self):
        self.classe(self.p1, "Execução de Título Extrajudicial")
        self.parte(self.p1, "Banco SA", "A")
        self.b.conn.execute("UPDATE processo_partes SET excluida=1 WHERE processo_id=?", (self.p1,))
        self.b.conn.execute("UPDATE processos SET ativo=0 WHERE id=?", (self.p2,))
        self.classe(self.p2, "Cumprimento de Sentença")
        self.b.conn.commit()
        itens = {x["id"]: x for x in risco.sugerir(self.b)["itens"]}
        self.assertEqual(itens[self.p1]["sugerida"], "")
        self.assertNotIn(self.p2, itens)


if __name__ == "__main__":
    unittest.main()
