"""Testes da v0.6: agrupamento de publicações repetidas, situação compartilhada, responsável, e-mail ao responsável e cálculo de prazo."""
import datetime as dt
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import agrupar  # noqa: E402
import app  # noqa: E402
import dje  # noqa: E402
import mailer  # noqa: E402
import prazos  # noqa: E402
from config import Config  # noqa: E402
from db import Banco  # noqa: E402
from monitor import Monitor  # noqa: E402
from test_equipe import Base  # noqa: E402

NUM = "0021000-84.2003.5.01.0006"
LIMPO = "00210008420035010006"
CORPO = ("INTIMACAO Fica V. Sa. intimado para tomar ciencia da Decisao ID c422efc proferida nos autos. DECISAO Vistos, etc. "
         "Mantenho a decisao agravada, por seus proprios fundamentos. RIO DE JANEIRO/RJ, 18 de novembro de 2026.")


def texto(intimados, cabecalho="", n=1):
    return f"{cabecalho}Processo: {NUM} Conteudo: {CORPO} Intimado(s) / Citado(s) - {intimados} |comunicacao_id: {n}|"


def api(id_, tx, data="2026-11-19", **kw):
    d = {"id": id_, "numero_processo": LIMPO, "texto": tx, "data_disponibilizacao": data, "tipoComunicacao": "Intimação",
         "nomeOrgao": "CAEX REEF", "tipoDocumento": "Despacho", "siglaTribunal": "TRT1",
         "destinatarios": [{"nome": "PARTE " + str(id_), "polo": "A"}]}
    d.update(kw)
    return dje.normalizar_item(d)


class Agrupamento(unittest.TestCase):
    def test_copias_que_diferem_so_nos_intimados_tem_a_mesma_chave(self):
        a = api(1, texto("ALFA LTDA - BETA SA", n=1))
        b = api(2, texto("GAMA - ME - DELTA", n=2))
        self.assertEqual(agrupar.chave(7, a), agrupar.chave(7, b))
        self.assertTrue(agrupar.chave(7, a).startswith("g:"))

    def test_prefixo_do_djen_e_espacos_nao_quebram_a_igualdade(self):
        a = api(1, texto("ALFA"))
        b = api(2, texto("BETA", cabecalho="DIARIO DE JUSTICA ELETRONICO NACIONAL (DJEN) ").replace("Fica V. Sa.", "Fica  V. Sa.\n"))
        self.assertEqual(agrupar.chave(7, a), agrupar.chave(7, b))

    def test_texto_parecido_nao_agrupa(self):
        a = api(1, texto("ALFA"))
        b = api(2, texto("ALFA").replace("Mantenho", "Reformo"))
        c = api(3, texto("ALFA").replace("Decisao", "decisao"))        # só maiúscula: também não
        self.assertNotEqual(agrupar.chave(7, a), agrupar.chave(7, b))
        self.assertNotEqual(agrupar.chave(7, a), agrupar.chave(7, c))

    def test_outros_campos_diferentes_nao_agrupam(self):
        base = api(1, texto("ALFA"))
        for outro in (api(2, texto("ALFA"), data="2026-11-20"), api(2, texto("ALFA"), nomeOrgao="OUTRO ORGAO"),
                      api(2, texto("ALFA"), tipoComunicacao="Edital"), api(2, texto("ALFA"), tipoDocumento="Decisão"),
                      api(2, texto("ALFA"), ativo=False)):
            self.assertNotEqual(agrupar.chave(7, base), agrupar.chave(7, outro))
        self.assertNotEqual(agrupar.chave(7, base), agrupar.chave(8, base))   # outro processo

    def test_texto_curto_nunca_agrupa(self):
        a, b = api(1, "Intimacao. Intimado(s) / Citado(s) - A"), api(2, "Intimacao. Intimado(s) / Citado(s) - B")
        self.assertEqual(agrupar.chave(7, a), "u:1")
        self.assertEqual(agrupar.chave(7, b), "u:2")

    def test_intimados_do_texto_nao_tenta_separar_nomes_com_hifen(self):
        t = texto("FX ENGENHARIA E CONSTRUCOES LTDA - ME - MX - ARTE, CRIACAO LTDA")
        self.assertEqual(agrupar.intimados_do_texto(t), "FX ENGENHARIA E CONSTRUCOES LTDA - ME - MX - ARTE, CRIACAO LTDA")


