"""Testes do risco de penhora (v0.6.5): sinais por palavras-chave, graus, prazo aproximado, visão do credor, avisos por e-mail, rotas."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import app  # noqa: E402
import risco  # noqa: E402
from test_equipe import Base  # noqa: E402


class Risco(Base):
    def setUp(self):
        super().setUp()
        risco._ult_aviso["t"] = 0.0
        self.adm = self.admin()
        self.maria = self.comum(self.adm)
        self.b = app.CTX.banco
        self.pid = self.b.adicionar("0000001-00.2024.8.19.0001", "00000010020248190001", "tjrj", apelido="Caso A", cliente="Devedor SA")
        self.n = 0

    def pub(self, data, texto, tipo="Intimação", pid=None):
        self.n += 1
        self.b.conn.execute("INSERT INTO publicacoes(processo_id,pub_id,data_disp,tipo_com,orgao,texto,grupo,situacao,detectado_em) VALUES(?,?,?,?,?,?,?,?,?)",
                            (pid or self.pid, f"x{self.n}", data, tipo, "1ª Vara", texto, f"g{self.n}", "nova", "2026-11-20T00:00:00Z"))
        self.b.conn.commit()

    def mov(self, data, nome, compl=""):
        self.n += 1
        self.b.conn.execute("INSERT INTO movimentos(processo_id,chave,nome,data_hora,complementos,visto,detectado_em) VALUES(?,?,?,?,?,?,?)",
                            (self.pid, f"m{self.n}", nome, data + "T10:00:00", compl, 0, "2026-11-20T00:00:00Z"))
        self.b.conn.commit()

    def av(self, pos="executado", hoje="2026-11-24"):
        p = self.b.obter(self.pid)
        p["posicao"] = pos
        return risco.avaliar(self.b, p, hoje=hoje)

    def test_graus_do_executado(self):
        self.assertEqual(self.av()["grau"], "")
        self.b.conn.execute("UPDATE processos SET classe='Cumprimento de Sentença' WHERE id=?", (self.pid,))
        self.b.conn.commit()
        self.assertEqual(self.av()["grau"], "baixo")
        self.pub("2026-11-10", "Intime-se o executado para pagar o débito no prazo de 15 dias, sob pena de multa de 10% (art. 523 do CPC).")
        r = self.av()
        self.assertEqual(r["grau"], "medio")
        self.assertEqual(r["prazo"]["dias"], 15)
        self.assertTrue(r["prazo"]["aproximado"])
        venc = r["prazo"]["vencimento"]
        self.assertGreater(venc, "2026-11-24")
        self.assertEqual(self.av(hoje="2027-01-30")["grau"], "alto")        # prazo vencido
        self.pub("2026-11-20", "Requer o exequente a penhora de ativos via SISBAJUD.")
        self.assertEqual(self.av()["grau"], "alto")
        self.pub("2026-11-22", "Defiro o bloqueio de valores via Sisbajud, até o limite do débito.")
        self.assertEqual(self.av()["grau"], "critico")
        self.mov("2026-11-23", "Bloqueio efetivado", "valores bloqueados parcialmente")
        r = self.av()
        self.assertEqual(r["grau"], "critico")
        self.assertIn("bloqueio", r["rotulo"].lower())
        self.mov("2026-11-24", "Desbloqueio de valores")
        self.assertNotEqual(self.av()["grau"], "critico")
        self.pub("2026-11-24", "Julgo extinta a execução pelo pagamento (art. 924, II).")
        self.assertEqual(self.av()["grau"], "")
        self.assertIn("encerrada", self.av()["rotulo"].lower())

    def test_acordo_reduz(self):
        self.pub("2026-11-01", "Intime-se o executado para pagar (art. 523).")
        self.pub("2026-11-05", "Homologo o acordo celebrado entre as partes e suspendo o feito (art. 922 do CPC).")
        r = self.av()
        self.assertEqual(r["grau"], "baixo")
        self.assertIn("acordo", r["rotulo"].lower())

    def test_acentos_e_trecho(self):
        self.pub("2026-11-02", "DEFIRO a realização de BLOQUEIO de valores pelo SISBAJUD.")
        r = self.av()
        self.assertEqual(r["grau"], "critico")
        self.assertIn("BLOQUEIO", r["sinais"][-1]["trecho"])

    def test_visao_do_credor(self):
        self.b.conn.execute("UPDATE processos SET classe='Cumprimento de Sentença' WHERE id=?", (self.pid,))
        self.b.conn.commit()
        self.mov("2026-10-01", "Citação realizada", "executado intimado para pagar (art. 523)")
        r = self.av("exequente", hoje="2027-01-30")
        self.assertEqual(r["grau"], "oportunidade")
        self.assertTrue(any("523, § 3º" in s for s in r["sugestoes"]))
        self.pub("2027-01-20", "Defiro a penhora via SISBAJUD requerida pelo exequente.")
        self.assertEqual(self.av("exequente", hoje="2027-01-30")["grau"], "acompanhar")
        self.assertEqual(self.av("exequente", hoje="2026-10-02")["grau"], "acompanhar")

    def test_sugestoes_com_aviso_de_fraude_e_sem_dispor_de_bens(self):
        self.pub("2026-11-10", "Intime-se o executado para pagar (art. 523).")
        s = " ".join(self.av()["sugestoes"])
        self.assertIn("art. 792", s)
        self.assertIn("fraude à execução", s)

    def test_rotas_e_permissao(self):
        c = self.maria
        self.assertEqual(c.req("POST", f"/api/processos/{self.pid}/posicao", {"posicao": "ninguem"})[0], 400)
        self.assertEqual(c.req("POST", "/api/processos/999/posicao", {"posicao": "executado"})[0], 400)
        self.assertEqual(c.req("POST", f"/api/processos/{self.pid}/posicao", {"posicao": "executado"})[0], 200)
        self.pub("2026-11-22", "Defiro o bloqueio via Sisbajud.")
        st, d, _ = c.req("GET", "/api/penhora")
        self.assertEqual(st, 200)
        self.assertEqual(len(d["itens"]), 1)
        self.assertEqual(d["itens"][0]["grau"], "critico")
        self.assertEqual(d["resumo"]["executado"]["critico"], 1)
        self.assertEqual(c.req("GET", "/api/penhora?posicao=exequente")[1]["itens"], [])
        self.assertEqual(c.req("GET", "/api/penhora?q=devedor")[1]["itens"][0]["numero"], "0000001-00.2024.8.19.0001")
        self.assertEqual(c.req("GET", f"/api/processos/{self.pid}/penhora?posicao=exequente")[1]["posicao"], "exequente")
        self.assertEqual(c.req("GET", "/api/processos/999/penhora")[0], 404)
        self.assertEqual(c.req("POST", "/api/penhora/avisar", {})[0], 403)
        self.assertEqual(c.req("GET", "/api/dashboard")[1]["penhora"]["n_executado"], 1)
        self.assertEqual(c.req("POST", f"/api/processos/{self.pid}/posicao", {"posicao": ""})[0], 200)
        self.assertEqual(c.req("GET", "/api/penhora")[1]["itens"], [])

    def test_aviso_so_quando_sobe(self):
        env = []
        cfg = {"email": {"ativo": True, "smtp_host": "x", "usuario": "u", "senha": "s", "destinatario": "geral@exemplo.com"}, "prazos": {}}
        enviar = lambda em, a, c: env.append((em["destinatario"], a, c))  # noqa: E731
        risco.definir_posicao(self.b, self.pid, "executado")
        self.pub("2026-11-10", "Intime-se o executado para pagar (art. 523).")
        self.assertEqual(risco.avisar(self.b, cfg, enviar, "2026-11-24", intervalo=0)["enviados"], 0)   # médio: não avisa
        self.pub("2026-11-20", "Requer o exequente a penhora online via SISBAJUD.")
        self.assertEqual(risco.avisar(self.b, cfg, enviar, "2026-11-24", intervalo=0)["enviados"], 1)   # alto
        self.assertIn("Risco de penhora alto", env[0][1])
        self.assertIn("art. 854", env[0][2])
        self.assertEqual(risco.avisar(self.b, cfg, enviar, "2026-11-24", intervalo=0)["enviados"], 0)   # mesmo grau: não repete
        self.pub("2026-11-22", "Defiro o bloqueio via Sisbajud.")
        self.assertEqual(risco.avisar(self.b, cfg, enviar, "2026-11-24", intervalo=0)["enviados"], 1)   # subiu para crítico
        self.mov("2026-11-23", "Desbloqueio de valores")
        self.pub("2026-11-23", "Acordo homologado e suspensão do feito (art. 922).")
        risco.avisar(self.b, cfg, enviar, "2026-11-24", intervalo=0)
        self.pub("2026-11-24", "Retomada a execução. Defiro novo bloqueio via Sisbajud.")
        self.assertEqual(risco.avisar(self.b, cfg, enviar, "2026-11-24", intervalo=0)["enviados"], 1)   # voltou a subir
        cfg["email"]["ativo"] = False
        self.assertEqual(risco.avisar(self.b, cfg, enviar, intervalo=0)["motivo"], "e-mail desligado")

    def test_remover_processo_leva_estado(self):
        risco.definir_posicao(self.b, self.pid, "executado")
        risco.avisar(self.b, {"email": {"ativo": True, "destinatario": "a@b.com", "smtp_host": "x"}, "prazos": {}}, lambda *a: None, intervalo=0)
        self.b.remover(self.pid)
        self.assertEqual(self.b.conn.execute("SELECT COUNT(*) FROM risco_estado").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
