"""Testes da v0.7.0: agenda (tipos e eventos editáveis, filtros, conflito de edição, .ics, resumo no Início, calendário de feriados)."""
import datetime as dt
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import agenda  # noqa: E402
import app  # noqa: E402
from test_equipe import Base  # noqa: E402

HOJE = dt.date.today()


def dia(n):
    return (HOJE + dt.timedelta(days=n)).isoformat()


class Agenda(Base):
    def setUp(self):
        super().setUp()
        self.adm = self.admin()
        self.maria = self.comum(self.adm)
        self.b = app.CTX.banco
        self.pid = self.b.adicionar("0000001-00.2024.8.19.0001", "00000010020248190001", "tjrj", apelido="Caso Vivae", cliente="Vivae")
        self.tipos = {t["nome"]: t["id"] for t in self.adm.req("GET", "/api/tipos-evento")[1]}
        self.uid_maria = next(u["id"] for u in self.adm.req("GET", "/api/equipe")[1] if u["nome"] == "Maria")

    def novo_ev(self, cli=None, **kw):
        c = {"titulo": "Contestação", "tipo_id": self.tipos["Prazo fatal"], "data": dia(3), "processo_id": self.pid}
        c.update(kw)
        st, d, _ = (cli or self.maria).req("POST", "/api/eventos", c)
        self.assertEqual(st, 200, d)
        return d["id"]

    # ---- tipos
    def test_tipos_padrao_e_crud(self):
        self.assertEqual(set(self.tipos), {"Audiência", "Prazo fatal", "Prazo interno", "Diligência / tarefa", "Perícia",
                                           "Reunião / atendimento", "Outro"})
        st, d, _ = self.adm.req("POST", "/api/tipos-evento", {"nome": "Mediação", "cor": "#112233"})
        self.assertEqual(st, 200)
        tid = d["id"]
        self.assertEqual(self.adm.req("POST", "/api/tipos-evento", {"nome": "mediação", "cor": "#112233"})[0], 400)   # nome repetido (sem distinguir caixa)
        self.assertEqual(self.adm.req("POST", "/api/tipos-evento", {"nome": "X", "cor": "#112233"})[0], 400)
        self.assertEqual(self.adm.req("POST", "/api/tipos-evento", {"nome": "Cor ruim", "cor": "vermelho"})[0], 400)
        self.assertEqual(self.adm.req("POST", f"/api/tipos-evento/{tid}", {"nome": "Mediação/Conciliação"})[0], 200)
        self.assertEqual(self.adm.req("DELETE", f"/api/tipos-evento/{tid}")[0], 200)                                # sem uso: exclui
        self.assertEqual(len(self.adm.req("GET", "/api/tipos-evento")[1]), 7)

    def test_tipo_em_uso_nao_exclui_mas_desativa(self):
        self.novo_ev()
        t = self.tipos["Prazo fatal"]
        st, d, _ = self.adm.req("DELETE", f"/api/tipos-evento/{t}")
        self.assertEqual(st, 400)
        self.assertIn("Desative", d["erro"])
        self.assertEqual(self.adm.req("POST", f"/api/tipos-evento/{t}", {"ativo": False})[0], 200)
        self.assertEqual(self.maria.req("POST", "/api/eventos", {"titulo": "Novo", "tipo_id": t, "data": dia(1)})[0], 400)   # desativado: não serve p/ novos
        ev = self.adm.req("GET", "/api/eventos")[1][0]
        self.assertEqual(ev["tipo_nome"], "Prazo fatal")                                                                   # o antigo continua

    def test_tipos_so_admin_altera(self):
        self.assertEqual(self.maria.req("POST", "/api/tipos-evento", {"nome": "Mediação", "cor": "#112233"})[0], 403)
        self.assertEqual(self.maria.req("DELETE", f"/api/tipos-evento/{self.tipos['Outro']}")[0], 403)
        self.assertEqual(self.maria.req("GET", "/api/tipos-evento")[0], 200)

    def test_nao_deixa_zerar_tipos_ativos(self):
        ids = list(self.tipos.values())
        for i in ids[:-1]:
            self.assertEqual(self.adm.req("POST", f"/api/tipos-evento/{i}", {"ativo": False})[0], 200)
        self.assertEqual(self.adm.req("POST", f"/api/tipos-evento/{ids[-1]}", {"ativo": False})[0], 400)

    # ---- eventos
    def test_criar_e_validacoes(self):
        eid = self.novo_ev(descricao="Juntar procuração e documentos", hora="14:30", local="Sala 3", responsavel_id=self.uid_maria, lembretes=[7, 1, 0])
        e = self.maria.req("GET", f"/api/eventos/{eid}")[1]
        self.assertEqual((e["titulo"], e["data"], e["hora"], e["situacao"], e["versao"]), ("Contestação", dia(3), "14:30", "aberto", 1))
        self.assertEqual((e["responsavel_nome"], e["processo_apelido"], e["tipo_nome"], e["lembretes"]), ("Maria", "Caso Vivae", "Prazo fatal", [7, 1, 0]))
        self.assertEqual(e["criado_por"], "Maria")
        self.assertEqual(e["log"][0]["acao"], "criado")
        ruim = [{"titulo": "x"}, {"titulo": "Ok", "tipo_id": self.tipos["Outro"], "data": "31/12/2026"},
                {"titulo": "Ok", "tipo_id": self.tipos["Outro"], "data": dia(1), "hora": "25:00"},
                {"titulo": "Ok", "tipo_id": 9999, "data": dia(1)},
                {"titulo": "Ok", "tipo_id": self.tipos["Outro"], "data": dia(1), "processo_id": 9999},
                {"titulo": "Ok", "tipo_id": self.tipos["Outro"], "data": dia(1), "responsavel_id": 9999},
                {"titulo": "Ok", "tipo_id": self.tipos["Outro"], "data": dia(1), "lembretes": [4]},
                {"titulo": "Ok", "tipo_id": self.tipos["Outro"], "data": dia(1), "descricao": "x" * 4001}]
        for c in ruim:
            self.assertEqual(self.maria.req("POST", "/api/eventos", c)[0], 400, c)
        self.assertEqual(self.novo().req("POST", "/api/eventos", {"titulo": "Ok"})[0], 401)

    def test_evento_sem_processo(self):
        eid = self.novo_ev(processo_id=None, titulo="Reunião de equipe", tipo_id=self.tipos["Reunião / atendimento"])
        self.assertIsNone(self.maria.req("GET", f"/api/eventos/{eid}")[1]["processo_id"])

    def test_editar_teor_tipo_e_data_com_log(self):
        eid = self.novo_ev()
        v = self.maria.req("GET", f"/api/eventos/{eid}")[1]["versao"]
        st, d, _ = self.maria.req("POST", f"/api/eventos/{eid}", {"versao": v, "descricao": "Preparar réplica", "tipo_id": self.tipos["Prazo interno"], "data": dia(5)})
        self.assertEqual((st, d["versao"]), (200, v + 1))
        e = self.adm.req("GET", f"/api/eventos/{eid}")[1]
        self.assertEqual((e["descricao"], e["tipo_nome"], e["data"]), ("Preparar réplica", "Prazo interno", dia(5)))
        log = e["log"][0]
        self.assertEqual(log["acao"], "editado")
        self.assertIn("Prazo fatal → Prazo interno", log["detalhe"])
        self.assertIn(f"{dia(3)} → {dia(5)}", log["detalhe"])
        self.assertIn("teor alterado", log["detalhe"])
        self.assertEqual(e["atualizado_por"], "Maria")

    def test_conflito_de_edicao_nao_sobrescreve(self):
        eid = self.novo_ev()
        v = self.maria.req("GET", f"/api/eventos/{eid}")[1]["versao"]
        self.assertEqual(self.adm.req("POST", f"/api/eventos/{eid}", {"versao": v, "titulo": "Feito pelo chefe"})[0], 200)
        st, d, _ = self.maria.req("POST", f"/api/eventos/{eid}", {"versao": v, "titulo": "Feito pela Maria"})   # ainda com a versão antiga
        self.assertEqual(st, 409)
        self.assertIn("Chefe", d["erro"])
        self.assertEqual(self.adm.req("GET", f"/api/eventos/{eid}")[1]["titulo"], "Feito pelo chefe")
        self.assertEqual(self.maria.req("POST", f"/api/eventos/{eid}", {"titulo": "sem versão"})[0], 400)

    def test_concluir_reabrir_cancelar(self):
        eid = self.novo_ev(data=dia(-2))
        self.assertTrue(self.maria.req("GET", f"/api/eventos/{eid}")[1]["atrasado"])
        self.assertEqual(self.adm.req("POST", f"/api/eventos/{eid}/situacao", {"situacao": "concluido", "obs": "Protocolado"})[0], 200)
        e = self.maria.req("GET", f"/api/eventos/{eid}")[1]
        self.assertEqual((e["situacao"], e["concluido_por"], e["conclusao_obs"], e["atrasado"]), ("concluido", "Chefe", "Protocolado", False))
        self.assertEqual(self.adm.req("POST", f"/api/eventos/{eid}/situacao", {"situacao": "aberto"})[0], 200)
        e = self.maria.req("GET", f"/api/eventos/{eid}")[1]
        self.assertEqual((e["situacao"], e["concluido_por"]), ("aberto", ""))
        self.assertEqual(self.maria.req("POST", f"/api/eventos/{eid}/situacao", {"situacao": "cancelado", "obs": "Duplicado"})[0], 200)
        self.assertEqual([x["acao"] for x in self.maria.req("GET", f"/api/eventos/{eid}")[1]["log"]], ["cancelado", "reaberto", "concluído", "criado"])
        self.assertEqual(self.maria.req("POST", f"/api/eventos/{eid}/situacao", {"situacao": "feito"})[0], 400)

    def test_excluir_so_admin_ou_criador(self):
        eid = self.novo_ev(cli=self.adm)                                       # criado pelo chefe
        st, d, _ = self.maria.req("DELETE", f"/api/eventos/{eid}")
        self.assertEqual(st, 403)
        self.assertIn("cancelá-lo", d["erro"])
        eid2 = self.novo_ev(cli=self.maria)                                    # criado pela Maria
        self.assertEqual(self.maria.req("DELETE", f"/api/eventos/{eid2}")[0], 200)
        self.assertEqual(self.adm.req("DELETE", f"/api/eventos/{eid}")[0], 200)  # admin exclui qualquer um
        self.assertEqual(self.adm.req("GET", f"/api/eventos/{eid}")[0], 404)
        self.assertIn("evento_excluido", json.dumps(self.adm.req("GET", "/api/auditoria")[1]))

    def test_remover_processo_leva_os_eventos(self):
        eid = self.novo_ev()
        self.novo_ev(processo_id=None, titulo="Sem processo")
        self.assertEqual(self.adm.req("DELETE", f"/api/processos/{self.pid}")[0], 200)
        self.assertEqual(self.adm.req("GET", f"/api/eventos/{eid}")[0], 404)
        self.assertEqual([e["titulo"] for e in self.adm.req("GET", "/api/eventos")[1]], ["Sem processo"])

    # ---- listagem e filtros
    def test_filtros(self):
        p2 = self.b.adicionar("0000002-00.2024.8.19.0002", "00000020020248190002", "trt1", apelido="Caso Paulo")
        a = self.novo_ev(titulo="Ev A", data=dia(1), responsavel_id=self.uid_maria)
        b2 = self.novo_ev(titulo="Ev B", data=dia(10), tipo_id=self.tipos["Audiência"], processo_id=p2)
        c = self.novo_ev(titulo="Ev C", data=dia(20), processo_id=None, tipo_id=self.tipos["Outro"])
        self.adm.req("POST", f"/api/eventos/{c}/situacao", {"situacao": "cancelado"})
        d = self.novo_ev(titulo="Ev D", data=dia(2), tipo_id=self.tipos["Outro"])
        self.adm.req("POST", f"/api/eventos/{d}/situacao", {"situacao": "concluido"})
        titulos = lambda qs: [e["titulo"] for e in self.adm.req("GET", "/api/eventos" + qs)[1]]  # noqa: E731
        self.assertEqual(titulos(""), ["Ev A", "Ev D", "Ev B"])                                       # padrão: sem cancelados, em ordem de data
        self.assertEqual(titulos("?situacao=todos"), ["Ev A", "Ev D", "Ev B", "Ev C"])
        self.assertEqual(titulos("?situacao=aberto"), ["Ev A", "Ev B"])
        self.assertEqual(titulos("?situacao=concluido"), ["Ev D"])
        self.assertEqual(titulos("?situacao=cancelado"), ["Ev C"])
        self.assertEqual(titulos(f"?processo={p2}"), ["Ev B"])
        self.assertEqual(titulos(f"?tipo={self.tipos['Audiência']},{self.tipos['Outro']}"), ["Ev D", "Ev B"])
        self.assertEqual(titulos(f"?resp={self.uid_maria}"), ["Ev A"])
        self.assertEqual(titulos("?resp=sem"), ["Ev D", "Ev B"])
        self.assertEqual(titulos(f"?resp=sem,{self.uid_maria}"), ["Ev A", "Ev D", "Ev B"])
        self.assertEqual(titulos(f"?de={dia(2)}&ate={dia(10)}"), ["Ev D", "Ev B"])
        self.assertEqual(titulos("?q=paulo"), ["Ev B"])                                          # busca também no apelido do processo
        self.assertEqual(self.adm.req("GET", "/api/eventos?de=lixo")[0], 400)
        self.assertEqual(titulos("?tipo=abc&resp=x'--"), ["Ev A", "Ev D", "Ev B"])                      # lixo ignorado, sem erro

    # ---- .ics
    def test_ics(self):
        self.novo_ev(titulo="Audiência; de, instrução", tipo_id=self.tipos["Audiência"], hora="09:30", local="Fórum, sala 5",
                     descricao="Levar\ntestemunhas " + "x" * 200)
        self.novo_ev(titulo="Sem hora", data=dia(4))
        st, corpo, h = self.maria.req("GET", "/api/eventos.ics")
        self.assertEqual(st, 200)
        self.assertIn("text/calendar", h["Content-Type"])
        self.assertTrue(corpo.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(corpo.endswith("END:VCALENDAR\r\n"))
        self.assertEqual(corpo.count("BEGIN:VEVENT"), 2)
        self.assertIn("DTSTART:" + dia(3).replace("-", "") + "T093000", corpo)
        self.assertIn("DTSTART;VALUE=DATE:" + dia(4).replace("-", ""), corpo)
        self.assertIn("SUMMARY:[Audiência] Audiência\\; de\\, instrução", corpo)
        self.assertIn("LOCATION:Fórum\\, sala 5", corpo)
        for linha in corpo.split("\r\n"):
            self.assertLessEqual(len(linha.encode("utf-8")), 75)
        self.assertEqual(self.novo().req("GET", "/api/eventos.ics")[0], 401)
        st, corpo, _ = self.maria.req("GET", f"/api/eventos.ics?q=Sem+hora")
        self.assertEqual(corpo.count("BEGIN:VEVENT"), 1)

    # ---- início
    def test_resumo_no_inicio(self):
        self.novo_ev(titulo="Vencido", data=dia(-3), responsavel_id=self.uid_maria)
        self.novo_ev(titulo="Hoje", data=dia(0))
        self.novo_ev(titulo="Semana", data=dia(7))
        self.novo_ev(titulo="Longe", data=dia(8))
        x = self.novo_ev(titulo="Feito", data=dia(1))
        self.adm.req("POST", f"/api/eventos/{x}/situacao", {"situacao": "concluido"})
        a = self.adm.req("GET", "/api/dashboard")[1]["agenda"]
        self.assertEqual((a["vencidos"], a["hoje"], a["proximos7"]), (1, 1, 1))
        self.assertEqual([e["titulo"] for e in a["lista"]], ["Vencido", "Hoje", "Semana", "Longe"])
        self.assertTrue(a["lista"][0]["atrasado"])
        a2 = self.adm.req("GET", f"/api/dashboard?resp={self.uid_maria}")[1]["agenda"]
        self.assertEqual((a2["vencidos"], a2["hoje"], a2["proximos7"]), (1, 0, 0))
        a3 = self.adm.req("GET", "/api/dashboard?cliente=Outro")[1]["agenda"]
        self.assertEqual((a3["vencidos"], a3["hoje"], a3["proximos7"]), (0, 0, 0))

    # ---- calendário
    def test_dias_de_feriado_e_recesso(self):
        st, d, _ = self.maria.req("GET", "/api/calendario/dias?de=2026-11-01&ate=2027-01-31")
        self.assertEqual(st, 200)
        por = {x["dia"]: x for x in d}
        self.assertIn("Finados", por["2026-11-02"]["feriado"])
        self.assertIn("Consciência", por["2026-11-20"]["feriado"])
        self.assertTrue(por["2026-12-20"]["recesso"])
        self.assertTrue(por["2027-01-20"]["recesso"])
        self.assertNotIn("2027-01-21", por)
        self.assertNotIn("2026-11-03", por)
        self.assertEqual(self.maria.req("GET", "/api/calendario/dias?de=2026-01-01&ate=2028-01-01")[0], 400)
        self.assertEqual(self.maria.req("GET", "/api/calendario/dias?de=x&ate=y")[0], 400)

    def test_migracao_idempotente(self):
        agenda.migrar(self.b)
        agenda.migrar(self.b)
        self.assertEqual(len(agenda.tipos(self.b)), 7)


if __name__ == "__main__":
    unittest.main()


class TesteInterfaceAgenda(unittest.TestCase):
    def test_pagina_tem_calendario(self):
        h = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
        for t in ('data-t="calendario"', 'id="t-calendario"', 'id="d-evento"', 'id="d-tipos"', 'id="i-agenda"', "carregarCalendario", "Exportar .ics"):
            self.assertIn(t, h)