class BancoV06(Base):
    def setUp(self):
        super().setUp()
        self.b = app.CTX.banco
        self.pid = self.b.adicionar(NUM, LIMPO, "trt1")

    def reg(self, *itens):
        return self.b.registrar_publicacoes(self.pid, list(itens))

    def test_copias_viram_um_grupo_um_alerta_e_um_contador(self):
        novas = self.reg(api(1, texto("ALFA", n=1)), api(2, texto("BETA", n=2)))
        self.assertEqual(len(novas), 1)               # um alerta só
        self.assertEqual(novas[0]["copias"], 2)       # ... que informa as 2 cópias
        pubs = self.b.publicacoes()
        self.assertEqual(len(pubs), 1)
        self.assertEqual((pubs[0]["copias"], len(pubs[0]["ids"])), (2, 2))
        self.assertEqual(sorted(pubs[0]["intimados"]), ["ALFA", "BETA"])
        self.assertEqual({d["nome"] for d in pubs[0]["destinatarios_uniao"]}, {"PARTE 1", "PARTE 2"})
        p = self.b.listar()[0]
        self.assertEqual((p["pub_total"], p["pub_novas"]), (1, 1))
        self.assertEqual(self.b.total_pub_novas(), 1)

    def test_textos_diferentes_continuam_separados(self):
        novas = self.reg(api(1, texto("ALFA")), api(2, texto("ALFA").replace("Mantenho", "Reformo")))
        self.assertEqual(len(novas), 2)
        self.assertEqual(len(self.b.publicacoes()), 2)

    def test_situacao_vale_para_o_grupo_e_guarda_quem_e_quando(self):
        self.reg(api(1, texto("ALFA", n=1)), api(2, texto("BETA", n=2)))
        g = self.b.publicacoes()[0]["grupo"]
        self.assertEqual(self.b.definir_situacao(self.pid, g, "analise", "Maria"), 2)
        pub = self.b.publicacoes()[0]
        self.assertEqual((pub["situacao"], pub["situacao_por"], pub["visto"]), ("analise", "Maria", 1))
        self.assertTrue(pub["situacao_em"])
        self.assertEqual(self.b.listar()[0]["pub_novas"], 0)
        self.assertEqual(self.b.listar()[0]["pub_analise"], 1)
        self.b.definir_situacao(self.pid, g, "tratada", "João")
        self.assertEqual(self.b.publicacoes(situacao="tratada")[0]["situacao_por"], "João")
        self.assertEqual(self.b.publicacoes(situacao="nova"), [])
        self.b.definir_situacao(self.pid, g, "nova", "João")        # pode voltar a "nova"
        self.assertEqual(self.b.total_pub_novas(), 1)
        with self.assertRaises(ValueError):
            self.b.definir_situacao(self.pid, g, "qualquer", "x")

    def test_copia_que_chega_depois_herda_a_situacao_e_nao_alerta(self):
        self.reg(api(1, texto("ALFA", n=1)))
        self.b.definir_situacao(self.pid, self.b.publicacoes()[0]["grupo"], "tratada", "Maria")
        novas = self.reg(api(1, texto("ALFA", n=1)), api(2, texto("BETA", n=2)))
        self.assertEqual(novas, [])
        pub = self.b.publicacoes()[0]
        self.assertEqual((pub["copias"], pub["situacao"], pub["situacao_por"]), (2, "tratada", "Maria"))
        self.assertEqual(self.b.total_pub_novas(), 0)

    def test_marcar_vistas_passa_novas_para_analise_e_nao_para_tratada(self):
        self.reg(api(1, texto("ALFA")))
        self.b.marcar_pub_vistas(None, por="Maria")
        pub = self.b.publicacoes()[0]
        self.assertEqual((pub["situacao"], pub["situacao_por"]), ("analise", "Maria"))

    def test_historico_da_primeira_consulta_entra_como_tratada_de_origem_explicita(self):
        antiga = api(1, texto("ALFA"), data=(dt.date.today() - dt.timedelta(days=90)).isoformat())
        self.assertEqual(self.reg(antiga), [])
        pub = self.b.publicacoes()[0]
        self.assertEqual((pub["situacao"], pub["situacao_por"]), ("tratada", "(histórico)"))

    def test_banco_da_v05_e_migrado(self):
        caminho = Path(self.tmp.name) / "antigo.db"
        con = sqlite3.connect(caminho)
        con.executescript("""
        CREATE TABLE processos(id INTEGER PRIMARY KEY AUTOINCREMENT, numero TEXT NOT NULL UNIQUE, numero_limpo TEXT NOT NULL,
          alias TEXT NOT NULL, apelido TEXT DEFAULT '', cliente TEXT DEFAULT '', pasta TEXT DEFAULT '', ativo INTEGER NOT NULL DEFAULT 1,
          criado_em TEXT NOT NULL, ultima_consulta TEXT, status TEXT NOT NULL DEFAULT 'pendente', mensagem TEXT DEFAULT '',
          classe TEXT DEFAULT '', orgao TEXT DEFAULT '', ultima_atualizacao TEXT DEFAULT '');
        CREATE TABLE publicacoes(id INTEGER PRIMARY KEY AUTOINCREMENT, processo_id INTEGER NOT NULL, pub_id TEXT NOT NULL,
          data_disp TEXT DEFAULT '', tipo_com TEXT DEFAULT '', orgao TEXT DEFAULT '', tipo_doc TEXT DEFAULT '', classe TEXT DEFAULT '',
          texto TEXT DEFAULT '', meio TEXT DEFAULT '', hash TEXT DEFAULT '', destinatarios TEXT DEFAULT '[]', advogados TEXT DEFAULT '[]',
          cancelada INTEGER NOT NULL DEFAULT 0, visto INTEGER NOT NULL DEFAULT 0, detectado_em TEXT NOT NULL, UNIQUE(processo_id, pub_id));
        INSERT INTO processos(numero,numero_limpo,alias,criado_em) VALUES('%s','%s','trt1','2026-01-01');
        """ % (NUM, LIMPO))
        for i, (tx, visto) in enumerate([(texto("A"), 1), (texto("B"), 0), (texto("C").replace("Mantenho", "Reformo"), 1)], 1):
            con.execute("INSERT INTO publicacoes(processo_id,pub_id,data_disp,tipo_com,orgao,tipo_doc,texto,visto,detectado_em) "
                        "VALUES(1,?,?,?,?,?,?,?,'2026-11-19')", (str(i), "2026-11-19", "Intimação", "CAEX REEF", "Despacho", tx, visto))
        con.commit()
        con.close()
        b2 = Banco(caminho)
        linhas = b2._linhas("SELECT pub_id, situacao, situacao_por, visto, grupo FROM publicacoes ORDER BY id")
        # as cópias 1 e 2 formam um grupo; uma estava "vista" e a outra não: vale a mais avançada
        self.assertEqual(linhas[0]["grupo"], linhas[1]["grupo"])
        self.assertNotEqual(linhas[0]["grupo"], linhas[2]["grupo"])
        self.assertEqual([x["situacao"] for x in linhas], ["tratada", "tratada", "tratada"])
        self.assertEqual(linhas[2]["situacao_por"], "(antes da v0.6)")
        self.assertEqual(len(b2.publicacoes()), 2)
        Banco(caminho)   # abrir de novo não muda nada
        b2.conn.close()


