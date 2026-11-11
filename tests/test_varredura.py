"""Testes da v0.5: varredura diária em blocos, arquivamento, descoberta por OAB e importação de planilhas."""
import base64
import datetime as dt
import io
import json
import sys
import time
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import datajud  # noqa: E402
import importacao  # noqa: E402
from db import inicio_do_dia_utc  # noqa: E402
from test_monitor import ApiFalsa, Base, DjenFalso, MOV1, cnj, dias_atras, item_dje  # noqa: E402

MANHA = dt.datetime(2026, 11, 11, 10, 0)


def num(seq, origem="0001"):
    return cnj(f"{seq:07d}", "2023", 8, "19", origem)


def limpo(seq, origem="0001"):
    return num(seq, origem).replace("-", "").replace(".", "")


def esperar(mon, limite=20):
    fim = time.time() + limite
    while mon.rodando and time.time() < fim:
        time.sleep(0.02)
    assert not mon.rodando, "o trabalhador não terminou"


class Varredura(Base):
    def setUp(self):
        super().setUp()
        self.cfg.dados["varredura"].update({"ativa": True, "bloco": 10, "intervalo_min": 30, "inicio": "02:00"})
        self.cfg.dados["dje_ativo"] = False
        ApiFalsa.estado["movs"] = [MOV1]

    def varios(self, n):
        return [self.add(f"{i:07d}") for i in range(1, n + 1)]

    def test_bloco_pega_so_o_tamanho_configurado_e_depois_o_resto(self):
        self.varios(25)
        self.assertEqual(self.mon.iniciar_bloco_se_devido(MANHA), 10)
        esperar(self.mon)
        self.assertEqual(self.banco.consultados_desde(inicio_do_dia_utc()), 10)
        # ainda dentro do intervalo de 30 min: não dispara outro bloco
        self.assertEqual(self.mon.iniciar_bloco_se_devido(MANHA), 0)
        self.mon.ultimo_bloco = time.time() - 31 * 60
        self.assertEqual(self.mon.iniciar_bloco_se_devido(MANHA), 10)
        esperar(self.mon)
        self.mon.ultimo_bloco = time.time() - 31 * 60
        self.assertEqual(self.mon.iniciar_bloco_se_devido(MANHA), 5)
        esperar(self.mon)
        self.mon.ultimo_bloco = time.time() - 31 * 60
        self.assertEqual(self.mon.iniciar_bloco_se_devido(MANHA), 0)  # todos consultados hoje: nada a fazer
        self.assertEqual(self.mon.resumo_varredura(MANHA)["restantes"], 0)

    def test_cada_processo_uma_vez_por_dia_e_amanha_recomeca(self):
        self.varios(4)
        self.mon.iniciar_bloco_se_devido(MANHA)
        esperar(self.mon)
        n_req = len(ApiFalsa.estado["caminhos"])
        self.mon.ultimo_bloco = 0
        self.assertEqual(self.mon.iniciar_bloco_se_devido(MANHA), 0)
        self.assertEqual(len(ApiFalsa.estado["caminhos"]), n_req)   # nenhuma consulta repetida no mesmo dia
        # "amanhã": a consulta de todos ficou para trás
        self.banco.conn.execute("UPDATE processos SET ultima_consulta='2020-01-01T00:00:00Z'")
        self.banco.conn.commit()
        self.assertEqual(self.mon.iniciar_bloco_se_devido(MANHA), 4)
        esperar(self.mon)

    def test_nunca_consultados_vem_antes_dos_mais_antigos(self):
        ids = self.varios(3)
        self.banco.conn.execute("UPDATE processos SET ultima_consulta='2020-01-01T00:00:00Z' WHERE id=?", (ids[0],))
        self.banco.conn.execute("UPDATE processos SET ultima_consulta='2019-01-01T00:00:00Z' WHERE id=?", (ids[1],))
        self.banco.conn.commit()
        self.assertEqual(self.mon.candidatos_hoje(MANHA), [ids[2], ids[1], ids[0]])

    def test_comeca_de_madrugada_e_nao_tem_horario_final(self):
        self.varios(3)
        self.assertEqual(self.mon.iniciar_bloco_se_devido(dt.datetime(2026, 11, 11, 1, 59)), 0)   # antes da hora de início
        self.assertEqual(self.mon.iniciar_bloco_se_devido(dt.datetime(2026, 11, 11, 23, 40)), 3)  # sem fim: roda a qualquer hora depois
        esperar(self.mon)

    def test_pc_ligado_tarde_recupera_a_varredura_do_dia(self):
        self.varios(2)  # o PC estava desligado às 02:00; ao ligar às 14:00, a varredura do dia ainda roda
        self.assertEqual(self.mon.iniciar_bloco_se_devido(dt.datetime(2026, 11, 11, 14, 0)), 2)
        esperar(self.mon)

    def test_desligada_nao_faz_nada(self):
        self.varios(3)
        self.cfg.dados["varredura"]["ativa"] = False
        self.assertEqual(self.mon.iniciar_bloco_se_devido(MANHA), 0)

    def test_arquivados_nao_sao_consultados(self):
        ids = self.varios(3)
        self.assertEqual(self.banco.definir_ativo([ids[0]], False), 1)
        self.assertEqual(self.mon.iniciar_bloco_se_devido(MANHA), 2)
        esperar(self.mon)
        self.assertEqual(self.banco.obter(ids[0])["status"], "pendente")
        self.assertEqual(self.banco.definir_ativo([ids[0]], True), 1)

    def test_pendentes_novos_nao_furam_o_ritmo_dos_blocos(self):
        self.varios(30)
        self.assertEqual(self.mon.agendar_pendentes(), 0)  # com a varredura ativa, a 1ª consulta espera o bloco
        self.cfg.dados["varredura"]["ativa"] = False
        self.assertGreater(self.mon.agendar_pendentes(), 0)  # no modo clássico, continua como antes
        esperar(self.mon)

    def test_erro_nao_conta_como_pendente_do_bloco_mas_volta_por_backoff(self):
        ids = self.varios(2)
        ApiFalsa.estado["padrao"] = "500"
        self.mon.iniciar_bloco_se_devido(MANHA)
        esperar(self.mon)
        self.assertEqual({p["status"] for p in self.banco.listar()}, {"erro"})
        self.assertEqual(self.mon.candidatos_hoje(MANHA), [])  # já tentados hoje: o retorno é por backoff, não pelos blocos
        self.assertIn(ids[0], self.mon.proxima)

    def test_resumo_previsao_de_duracao(self):
        self.varios(25)
        r = self.mon.resumo_varredura(MANHA)
        self.assertEqual((r["ativos"], r["feitos_hoje"], r["restantes"], r["na_janela"]), (25, 0, 25, True))
        self.assertEqual(r["previsao_min"], 3 * 30)    # 25 processos / blocos de 10 = 3 blocos x 30 min
        self.assertNotIn("fim", r)

    def test_config_valida_varredura(self):
        for ruim in ({"bloco": 5}, {"bloco": 900}, {"intervalo_min": 1}, {"inicio": "25:00"}, {"inicio": "2h"}):
            with self.assertRaises(ValueError):
                self.cfg.atualizar({"varredura": ruim})
        self.cfg.atualizar({"varredura": {"bloco": 50, "inicio": "03:30", "fim": "20:00"}})
        self.assertNotIn("fim", self.cfg.dados["varredura"])
        self.assertEqual(self.cfg.dados["varredura"]["bloco"], 50)


