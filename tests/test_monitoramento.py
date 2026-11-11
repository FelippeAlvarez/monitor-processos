"""Testes da v0.5.2: monitoramento por processo (marcação + motivo) e OAB automática."""
import base64
import datetime as dt
import sqlite3
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import datajud  # noqa: E402
import mailer  # noqa: E402
from db import Banco  # noqa: E402
from test_monitor import ApiFalsa, Base, DjenFalso, MOV1, cnj, dias_atras, item_dje  # noqa: E402
from test_varredura import MANHA, esperar, limpo, num, xlsx  # noqa: E402

OUTRO_ADV = [{"id": 1, "advogado": {"id": 9, "nome": "OUTRO ADVOGADO", "numero_oab": "777777", "uf_oab": "RJ"}}]
NOSSO_ADV = [{"id": 2, "advogado": {"id": 8, "nome": "NOSSO ADVOGADO", "numero_oab": "123456", "uf_oab": "RJ"}}]


class Monitoramento(Base):
    def setUp(self):
        super().setUp()
        self.cfg.dados["varredura"].update({"ativa": True, "bloco": 10, "intervalo_min": 30, "inicio": "02:00"})
        ApiFalsa.estado["movs"] = [MOV1]
        self.oid = self.banco.adicionar_oab("123456", "RJ", "minha OAB")

    def add_nm(self, seq, monitorar=False, **kw):
        info = datajud.interpretar_numero(num(seq))
        return self.banco.adicionar(info["numero"], info["limpo"], "tjrj", monitorar=monitorar, **kw)

    def djen(self, seq, itens):
        DjenFalso.estado["itens"][limpo(seq)] = itens

    def pub(self, pid, dias, seq, advs=None, **kw):
        extra = dict(kw)
        if advs is not None:
            extra["destinatarioadvogados"] = advs
        return item_dje(pid, dias_atras(dias), limpo(seq), **extra)

    def triar(self, n=1):
        self.assertGreaterEqual(self.mon.iniciar_bloco_se_devido(MANHA), n)
        esperar(self.mon)


class Marcacao(Monitoramento):
    def test_padroes_e_motivo(self):
        a = self.add_nm(1, monitorar=True, motivo="estrategico", motivo_obs="cliente X")
        b = self.add_nm(2, monitorar=False, motivo="estrategico")      # motivo só vale para monitorados
        c = self.add_nm(3, monitorar=True, motivo="inventado")         # motivo desconhecido vira vazio
        pa, pb, pc = (self.banco.obter(i) for i in (a, b, c))
        self.assertEqual((pa["monitorar"], pa["motivo"], pa["motivo_obs"], pa["monitorar_origem"]), (1, "estrategico", "cliente X", "manual"))
        self.assertEqual((pb["monitorar"], pb["motivo"], pb["monitorar_origem"]), (0, "", ""))
        self.assertEqual((pc["monitorar"], pc["motivo"]), (1, ""))

    def test_banco_antigo_continua_monitorado(self):
        caminho = Path(self.tmp.name) / "antigo.db"
        con = sqlite3.connect(str(caminho))
        con.executescript("""CREATE TABLE processos(id INTEGER PRIMARY KEY AUTOINCREMENT, numero TEXT NOT NULL UNIQUE,
            numero_limpo TEXT NOT NULL, alias TEXT NOT NULL, apelido TEXT DEFAULT '', cliente TEXT DEFAULT '', pasta TEXT DEFAULT '',
            ativo INTEGER NOT NULL DEFAULT 1, criado_em TEXT NOT NULL, ultima_consulta TEXT, status TEXT NOT NULL DEFAULT 'pendente',
            mensagem TEXT DEFAULT '', classe TEXT DEFAULT '', orgao TEXT DEFAULT '', ultima_atualizacao TEXT DEFAULT '');
            INSERT INTO processos(numero,numero_limpo,alias,criado_em) VALUES('x','1','tjrj','2026-01-01T00:00:00Z');""")
        con.commit()
        con.close()
        b = Banco(caminho)
        self.assertEqual(b.listar()[0]["monitorar"], 1)                # quem já usava o programa não perde o monitoramento
        self.assertEqual(len(b.ativos(so_monitorados=True)), 1)

    def test_varredura_e_consultar_todos_so_pegam_monitorados(self):
        m1, m2 = self.add_nm(1, True), self.add_nm(2, True)
        n1 = self.add_nm(3, False)
        self.assertEqual({c["id"] for c in self.banco.candidatos_varredura("9999-01-01T00:00:00Z")}, {m1, m2})
        self.assertEqual(self.mon.solicitar(None), 2)                  # "consultar todos" = só os monitorados
        esperar(self.mon)
        self.assertEqual(self.banco.obter(n1)["status"], "pendente")
        self.assertEqual(self.mon.solicitar({n1}), 1)                  # pedido explícito (botão "Atualizar") vale para qualquer um
        esperar(self.mon)
        self.assertEqual(self.banco.obter(n1)["status"], "ok")

    def test_resumo_conta_so_monitorados(self):
        self.add_nm(1, True)
        self.add_nm(2, False)
        self.add_nm(3, False)
        r = self.mon.resumo_varredura(MANHA)
        self.assertEqual((r["ativos"], r["restantes"], r["triagem_restantes"]), (1, 1, 2))

    def test_definir_monitorar_manual_limpa_aviso_e_trava_a_automacao(self):
        pid = self.add_nm(1, False)
        self.banco.conn.execute("UPDATE processos SET aviso='x' WHERE id=?", (pid,))
        self.assertEqual(self.banco.definir_monitorar([pid], True, "alto_valor"), 1)
        p = self.banco.obter(pid)
        self.assertEqual((p["monitorar"], p["motivo"], p["monitorar_origem"], p["aviso"]), (1, "alto_valor", "manual", ""))
        self.banco.definir_monitorar([pid], False)
        p = self.banco.obter(pid)
        self.assertEqual((p["monitorar"], p["motivo"], p["monitorar_origem"]), (0, "", "manual"))