class Rotas(Base):
    def setUp(self):
        super().setUp()
        self.adm = self.admin()
        self.maria = self.comum(self.adm)
        b = app.CTX.banco
        self.pid = b.adicionar(NUM, LIMPO, "trt1")
        b.registrar_publicacoes(self.pid, [api(1, texto("ALFA", n=1)), api(2, texto("BETA", n=2))])

    def test_situacao_pela_api_e_visivel_para_os_dois(self):
        pub = self.maria.req("GET", "/api/publicacoes")[1][0]
        self.assertEqual(pub["copias"], 2)
        st, d, _ = self.maria.req("POST", "/api/publicacoes/situacao",
                                  {"processo_id": pub["processo_id"], "grupo": pub["grupo"], "situacao": "analise"})
        self.assertEqual((st, d["alteradas"]), (200, 2))
        visto_pelo_admin = self.adm.req("GET", "/api/publicacoes?situacao=analise")[1]
        self.assertEqual(len(visto_pelo_admin), 1)
        self.assertEqual(visto_pelo_admin[0]["situacao_por"], "Maria")
        self.assertEqual(self.adm.req("GET", "/api/publicacoes?situacao=nova")[1], [])
        aud = self.adm.req("GET", "/api/auditoria")[1]
        self.assertIn("publicacao_situacao", json.dumps(aud))

    def test_botao_so_dje_nao_consulta_datajud(self):
        chamadas = {"dje": 0, "datajud": 0}
        m = app.CTX.monitor
        m.solicitar_dje = lambda ids: chamadas.__setitem__("dje", len(ids)) or len(ids)
        m.solicitar = lambda *a, **k: chamadas.__setitem__("datajud", 1) or 0
        m.solicitar_oab = lambda *a, **k: 0
        app.CTX.config.dados["dje_ativo"] = True
        st, d, _ = self.maria.req("POST", "/api/consultar-dje", {})
        self.assertEqual((st, d["enfileirados"]), (200, 1))
        self.assertEqual(chamadas, {"dje": 1, "datajud": 0})
        self.assertEqual(self.novo().req("POST", "/api/consultar-dje", {})[0], 401)
        app.CTX.config.dados["dje_ativo"] = False
        self.assertEqual(self.maria.req("POST", "/api/consultar-dje", {})[0], 400)
        self.assertIn("b-consultar-dje", (HERE.parent / "static" / "index.html").read_text(encoding="utf-8"))

    def test_situacao_invalida_e_sem_login(self):
        pub = self.adm.req("GET", "/api/publicacoes")[1][0]
        ok = {"processo_id": pub["processo_id"], "grupo": pub["grupo"]}
        self.assertEqual(self.adm.req("POST", "/api/publicacoes/situacao", {**ok, "situacao": "feito"})[0], 400)
        self.assertEqual(self.adm.req("POST", "/api/publicacoes/situacao", {"situacao": "nova"})[0], 400)
        self.assertEqual(self.novo().req("POST", "/api/publicacoes/situacao", {**ok, "situacao": "nova"})[0], 401)

    def test_responsavel(self):
        eq = self.maria.req("GET", "/api/equipe")[1]
        self.assertEqual({u["nome"] for u in eq}, {"Chefe", "Maria"})
        self.assertNotIn("email", json.dumps(eq))
        uid = next(u["id"] for u in eq if u["nome"] == "Maria")
        st, d, _ = self.adm.req("POST", "/api/processos/responsavel", {"ids": [self.pid], "responsavel_id": uid})
        self.assertEqual((st, d["alterados"]), (200, 1))
        p = self.maria.req("GET", "/api/processos")[1][0]
        self.assertEqual((p["responsavel_id"], p["responsavel_nome"]), (uid, "Maria"))
        self.assertEqual(self.adm.req("GET", "/api/publicacoes")[1][0]["responsavel_nome"], "Maria")
        self.assertEqual(self.adm.req("POST", "/api/processos/responsavel", {"ids": [self.pid], "responsavel_id": uid})[1]["alterados"], 0)
        self.assertEqual(self.adm.req("POST", "/api/processos/responsavel", {"ids": [self.pid], "responsavel_id": 9999})[0], 400)
        self.assertEqual(self.adm.req("POST", "/api/processos/responsavel", {"ids": [], "responsavel_id": uid})[0], 400)
        self.assertEqual(self.adm.req("POST", "/api/processos/responsavel", {"ids": [self.pid], "responsavel_id": None})[1]["alterados"], 1)
        self.assertIsNone(self.adm.req("GET", "/api/processos")[1][0]["responsavel_nome"])

    def test_usuario_desativado_nao_pode_ser_responsavel(self):
        uid = next(u["id"] for u in self.adm.req("GET", "/api/equipe")[1] if u["nome"] == "Maria")
        self.assertEqual(self.adm.req("POST", f"/api/usuarios/{uid}", {"ativo": False})[0], 200)
        self.assertEqual(self.adm.req("POST", "/api/processos/responsavel", {"ids": [self.pid], "responsavel_id": uid})[0], 400)
        self.assertNotIn("Maria", json.dumps(self.adm.req("GET", "/api/equipe")[1]))

    def test_prazo_pela_api(self):
        st, d, _ = self.maria.req("POST", "/api/prazo", {"data": "2026-11-19", "dias": 5})
        self.assertEqual(st, 200, d)
        self.assertEqual((d["publicacao"], d["inicio"], d["vencimento"]), ("2026-11-23", "2026-11-24", "2026-11-30"))
        self.assertIn("NÃO substitui", d["aviso"])
        self.assertTrue(any("20/11/2026" in m and "Consciência Negra" in m for m in d["memoria"]))
        for corpo in ({"data": "2026-11-19", "dias": 0}, {"data": "2026-11-19", "dias": "abc"}, {"data": "19/11/2026", "dias": 5},
                      {"dias": 5}, {"data": "2026-11-19", "dias": 401}, {"data": "2026-11-19", "dias": True}):
            self.assertEqual(self.maria.req("POST", "/api/prazo", corpo)[0], 400, corpo)
        self.assertEqual(self.novo().req("POST", "/api/prazo", {"data": "2026-11-19", "dias": 5})[0], 401)

    def test_prazo_usa_feriados_extras_e_recesso_da_configuracao(self):
        st, _, _ = self.adm.req("POST", "/api/config", {"prazos": {"feriados_extras": "2026-11-24 Feriado de teste",
                                                                   "recesso": {"ativo": True, "inicio": "12-20", "fim": "01-19"}}})
        self.assertEqual(st, 200)
        d = self.maria.req("POST", "/api/prazo", {"data": "2026-11-19", "dias": 1})[1]
        self.assertEqual(d["vencimento"], "2026-11-25")           # 24/11 virou feriado extra
        d = self.maria.req("POST", "/api/prazo", {"data": "2026-12-18", "dias": 1})[1]
        self.assertEqual(d["inicio"], "2027-01-20")               # recesso até 19/01 (configurado)
        for ruim in ({"feriados_extras": "isto não é data"}, {"recesso": {"inicio": "13-40"}}):
            self.assertEqual(self.adm.req("POST", "/api/config", {"prazos": ruim})[0], 400)
        self.assertEqual(self.maria.req("POST", "/api/config", {"prazos": {}})[0], 403)   # só administrador