class Arquivamento(Base):
    def test_sem_andamento_considera_movimento_e_publicacao(self):
        parado = self.add("0000001")
        movimentado = self.add("0000002")
        com_pub = self.add("0000003")
        sem_dados = self.add("0000004")
        antigo = {"codigo": 1, "nome": "Distribuição", "dataHora": "2023-01-01T10:00:00.000Z"}
        recente = {"codigo": 2, "nome": "Juntada", "dataHora": dias_atras(10) + "T10:00:00.000Z"}
        for pid, movs in ((parado, [antigo]), (movimentado, [antigo, recente]), (com_pub, [antigo])):
            ApiFalsa.estado["movs"] = movs
            self.mon.executar([pid], usar_dje=False)
        self.banco.registrar_publicacoes(com_pub, [dict(pub_id="9", data_disp=dias_atras(5), tipo_com="", orgao="", tipo_doc="",
                                                        classe="", texto="x", meio="", hash="", destinatarios="[]", advogados="[]",
                                                        cancelada=0, numero="", tribunal="")])
        corte = dias_atras(365)
        ids = [x["id"] for x in self.banco.sem_andamento(corte)]
        self.assertEqual(ids, [parado])           # recente (movimento) e com publicação recente ficam; sem dados não se afirma nada
        self.assertNotIn(sem_dados, ids)

    def test_definir_ativo_e_listagem(self):
        a, b = self.add("0000001"), self.add("0000002")
        self.assertEqual(self.banco.definir_ativo([a, b], False), 2)
        self.assertEqual(self.banco.definir_ativo([a, b], False), 0)        # já estavam arquivados
        self.assertEqual(len(self.banco.ativos()), 0)
        self.assertEqual(len(self.banco.listar()), 2)                       # arquivados continuam listados (e não são apagados)
        self.assertEqual(self.banco.definir_ativo([], True), 0)

    def test_arquivado_que_reaparece_no_filtro_gera_alerta(self):
        numero = limpo(7)
        pid = self.banco.adicionar(num(7), numero, "tjrj")
        oid = self.banco.adicionar_oab("123456", "RJ", "x")
        itens = [{"pub_id": "1", "data_disp": dias_atras(0), "tipo_com": "", "orgao": "", "tipo_doc": "", "classe": "", "texto": "t",
                  "meio": "", "destinatarios": "[]", "advogados": "[]", "cancelada": 0, "numero": numero, "tribunal": "TJRJ"}]
        self.assertEqual(self.banco.registrar_oab_pubs(oid, itens), [])      # processo ativo: tratado na consulta por processo
        self.banco.definir_ativo([pid], False)
        itens[0]["pub_id"] = "2"
        itens[0]["texto"] = "outra"
        self.assertEqual(len(self.banco.registrar_oab_pubs(oid, itens)), 1)  # arquivado: a novidade volta a alertar