class OabAutomatica(Monitoramento):
    def test_sem_oab_com_publicacao_recente_entra_no_monitoramento(self):
        pid = self.add_nm(1)
        self.djen(1, [self.pub(1, 3, 1, OUTRO_ADV)])
        self.triar()
        p = self.banco.obter(pid)
        self.assertEqual((p["oab_status"], p["monitorar"], p["motivo"], p["monitorar_origem"]), ("sem", 1, "sem_oab", "auto"))
        self.assertIn("Entrou no monitoramento", p["aviso"])

    def test_com_oab_nao_entra(self):
        pid = self.add_nm(1)
        self.djen(1, [self.pub(1, 3, 1, NOSSO_ADV)])
        self.triar()
        p = self.banco.obter(pid)
        self.assertEqual((p["oab_status"], p["monitorar"], p["aviso"]), ("com", 0, ""))
        self.assertEqual(p["oab_data"], dias_atras(3))

    def test_sem_oab_mas_parado_ha_muito_tempo_nao_entra(self):
        pid = self.add_nm(1)
        self.djen(1, [self.pub(1, 400, 1, OUTRO_ADV)])
        self.triar()
        p = self.banco.obter(pid)
        self.assertEqual((p["oab_status"], p["monitorar"]), ("sem", 0))

    def test_sem_publicacao_fica_indeterminado(self):
        pid = self.add_nm(1)
        self.djen(1, [])
        self.triar()
        p = self.banco.obter(pid)
        self.assertEqual((p["oab_status"], p["monitorar"], p["dje_status"]), ("", 0, "ok"))

    def test_decisao_manual_de_nao_monitorar_e_respeitada(self):
        pid = self.add_nm(1)
        self.banco.definir_monitorar([pid], False)                     # o usuário decidiu: não quero monitorar
        self.djen(1, [self.pub(1, 3, 1, OUTRO_ADV)])
        self.triar()
        self.assertEqual(self.banco.obter(pid)["monitorar"], 0)

    def test_politica_pode_ser_desligada(self):
        self.cfg.dados["monitoramento"]["auto_sem_oab"] = False
        pid = self.add_nm(1)
        self.djen(1, [self.pub(1, 3, 1, OUTRO_ADV)])
        self.triar()
        p = self.banco.obter(pid)
        self.assertEqual((p["oab_status"], p["monitorar"]), ("sem", 0))

    def test_sem_filtro_de_oab_cadastrado_nada_e_inferido(self):
        self.banco.remover_oab(self.oid)
        pid = self.add_nm(1)
        self.djen(1, [self.pub(1, 3, 1, OUTRO_ADV)])
        self.triar()
        p = self.banco.obter(pid)
        self.assertEqual((p["oab_status"], p["monitorar"]), ("", 0))

    def test_filtro_que_nao_e_oab_nao_conta(self):
        self.banco.remover_oab(self.oid)
        self.banco.adicionar_oab("123.456.789-00", "", "cpf", tipo="cpf")
        pid = self.add_nm(1)
        self.djen(1, [self.pub(1, 3, 1, OUTRO_ADV)])
        self.triar()
        self.assertEqual(self.banco.obter(pid)["oab_status"], "")

    def test_oab_com_zeros_e_sufixo_e_comparada_pelos_digitos(self):
        self.banco.remover_oab(self.oid)
        self.banco.adicionar_oab("0123456", "RJ", "x")
        pid = self.add_nm(1)
        self.djen(1, [self.pub(1, 3, 1, [{"id": 2, "advogado": {"id": 8, "nome": "N", "numero_oab": "123456A", "uf_oab": "rj"}}])])
        self.triar()
        self.assertEqual(self.banco.obter(pid)["oab_status"], "com")

    def test_oab_de_outra_uf_nao_conta(self):
        pid = self.add_nm(1)
        self.djen(1, [self.pub(1, 3, 1, [{"id": 2, "advogado": {"id": 8, "nome": "N", "numero_oab": "123456", "uf_oab": "SP"}}])])
        self.triar()
        self.assertEqual(self.banco.obter(pid)["oab_status"], "sem")

    def test_triagem_nao_repete_antes_de_7_dias_e_nao_pega_quem_ja_tem_oab(self):
        a, b = self.add_nm(1), self.add_nm(2)
        self.djen(1, [self.pub(1, 400, 1, OUTRO_ADV)])    # sem OAB, parado: continua não monitorado
        self.djen(2, [self.pub(2, 3, 2, NOSSO_ADV)])      # com OAB: sai da triagem para sempre
        self.triar()
        self.assertEqual(self.mon.candidatos_triagem(), [])                    # conferidos agora: só voltam depois de 7 dias
        self.banco.conn.execute("UPDATE processos SET dje_consulta='2020-01-01T00:00:00Z'")
        self.assertEqual(self.mon.candidatos_triagem(), [a])                    # passados os 7 dias, só o "sem OAB" volta