class Prazos(unittest.TestCase):
    def d(self, s):
        return dt.date.fromisoformat(s)

    def test_caso_real_de_19_11_2026(self):
        # o Gojur mostrou "Data Publicação 23/11/2026" para disponibilização em 19/11/2026: 20/11 é feriado nacional (Lei 14.759/2023)
        r = prazos.calcular(self.d("2026-11-19"), 15)
        self.assertEqual((r["publicacao"], r["inicio"]), ("2026-11-23", "2026-11-24"))
        self.assertEqual(r["vencimento"], "2026-12-14")      # 24/11 ... 14/12 = 15 dias úteis
        self.assertEqual(self.d(r["vencimento"]).weekday(), 0)

    def test_sexta_disponibiliza_segunda_publica_terca_comeca(self):
        r = prazos.calcular(self.d("2026-11-06"), 5)          # sexta
        self.assertEqual((r["publicacao"], r["inicio"], r["vencimento"]), ("2026-11-09", "2026-11-10", "2026-11-16"))

    def test_feriados_nacionais_fixos(self):
        cal = prazos.Calendario()
        for md in ("2026-01-01", "2026-04-21", "2026-05-01", "2026-09-07", "2026-10-12", "2026-11-02", "2026-11-15", "2026-11-20", "2026-12-25"):
            self.assertIsNotNone(cal.feriado(self.d(md)), md)
        self.assertIsNone(cal.feriado(self.d("2026-02-17")))   # Carnaval não é feriado nacional por lei: só entra se cadastrado

    def test_recesso_20_12_a_20_01_inclusive(self):
        r = prazos.calcular(self.d("2026-12-18"), 1)           # publica 21/12 (dentro do recesso); contagem só depois de 20/01
        self.assertEqual((r["publicacao"], r["inicio"], r["vencimento"]), ("2026-12-21", "2027-01-21", "2027-01-21"))
        cal = prazos.Calendario(recesso={"ativo": False})
        self.assertEqual(prazos.calcular(self.d("2026-12-18"), 1, cal)["inicio"], "2026-12-22")
        self.assertTrue(prazos.Calendario().em_recesso(self.d("2027-01-20")))
        self.assertFalse(prazos.Calendario().em_recesso(self.d("2027-01-21")))
        self.assertFalse(prazos.Calendario().em_recesso(self.d("2026-12-19")))

    def test_prazo_atravessando_o_recesso_nao_conta_os_dias_suspensos(self):
        r = prazos.calcular(self.d("2026-12-10"), 10)          # publica 11/12; dias 14..18 (5), depois recesso; 21/01.. (5)
        self.assertEqual(r["inicio"], "2026-12-14")
        self.assertEqual(r["vencimento"], "2027-01-27")

    def test_entradas_invalidas(self):
        for dias in (0, -1, 401, "5", None, 2.5, True):
            with self.assertRaises(prazos.ErroPrazo):
                prazos.calcular(self.d("2026-11-19"), dias)
        with self.assertRaises(prazos.ErroPrazo):
            prazos.data_de_texto("ontem")

    def test_memoria_de_calculo_cita_a_base_legal(self):
        txt = " ".join(prazos.calcular(self.d("2026-11-19"), 5)["memoria"])
        for t in ("art. 224", "Lei 11.419/2006", "arts. 219", "art. 220"):
            self.assertIn(t, txt)