class Descoberta(Base):
    CH = "123456/RJ"

    def setUp(self):
        super().setUp()
        self.oid = self.banco.adicionar_oab("123456", "RJ", "x")

    def pub(self, pid, dias, seq, **kw):
        return item_dje(pid, dias_atras(dias), limpo(seq), **kw)

    def rodar(self, meses_dias=365):
        self.mon.solicitar_descoberta(self.oid, meses_dias)
        esperar(self.mon)
        return self.banco.descobertos(self.oid)

    def test_agrupa_por_processo_e_respeita_o_periodo(self):
        DjenFalso.estado["oab"][self.CH] = [self.pub(1, 5, 11), self.pub(2, 40, 11), self.pub(3, 20, 12), self.pub(4, 500, 13)]
        r = self.rodar(365)
        nums = {i["numero_limpo"]: i for i in r["itens"]}
        self.assertEqual(set(nums), {limpo(11), limpo(12)})              # 13 é de 500 dias atrás: fora do período
        self.assertEqual(nums[limpo(11)]["n_pubs"], 2)
        self.assertEqual(nums[limpo(11)]["ultima_data"], dias_atras(5))
        self.assertEqual(nums[limpo(11)]["numero"], num(11))   # devolvido já formatado
        self.assertEqual(r["info"]["status"], "ok")
        self.assertEqual([i["numero_limpo"] for i in r["itens"]][0], limpo(11))          # mais recente primeiro
        self.assertTrue(all(i["situacao"] == "novo" for i in r["itens"]))

    def test_nao_guarda_texto_e_marca_situacao(self):
        DjenFalso.estado["oab"][self.CH] = [self.pub(1, 5, 11, texto="SEGREDO DO TEXTO"), self.pub(2, 5, 12)]
        self.banco.adicionar(num(11), limpo(11), "tjrj")
        pid12 = self.banco.adicionar(num(12), limpo(12), "tjrj")
        self.banco.definir_ativo([pid12], False)
        r = self.rodar()
        self.assertEqual({i["numero_limpo"]: i["situacao"] for i in r["itens"]}, {limpo(11): "cadastrado", limpo(12): "arquivado"})
        con_texto = self.banco.conn.execute("SELECT COUNT(*) FROM oab_pubs").fetchone()[0]
        self.assertEqual(con_texto, 0)                                   # descoberta não alimenta a lista de publicações
        self.assertNotIn("SEGREDO", json.dumps(r))

    def test_numero_invalido_ou_tribunal_nao_suportado_e_ignorado(self):
        DjenFalso.estado["oab"][self.CH] = [self.pub(1, 5, 11), item_dje(2, dias_atras(5), "12345678901234567890")]
        r = self.rodar()
        self.assertEqual(len(r["itens"]), 1)
        self.assertEqual(r["info"]["ignorados"], 1)
        self.assertIn("ignorada", r["info"]["mensagem"])

    def test_resultado_parcial_e_avisado(self):
        DjenFalso.estado["oab"][self.CH] = [self.pub(100 + i, 1, 200 + i) for i in range(60)]
        DjenFalso.estado["vazia_na_pagina"] = 2     # a API "acaba" na página 2, apesar do total maior
        r = self.rodar()
        self.assertEqual(r["info"]["truncado"], 1)
        self.assertIn("parcial", r["info"]["mensagem"])

    def test_falha_da_api_vira_erro_legivel_sem_derrubar_nada(self):
        DjenFalso.estado["padrao"] = "500"
        r = self.rodar()
        self.assertEqual(r["info"]["status"], "erro")
        self.assertTrue(r["info"]["mensagem"])
        self.assertEqual(r["itens"], [])

    def test_nova_busca_substitui_a_anterior_e_filtro_removido_apaga_tudo(self):
        DjenFalso.estado["oab"][self.CH] = [self.pub(1, 5, 11)]
        self.rodar()
        DjenFalso.estado["oab"][self.CH] = [self.pub(2, 5, 12)]
        r = self.rodar()
        self.assertEqual([i["numero_limpo"] for i in r["itens"]], [limpo(12)])
        self.banco.remover_oab(self.oid)
        self.assertEqual(self.banco.conn.execute("SELECT COUNT(*) FROM descobertos").fetchone()[0], 0)
        self.assertEqual(self.banco.conn.execute("SELECT COUNT(*) FROM descoberta_exec").fetchone()[0], 0)


