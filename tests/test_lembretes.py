"""Testes da v0.7.2: resumo diário da agenda por e-mail, lembretes escolhidos por evento, memória de cálculo do prazo, VALARM no .ics."""
import datetime as dt
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import agenda  # noqa: E402
import app  # noqa: E402
import lembretes  # noqa: E402
import prazos  # noqa: E402
from test_equipe import Base  # noqa: E402

BASE = dt.date(2026, 11, 24)


def dia(n):
    return (BASE + dt.timedelta(days=n)).isoformat()


def as_(n, h=8):
    return dt.datetime.combine(BASE + dt.timedelta(days=n), dt.time(h, 0))


class Lembretes(Base):
    def setUp(self):
        super().setUp()
        lembretes._falhas.clear()
        self.adm = self.admin()
        self.maria = self.comum(self.adm)
        self.b = app.CTX.banco
        self.pid = self.b.adicionar("0000001-00.2024.8.19.0001", "00000010020248190001", "tjrj", apelido="Caso Vivae", cliente="Vivae")
        self.tipo = next(t["id"] for t in self.adm.req("GET", "/api/tipos-evento")[1] if t["nome"] == "Prazo fatal")
        self.uid_maria = next(u["id"] for u in self.adm.req("GET", "/api/equipe")[1] if u["nome"] == "Maria")
        self.cfg = {"email": {"ativo": True, "smtp_host": "x", "usuario": "u", "senha": "s", "destinatario": "geral@exemplo.com"},
                    "agenda": {"email_ativo": True, "hora": "07:00", "geral_recebe_tudo": True}}
        self.env = []

    def enviar(self, em, assunto, corpo):
        self.env.append((em["destinatario"], assunto, corpo))

    def rodar(self, quando, **kw):
        return lembretes.enviar_resumos(self.b, self.cfg, enviar=self.enviar, agora=quando, **kw)

    def ev(self, data, lemb=(7, 3, 1, 0), resp=None, **kw):
        c = {"titulo": "Contestação", "tipo_id": self.tipo, "data": data, "processo_id": self.pid, "lembretes": list(lemb), "responsavel_id": resp}
        c.update(kw)
        st, d, _ = self.adm.req("POST", "/api/eventos", c)
        self.assertEqual(st, 200, d)
        return d["id"]

    def test_lembretes_na_data_certa_sem_repetir(self):
        self.ev(dia(3))   # lembretes 7, 3, 1, 0
        self.assertEqual(self.rodar(as_(0, 6))["motivo"], "antes do horário")
        r = self.rodar(as_(0))   # 7 e 3 dias já devidos: um e-mail só
        self.assertEqual(r["enviados"], 1)
        self.assertEqual(len(self.env), 1)
        self.assertIn("em 3 dias", self.env[0][2])
        self.assertEqual(self.rodar(as_(0, 15))["enviados"], 0)   # no mesmo dia, nada novo
        self.assertEqual(self.rodar(as_(1))["enviados"], 0)       # dia 1: faltam 2 dias, sem lembrete previsto
        self.assertEqual(self.rodar(as_(2))["enviados"], 1)       # falta 1 dia
        self.assertEqual(self.rodar(as_(3))["enviados"], 1)       # no dia
        self.assertIn("HOJE", self.env[-1][2])
        self.assertEqual(self.rodar(as_(3, 18))["enviados"], 0)

    def test_pc_desligado_junta_tudo_e_vencidos_uma_vez_por_dia(self):
        self.ev(dia(2), lemb=(3, 1, 0))
        r = self.rodar(as_(2))   # primeiro contato já no dia do prazo: um e-mail com o que ficou devido
        self.assertEqual(r["enviados"], 1)
        self.assertEqual(self.rodar(as_(3))["enviados"], 1)   # venceu ontem: aparece em VENCIDOS
        self.assertIn("VENCIDOS", self.env[-1][2])
        self.assertIn("há 1 dia", self.env[-1][2])
        self.assertEqual(self.rodar(as_(3, 17))["enviados"], 0)   # no mesmo dia, não repete
        self.assertEqual(self.rodar(as_(4))["enviados"], 1)       # no dia seguinte, de novo, até concluir
        eid = self.b.conn.execute("SELECT id FROM eventos").fetchone()[0]
        self.assertEqual(self.adm.req("POST", f"/api/eventos/{eid}/situacao", {"situacao": "concluido"})[0], 200)
        self.assertEqual(self.rodar(as_(5))["enviados"], 0)

    def test_roteamento_responsavel_e_geral(self):
        self.ev(dia(0), lemb=(0,), resp=self.uid_maria)
        self.ev(dia(0), lemb=(0,), titulo="Sem resp")
        self.rodar(as_(0))
        por = {d: c for d, _, c in self.env}
        self.assertEqual(set(por), {"m@exemplo.com", "geral@exemplo.com"})
        self.assertIn("Contestação", por["m@exemplo.com"])
        self.assertNotIn("Sem resp", por["m@exemplo.com"])
        self.assertIn("Sem resp", por["geral@exemplo.com"])
        self.assertIn("Contestação", por["geral@exemplo.com"])   # geral recebe tudo
        self.env.clear()
        self.cfg["agenda"]["geral_recebe_tudo"] = False
        self.ev(dia(1), lemb=(1,), resp=self.uid_maria, titulo="Só da Maria")
        self.rodar(as_(0, 9))
        self.assertEqual([d for d, _, _ in self.env], ["m@exemplo.com"])

    def test_falha_nao_marca_e_nova_data_reabilita(self):
        eid = self.ev(dia(1), lemb=(1,))

        def falha(em, a, c):
            raise OSError("sem rede")
        self.assertEqual(lembretes.enviar_resumos(self.b, self.cfg, enviar=falha, agora=as_(0))["erros"], 1)
        self.assertEqual(self.rodar(as_(0, 9))["enviados"], 0)          # espera após a falha
        self.assertEqual(self.rodar(as_(0, 9), forcar=True)["enviados"], 1)   # com "enviar agora" tenta de novo
        self.assertEqual(self.rodar(as_(0, 10))["enviados"], 0)
        v = self.adm.req("GET", f"/api/eventos/{eid}")[1]["versao"]
        self.assertEqual(self.adm.req("POST", f"/api/eventos/{eid}", {"data": dia(2), "versao": v})[0], 200)
        self.assertEqual(self.rodar(as_(1))["enviados"], 1)             # nova data: lembrete de 1 dia volta a valer

    def test_desligado_e_permissao(self):
        self.ev(dia(0), lemb=(0,))
        self.cfg["agenda"]["email_ativo"] = False
        self.assertEqual(self.rodar(as_(0))["motivo"], "desligado")
        self.assertEqual(self.maria.req("POST", "/api/agenda/enviar-resumo", {})[0], 403)
        st, d, _ = self.adm.req("POST", "/api/agenda/enviar-resumo", {})
        self.assertEqual(st, 200)
        self.assertEqual(d["motivo"], "e-mail não configurado")

    def test_calculo_gravado_e_validado(self):
        r = prazos.calcular(dt.date(2026, 11, 19), 15, prazos.Calendario())
        st, d, _ = self.maria.req("POST", "/api/eventos", {"titulo": "Contestação", "tipo_id": self.tipo, "data": r["vencimento"], "processo_id": self.pid,
                                                           "calculo": {k: r[k] for k in ("disponibilizacao", "publicacao", "inicio", "vencimento", "dias", "memoria", "aviso")}})
        self.assertEqual(st, 200, d)
        e = self.maria.req("GET", f"/api/eventos/{d['id']}")[1]
        self.assertEqual(e["calculo"]["vencimento"], r["vencimento"])
        self.assertEqual(e["origem"], "prazo_calculado")
        self.assertTrue(e["calculo"]["memoria"])
        # a data gravada vale: o usuário pode mudá-la e a memória continua guardada
        self.assertEqual(self.maria.req("POST", f"/api/eventos/{d['id']}", {"data": dia(40), "versao": e["versao"]})[0], 200)
        e2 = self.maria.req("GET", f"/api/eventos/{d['id']}")[1]
        self.assertEqual(e2["data"], dia(40))
        self.assertEqual(e2["calculo"]["vencimento"], r["vencimento"])
        self.assertEqual(self.maria.req("POST", "/api/eventos", {"titulo": "Ruim", "tipo_id": self.tipo, "data": dia(1), "calculo": {"dias": "x"}})[0], 400)
        self.assertEqual(self.maria.req("POST", "/api/eventos", {"titulo": "Ruim", "tipo_id": self.tipo, "data": dia(1), "calculo": "lixo"})[0], 400)
        self.assertEqual(self.maria.req("POST", "/api/eventos", {"titulo": "Sem", "tipo_id": self.tipo, "data": dia(1)})[0], 200)

    def test_ics_com_alarmes(self):
        self.ev(dia(5), lemb=(3, 0))
        self.ev(dia(5), lemb=(1,), hora="14:00", titulo="Audiência")
        st, txt, _ = self.adm.req("GET", "/api/eventos.ics")
        self.assertEqual(st, 200)
        self.assertIn("BEGIN:VALARM", txt)
        self.assertIn("TRIGGER:-PT63H", txt)   # 3 dias antes, 9h (evento de dia inteiro)
        self.assertIn("TRIGGER:PT9H", txt)
        self.assertIn("TRIGGER:-P1D", txt)     # evento com hora: mesmo horário, 1 dia antes

    def test_config_agenda(self):
        self.assertEqual(self.adm.req("POST", "/api/config", {"agenda": {"hora": "7h"}})[0], 400)
        self.assertEqual(self.adm.req("POST", "/api/config", {"agenda": {"hora": "06:30", "geral_recebe_tudo": False}})[0], 200)
        d = self.adm.req("GET", "/api/config")[1]
        self.assertEqual(d["agenda"]["hora"], "06:30")
        self.assertFalse(d["agenda"]["geral_recebe_tudo"])


if __name__ == "__main__":
    unittest.main()