class EmailResponsavel(Base):
    def test_responsavel_recebe_so_o_dele_e_sem_duplicar(self):
        adm = self.admin()
        maria = self.comum(adm)
        b, mon = app.CTX.banco, app.CTX.monitor
        uid = next(u["id"] for u in adm.req("GET", "/api/equipe")[1] if u["nome"] == "Maria")
        p1 = b.adicionar(NUM, LIMPO, "trt1")
        p2 = b.adicionar("0000002-59.2023.8.19.0001", "00000025920238190001", "tjrj")
        b.definir_responsavel([p1], uid)
        novas = b.registrar_publicacoes(p1, [api(1, texto("A", n=1)), api(2, texto("B", n=2))])
        pubs = [(b.obter(p1), novas), (b.obter(p2), [api(9, texto("Z"))])]
        enviados = []
        with mock.patch.object(mailer, "enviar", side_effect=lambda cfg, ass, corpo: enviados.append((cfg["destinatario"], ass, corpo))):
            mon._avisar_responsaveis(pubs, {"ativo": True, "destinatario": "chefe@exemplo.com"})
            self.assertEqual([e[0] for e in enviados], ["m@exemplo.com"])
            self.assertIn("1 publicação(ões)", enviados[0][1])
            self.assertIn("2 cópias iguais", enviados[0][2])
            self.assertNotIn("0000002-59", enviados[0][2])           # processo de outro responsável não vai junto
            enviados.clear()
            mon._avisar_responsaveis(pubs, {"ativo": True, "destinatario": "x@y.com, M@exemplo.com"})   # já está na lista geral
            self.assertEqual(enviados, [])

    def test_falha_no_envio_ao_responsavel_nao_derruba(self):
        adm = self.admin()
        b, mon = app.CTX.banco, app.CTX.monitor
        uid = next(u["id"] for u in adm.req("GET", "/api/equipe")[1] if u["nome"] == "Chefe")
        p1 = b.adicionar(NUM, LIMPO, "trt1")
        b.definir_responsavel([p1], uid)
        with mock.patch.object(mailer, "enviar", side_effect=OSError("sem rede")):
            mon._avisar_responsaveis([(b.obter(p1), [api(1, texto("A"))])], {"ativo": True, "destinatario": "outro@y.com"})