def xlsx(linhas, compartilhado=True):
    """Monta um .xlsx mínimo (com sharedStrings ou texto embutido) para os testes."""
    def col(i):
        return "ABCDEFGHIJ"[i]
    strings, linhas_xml = [], []
    for r, lin in enumerate(linhas, 1):
        cel = []
        for c, v in enumerate(lin):
            if v is None:
                continue
            if compartilhado:
                strings.append(v)
                cel.append(f'<c r="{col(c)}{r}" t="s"><v>{len(strings) - 1}</v></c>')
            else:
                cel.append(f'<c r="{col(c)}{r}" t="inlineStr"><is><t>{v}</t></is></c>')
        linhas_xml.append(f'<row r="{r}">{"".join(cel)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(linhas_xml)}</sheetData></worksheet>')
        if compartilhado:
            z.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
    return buf.getvalue()


class Importacao(unittest.TestCase):
    N1, N2, N3 = (num(s) for s in (21, 22, 23))

    def test_xlsx_com_colunas_cliente_e_pasta(self):
        dados = xlsx([["Título", "Cliente", "Pasta", "Número do processo"],
                      ["Ação X", "Condomínio Exemplo", "172", self.N1],
                      ["Ação Y", "Maria Exemplo", "55", self.N2 + " (apenso)"],
                      ["sem número", "Fulano", "9", "—"],
                      ["repetido", "Outro", "1", self.N1]])
        r = importacao.analisar(dados)
        self.assertEqual([v["numero"] for v in r["validos"]], [self.N1, self.N2])         # sem repetição
        self.assertEqual((r["validos"][0]["cliente"], r["validos"][0]["pasta"], r["validos"][0]["apelido"]),
                         ("Condomínio Exemplo", "172", "Ação X"))
        self.assertEqual(r["colunas"], ["apelido", "cliente", "pasta"])

    def test_xlsx_texto_embutido_e_sem_cabecalho(self):
        r = importacao.analisar(xlsx([[self.N1], [None, self.N2]], compartilhado=False))
        self.assertEqual({v["numero"] for v in r["validos"]}, {self.N1, self.N2})
        self.assertEqual(r["colunas"], [])
        self.assertEqual(r["validos"][0]["cliente"], "")

    def test_csv_com_ponto_e_virgula_e_numeros_sem_pontuacao(self):
        csv_ = f"Cliente;Processo\nAna;{self.N1}\nBia;{limpo(22)}\n".encode("utf-8")
        r = importacao.analisar(csv_)
        self.assertEqual([(v["numero"], v["cliente"]) for v in r["validos"]], [(self.N1, "Ana"), (self.N2, "Bia")])

    def test_relatorio_html_disfarcado_de_xls(self):
        html = (f"<html><body><table><tr><th>Cliente</th><th>Processo</th></tr>"
                f"<tr><td>Cia &amp; Filhos</td><td>{self.N3}</td></tr></table></body></html>").encode("latin-1")
        r = importacao.analisar(html)
        self.assertEqual((r["validos"][0]["numero"], r["validos"][0]["cliente"]), (self.N3, "Cia & Filhos"))

    def test_digito_invalido_e_tribunal_nao_suportado_sao_rejeitados(self):
        ruim = self.N1[:-1] + ("1" if self.N1[-1] != "1" else "2")
        stf = datajud.RE_CNJ_BUSCA.findall(cnj("0000005", "2023", 1, "00", "0000"))
        r = importacao.analisar(f"{ruim}\n{self.N2}\n{stf[0]}\n".encode())
        self.assertEqual([v["numero"] for v in r["validos"]], [self.N2])
        self.assertEqual(len(r["invalidos"]), 2)

    def test_xls_antigo_xlsx_corrompido_e_vazio_dao_erro_claro(self):
        with self.assertRaises(importacao.ErroImportacao) as c:
            importacao.analisar(b"\xd0\xcf\x11\xe0" + b"0" * 50)
        self.assertIn(".xlsx", str(c.exception))
        with self.assertRaises(importacao.ErroImportacao):
            importacao.analisar(b"PK\x03\x04lixo")
        with self.assertRaises(importacao.ErroImportacao):
            importacao.analisar(b"")