class SaidaAutomatica(Monitoramento):
    def test_entrada_automatica_sai_quando_a_oab_aparece(self):
        pid = self.add_nm(1)
        self.djen(1, [self.pub(1, 3, 1, OUTRO_ADV)])
        self.triar()
        self.assertEqual(self.banco.obter(pid)["monitorar"], 1)
        # semana seguinte: a OAB passa a constar no processo e aparece numa publicação
        self.djen(1, [self.pub(1, 3, 1, OUTRO_ADV), self.pub(2, 1, 1, NOSSO_ADV)])
        self.mon.executar([pid], usar_datajud=False)
        p = self.banco.obter(pid)
        self.assertEqual((p["oab_status"], p["monitorar"]), ("com", 0))
        self.assertIn("Saiu do monitoramento", p["aviso"])
        self.assertEqual(p["monitorar_origem"], "auto")

    def test_marcacao_manual_sem_oab_so_avisa(self):
        pid = self.add_nm(1, True, motivo="sem_oab")
        self.djen(1, [self.pub(1, 3, 1, OUTRO_ADV)])
        self.mon.executar([pid], usar_datajud=False)
        self.assertEqual(self.banco.obter(pid)["oab_status"], "sem")
        self.djen(1, [self.pub(1, 3, 1, OUTRO_ADV), self.pub(2, 1, 1, NOSSO_ADV)])
        self.mon.executar([pid], usar_datajud=False)
        p = self.banco.obter(pid)
        self.assertEqual((p["oab_status"], p["monitorar"]), ("com", 1))        # decisão humana não é desfeita
        self.assertIn("considere parar de monitorá-lo", p["aviso"])

    def test_outro_motivo_nao_gera_aviso_nem_saida(self):
        pid = self.add_nm(1, True, motivo="estrategico")
        self.djen(1, [self.pub(1, 3, 1, NOSSO_ADV)])
        self.mon.executar([pid], usar_datajud=False)
        p = self.banco.obter(pid)
        self.assertEqual((p["oab_status"], p["monitorar"], p["aviso"]), ("com", 1, ""))

    def test_dispensar_aviso(self):
        pid = self.add_nm(1)
        self.djen(1, [self.pub(1, 3, 1, OUTRO_ADV)])
        self.triar()
        self.assertEqual(self.banco.total_avisos(), 1)
        self.banco.dispensar_avisos([pid])
        self.assertEqual(self.banco.total_avisos(), 0)
        self.assertEqual(self.banco.obter(pid)["monitorar"], 1)               # dispensar o aviso não muda o monitoramento