class Pagina(unittest.TestCase):
    def test_pagina_tem_os_novos_controles(self):
        html = (HERE.parent / "static" / "index.html").read_text(encoding="utf-8")
        for tok in ("p-sit", "d-prazo", "dz-calc", "d-resp", "b-resp-lote", "z-extras", "/api/publicacoes/situacao", "/api/prazo", "/api/processos/responsavel"):
            self.assertIn(tok, html)
        self.assertNotIn("confirm(", html.replace("confirmar(", ""))


if __name__ == "__main__":
    unittest.main()


class TesteDjeIndependente(unittest.TestCase):
    """v0.6.2: o DJEN tem a sua própria conta diária e um botão próprio, sem passar pelo DataJud."""

    def _monitor(self):
        import tempfile
        from pathlib import Path
        import config as cfgmod
        import db as dbmod
        import monitor as mon
        d = Path(tempfile.mkdtemp())
        banco = dbmod.Banco(d / "p.db")
        cfg = cfgmod.Config(d / "config.json")
        m = mon.Monitor(banco, cfg)
        m._dormir = lambda s: None
        return banco, cfg, m

    def test_candidatos_e_bloco_so_dje(self):
        import datetime as dt
        banco, cfg, m = self._monitor()
        banco.adicionar("0000001-00.2024.8.19.0001", "00000010020248190001", "api_publica_tjrj")
        ativos = banco.ativos(so_monitorados=True)
        self.assertEqual(len(ativos), 1)
        pid = ativos[0]["id"]
        hoje = dt.datetime.now(dt.timezone.utc)
        # DataJud consultado hoje, DJEN nunca: tem de entrar só no DJEN
        banco.conn.execute("UPDATE processos SET ultima_consulta=?, dje_consulta=NULL WHERE id=?",
                           (hoje.strftime("%Y-%m-%dT%H:%M:%SZ"), pid))
        banco.conn.commit()
        self.assertNotIn(pid, m.candidatos_hoje())
        self.assertIn(pid, m.candidatos_dje_hoje())
        chamadas = []
        m.solicitar_dje = lambda ids: chamadas.append(set(ids)) or len(ids)
        m.solicitar = lambda ids=None, forcar=False: chamadas.append(("dj", set(ids or []))) or 0
        cfg.dados["varredura"] = {"ativa": True, "bloco": 10, "intervalo_min": 1, "inicio": "00:00"}
        m.rodando = False
        self.assertGreaterEqual(m.iniciar_bloco_se_devido(), 1)
        self.assertIn({pid}, chamadas)
        r = m.resumo_varredura()
        self.assertEqual(r["dje_feitos_hoje"], 0)
        self.assertEqual(r["dje_restantes"], 1)