class RotasV05(Base):
    """Rotas HTTP novas, com login (reaproveita o servidor de teste da v0.4)."""

    def setUp(self):
        super().setUp()
        import app
        import auth as auth_mod
        import test_equipe
        from http.server import ThreadingHTTPServer
        import threading
        self.app = app
        app.DADOS = Path(self.tmp.name)
        app.CTX.config, app.CTX.banco, app.CTX.monitor = self.cfg, self.banco, self.mon
        app.CTX.auth = auth_mod.Auth(self.banco)
        app.CTX.sobre = {"versao": "t"}
        app.CTX.rede = app.CTX.proxy = False
        self.cfg.dados["dje_ativo"] = False
        self.srv_app = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.srv_app.serve_forever, daemon=True).start()
        app.CTX.auth.criar("chefe", "Chefe", "", "admin", "senha-de-teste-123", trocar_senha=False)
        self.cli = test_equipe.Cliente(self.srv_app.server_port)
        self.assertEqual(self.cli.req("POST", "/api/login", {"usuario": "chefe", "senha": "senha-de-teste-123"})[0], 200)

    def tearDown(self):
        self.srv_app.shutdown()
        self.srv_app.server_close()
        super().tearDown()

    def b64(self, dados):
        return base64.b64encode(dados).decode()

    def test_importar_simula_e_depois_cadastra_sem_disparar_tudo(self):
        self.cfg.dados["varredura"]["ativa"] = True
        n = [num(s) for s in range(30, 60)]
        planilha = xlsx([["Cliente", "Processo"]] + [["Cli " + str(i), x] for i, x in enumerate(n)])
        st, r, _ = self.cli.req("POST", "/api/importar", {"nome": "g.xlsx", "conteudo_b64": self.b64(planilha), "simular": True})
        self.assertEqual((st, r["novos"], r["simulado"]), (200, 30, True))
        self.assertEqual(len(self.banco.listar()), 0)                       # simulação não grava nada
        st, r, _ = self.cli.req("POST", "/api/importar", {"nome": "g.xlsx", "conteudo_b64": self.b64(planilha), "simular": False})
        self.assertEqual((r["adicionados"], r["em_varredura"]), (30, True))
        self.assertEqual(len(self.banco.listar()), 30)
        self.assertEqual(self.banco.listar()[0]["cliente"].startswith("Cli"), True)
        self.assertEqual(ApiFalsa.estado["caminhos"], [])                   # nada foi consultado na hora: fica para os blocos
        st, r, _ = self.cli.req("POST", "/api/importar", {"nome": "g.xlsx", "conteudo_b64": self.b64(planilha), "simular": True})
        self.assertEqual((r["novos"], r["repetidos"]), (0, 30))
        aud = [a["acao"] for a in self.app.CTX.auth.listar_auditoria()]
        self.assertIn("processos_importados", aud)

    def test_importar_erros(self):
        st, r, _ = self.cli.req("POST", "/api/importar", {"conteudo_b64": self.b64(b"\xd0\xcf\x11\xe0xxxx")})
        self.assertEqual(st, 400)
        st, r, _ = self.cli.req("POST", "/api/importar", {"conteudo_b64": self.b64(b"nada aqui"), "simular": True})
        self.assertEqual((st, r["encontrados"]), (200, 0))

    def test_arquivar_parados_pede_simulacao_antes(self):
        pid = self.add("0000001")
        ApiFalsa.estado["movs"] = [{"codigo": 1, "nome": "Distribuição", "dataHora": "2021-01-01T10:00:00.000Z"}]
        self.mon.executar([pid], usar_dje=False)
        st, r, _ = self.cli.req("POST", "/api/processos/inativos", {"meses": 12})   # padrão: só simula
        self.assertEqual((r["total"], len(self.banco.ativos())), (1, 1))
        st, r, _ = self.cli.req("POST", "/api/processos/inativos", {"meses": 12, "simular": False})
        self.assertEqual((r["arquivados"], len(self.banco.ativos())), (1, 0))
        self.assertEqual(self.cli.req("POST", "/api/processos/inativos", {"meses": 0})[0], 400)
        st, r, _ = self.cli.req("POST", "/api/processos/ativo", {"ids": [pid], "ativo": True})
        self.assertEqual((r["alterados"], len(self.banco.ativos())), (1, 1))
        self.assertEqual(self.cli.req("POST", "/api/processos/ativo", {"ids": []})[0], 400)

    def test_status_traz_varredura_e_rotas_de_descoberta(self):
        self.add("0000001")
        st, s, _ = self.cli.req("GET", "/api/status")
        self.assertEqual((s["varredura"]["ativos"], s["varredura"]["ativa"]), (1, False))   # nos testes a varredura está desligada
        oid = self.banco.adicionar_oab("123456", "RJ", "x")
        self.assertEqual(self.cli.req("POST", f"/api/oabs/{oid}/descobrir", {"meses": 0})[0], 400)
        self.assertEqual(self.cli.req("POST", "/api/oabs/999/descobrir", {"meses": 12})[0], 404)
        st, r, _ = self.cli.req("POST", f"/api/oabs/{oid}/descobrir", {"meses": 12})
        self.assertEqual(st, 200)
        esperar(self.mon)
        st, r, _ = self.cli.req("GET", f"/api/oabs/{oid}/descobertos")
        self.assertEqual(st, 200)
        self.assertIn("itens", r)

    def test_cadastro_em_massa_nao_dispara_consulta_imediata_com_varredura_ativa(self):
        self.cfg.dados["varredura"]["ativa"] = True
        nums = "\n".join(num(s) for s in range(100, 125))
        st, r, _ = self.cli.req("POST", "/api/processos", {"numeros": nums})
        self.assertEqual((len(r["adicionados"]), r["em_varredura"]), (25, True))
        time.sleep(0.2)
        self.assertEqual(ApiFalsa.estado["caminhos"], [])


if __name__ == "__main__":
    unittest.main()