class FiltroDeOabEProcessosNaoMonitorados(Monitoramento):
    def item(self, pid, seq, advs, dias=0, **kw):
        return item_dje(pid, dias_atras(dias), limpo(seq), destinatarioadvogados=advs, **kw)

    def rodar_filtro(self, itens):
        DjenFalso.estado["oab"]["123456/RJ"] = itens
        self.banco.conn.execute("UPDATE oabs SET desde=?", (dias_atras(30),))
        self.mon.executar_oab()

    def test_publicacao_de_processo_nao_monitorado_entra_no_processo_e_alerta_uma_vez(self):
        pid = self.add_nm(1)
        itens = [self.item(1, 1, NOSSO_ADV, 0)]
        self.rodar_filtro(itens)
        p = [x for x in self.banco.listar() if x["id"] == pid][0]
        self.assertEqual((p["pub_total"], p["pub_novas"], p["oab_status"]), (1, 1, "com"))
        self.assertEqual(self.mon.resumo_oab["novas"], 1)
        self.rodar_filtro(itens)                                              # mesma publicação de novo: não duplica nem realerta
        self.assertEqual(self.mon.resumo_oab["novas"], 0)
        self.assertEqual([x for x in self.banco.listar() if x["id"] == pid][0]["pub_total"], 1)
        self.assertEqual(self.banco.oab_publicacoes(), [])                    # não vai para a lista de "processos fora do cadastro"

    def test_inserir_via_filtro_nao_estraga_a_linha_de_base_do_djen(self):
        pid = self.add_nm(1)
        self.rodar_filtro([self.item(1, 1, NOSSO_ADV, 0)])
        self.assertEqual(self.banco.obter(pid)["dje_baseline"], 0)
        # a 1ª consulta completa no DJEN: o histórico antigo vira "visto"; só o recente conta
        self.djen(1, [self.item(1, 1, NOSSO_ADV, 0), self.item(2, 1, OUTRO_ADV, 100), self.item(3, 1, OUTRO_ADV, 200)])
        self.mon.executar([pid], usar_datajud=False)
        p = [x for x in self.banco.listar() if x["id"] == pid][0]
        self.assertEqual((p["pub_total"], p["pub_novas"]), (3, 1))

    def test_processo_monitorado_nao_recebe_a_publicacao_do_filtro_mas_marca_com(self):
        pid = self.add_nm(1, True)
        self.rodar_filtro([self.item(1, 1, NOSSO_ADV, 0)])
        p = [x for x in self.banco.listar() if x["id"] == pid][0]
        self.assertEqual((p["pub_total"], p["oab_status"]), (0, "com"))       # a publicação vem pela consulta por processo

    def test_nao_cadastrado_continua_igual(self):
        self.rodar_filtro([self.item(1, 9, NOSSO_ADV, 0)])
        self.assertEqual(len(self.banco.oab_publicacoes()), 1)

    def test_filtro_de_oab_tira_do_monitoramento_quem_entrou_automaticamente(self):
        pid = self.add_nm(1)
        self.djen(1, [self.pub(1, 3, 1, OUTRO_ADV)])
        self.triar()
        self.assertEqual(self.banco.obter(pid)["monitorar"], 1)
        self.rodar_filtro([self.item(5, 1, NOSSO_ADV, 0)])                    # a OAB aparece numa publicação do filtro
        p = self.banco.obter(pid)
        self.assertEqual((p["oab_status"], p["monitorar"]), ("com", 0))

    def test_email_inclui_publicacoes_de_processos_nao_monitorados(self):
        self.cfg.dados["email"].update({"ativo": True, "destinatario": "a@b.c", "usuario": "u"})
        enviados = []
        orig = mailer.enviar
        mailer.enviar = lambda cfg, assunto, corpo: enviados.append((assunto, corpo))
        try:
            pid = self.add_nm(1, apelido="Caso Teste")
            self.rodar_filtro([self.item(1, 1, NOSSO_ADV, 0, texto="Intime-se o autor.")])
        finally:
            mailer.enviar = orig
        self.assertEqual(len(enviados), 1)
        self.assertIn("Caso Teste", enviados[0][1])
        self.assertIn("Intime-se o autor.", enviados[0][1])
        self.assertIn("sem consulta diária", enviados[0][1])


class Rotas(Monitoramento):
    def setUp(self):
        super().setUp()
        import app
        import auth as auth_mod
        import test_equipe
        import threading
        from http.server import ThreadingHTTPServer
        self.app = app
        app.DADOS = Path(self.tmp.name)
        app.CTX.config, app.CTX.banco, app.CTX.monitor = self.cfg, self.banco, self.mon
        app.CTX.auth = auth_mod.Auth(self.banco)
        app.CTX.sobre = {"versao": "t"}
        app.CTX.rede = app.CTX.proxy = False
        self.srv_app = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.srv_app.serve_forever, daemon=True).start()
        app.CTX.auth.criar("chefe", "Chefe", "", "admin", "senha-de-teste-123", trocar_senha=False)
        self.cli = test_equipe.Cliente(self.srv_app.server_port)
        self.assertEqual(self.cli.req("POST", "/api/login", {"usuario": "chefe", "senha": "senha-de-teste-123"})[0], 200)

    def tearDown(self):
        self.srv_app.shutdown()
        self.srv_app.server_close()
        super().tearDown()

    def test_adicionar_monitorado_consulta_tudo_e_nao_monitorado_so_confere_o_djen(self):
        st, r, _ = self.cli.req("POST", "/api/processos", {"numeros": num(1), "motivo": "tribunal_raro", "motivo_obs": "TJ pouco usado"})
        self.assertEqual((st, r["monitorar"]), (200, True))
        esperar(self.mon)
        p = self.banco.listar()[0]
        self.assertEqual((p["monitorar"], p["motivo"], p["motivo_obs"], p["status"]), (1, "tribunal_raro", "TJ pouco usado", "ok"))
        self.assertTrue(ApiFalsa.estado["caminhos"])                        # DataJud consultado
        ApiFalsa.reiniciar()
        DjenFalso.reiniciar()
        st, r, _ = self.cli.req("POST", "/api/processos", {"numeros": num(2), "monitorar": False})
        esperar(self.mon)
        q = [x for x in self.banco.listar() if x["numero_limpo"] == limpo(2)][0]
        self.assertEqual((q["monitorar"], q["status"], q["dje_status"]), (0, "pendente", "ok"))   # DataJud intocado; DJEN conferido
        self.assertEqual(ApiFalsa.estado["caminhos"], [])
        self.assertTrue(DjenFalso.estado["caminhos"])

    def test_importar_nao_monitora_por_padrao_e_aceita_monitorar(self):
        plan = xlsx([["Cliente", "Processo"], ["A", num(10)], ["B", num(11)]])
        b64 = base64.b64encode(plan).decode()
        st, r, _ = self.cli.req("POST", "/api/importar", {"nome": "g.xlsx", "conteudo_b64": b64, "simular": False})
        self.assertEqual((r["adicionados"], r["monitorar"]), (2, False))
        self.assertEqual({p["monitorar"] for p in self.banco.listar()}, {0})
        plan2 = xlsx([["Cliente", "Processo"], ["C", num(12)]])
        st, r, _ = self.cli.req("POST", "/api/importar", {"nome": "h.xlsx", "conteudo_b64": base64.b64encode(plan2).decode(),
                                                          "simular": False, "monitorar": True, "motivo": "alto_valor"})
        esperar(self.mon)
        novo = [p for p in self.banco.listar() if p["numero_limpo"] == limpo(12)][0]
        self.assertEqual((novo["monitorar"], novo["motivo"]), (1, "alto_valor"))

    def test_rota_monitorar_e_dispensar_aviso(self):
        a, b = self.add_nm(1), self.add_nm(2)
        st, r, _ = self.cli.req("POST", "/api/processos/monitorar", {"ids": [a, b], "monitorar": True, "motivo": "estrategico"})
        self.assertEqual((st, r["alterados"]), (200, 2))
        esperar(self.mon)
        self.assertEqual({p["motivo"] for p in self.banco.listar()}, {"estrategico"})
        st, r, _ = self.cli.req("POST", "/api/processos/monitorar", {"ids": [a], "monitorar": False})
        self.assertEqual(self.banco.obter(a)["monitorar"], 0)
        self.assertEqual(self.cli.req("POST", "/api/processos/monitorar", {"ids": []})[0], 400)
        self.banco.conn.execute("UPDATE processos SET aviso='teste' WHERE id=?", (a,))
        st, s, _ = self.cli.req("GET", "/api/status")
        self.assertEqual((s["monitorados"], s["avisos"]), (1, 1))
        st, r, _ = self.cli.req("POST", "/api/processos/avisos/dispensar", {"ids": [a]})
        self.assertEqual((st, r["dispensados"]), (200, 1))
        aud = [x["acao"] for x in self.app.CTX.auth.listar_auditoria()]
        self.assertIn("monitoramento_ligado", aud)
        self.assertIn("monitoramento_desligado", aud)

    def test_config_da_politica_automatica(self):
        st, r, _ = self.cli.req("POST", "/api/config", {"monitoramento": {"auto_sem_oab": False, "auto_dias": 90}})
        self.assertEqual((st, r["monitoramento"]), (200, {"auto_sem_oab": False, "auto_dias": 90}))
        self.assertEqual(self.cli.req("POST", "/api/config", {"monitoramento": {"auto_dias": 5}})[0], 400)


if __name__ == "__main__":
    unittest.main()
