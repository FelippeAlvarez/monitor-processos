import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datajud  # noqa: E402
import dje  # noqa: E402
import monitor as monitor_mod  # noqa: E402
from config import Config  # noqa: E402
from db import Banco  # noqa: E402
from monitor import Monitor  # noqa: E402


def cnj(seq, ano, j, tr, origem):
    dv = datajud.calcular_dv(seq, ano, j, tr, origem)
    return f"{seq}-{dv:02d}.{ano}.{j}.{tr}.{origem}"


class TesteNumero(unittest.TestCase):
    def test_exemplo_publico_valido(self):
        info = datajud.interpretar_numero("0000832-35.2018.4.01.3202")
        self.assertEqual(info["limpo"], "00008323520184013202")
        self.assertEqual(datajud.alias_tribunal(info["j"], info["tr"]), "trf1")

    def test_dv_invalido(self):
        with self.assertRaises(datajud.ErroNumero):
            datajud.interpretar_numero("0000832-36.2018.4.01.3202")

    def test_formato_invalido(self):
        with self.assertRaises(datajud.ErroNumero):
            datajud.interpretar_numero("12345")

    def test_sem_pontuacao(self):
        self.assertEqual(datajud.interpretar_numero("00008323520184013202")["numero"], "0000832-35.2018.4.01.3202")

    def test_aliases(self):
        self.assertEqual(datajud.alias_tribunal(8, 19), "tjrj")
        self.assertEqual(datajud.alias_tribunal(8, 26), "tjsp")
        self.assertEqual(datajud.alias_tribunal(8, 5), "tjba")
        self.assertEqual(datajud.alias_tribunal(5, 1), "trt1")
        self.assertEqual(datajud.alias_tribunal(3, 0), "stj")
        with self.assertRaises(datajud.TribunalNaoSuportado):
            datajud.alias_tribunal(1, 0)

    def test_busca_em_texto(self):
        texto = "ver 0000832-35.2018.4.01.3202, e também 00008323520184013202;"
        self.assertEqual(len(datajud.RE_CNJ_BUSCA.findall(texto)), 2)


class TesteNormalizar(unittest.TestCase):
    def test_une_graus_e_remove_duplicados(self):
        mov = {"codigo": 26, "nome": "Distribuição", "dataHora": "2024-01-02T10:00:00.000Z"}
        fontes = [
            {"grau": "G1", "classe": {"nome": "Execução"}, "dataHoraUltimaAtualizacao": "2024-02-01T00:00:00Z",
             "orgaoJulgador": {"nome": "5ª Vara"}, "movimentos": [mov, mov]},
            {"grau": "G2", "dataHoraUltimaAtualizacao": "2024-01-01T00:00:00Z", "movimentos": [mov]},
        ]
        r = datajud.normalizar(fontes)
        self.assertEqual(len(r["movimentos"]), 2)
        self.assertEqual(r["classe"], "Execução")

    def test_data_compacta(self):
        self.assertEqual(datajud.normalizar_data("20240102103000"), "2024-01-02T10:30:00")

    def test_vazio(self):
        self.assertIsNone(datajud.normalizar([]))


# ---------------------------------------------------------------- API simulada
class ApiFalsa(BaseHTTPRequestHandler):
    """Estado de classe controla o comportamento; 'sequencia' é consumida uma ação por requisição.
    Ações: ok | 429 | 429ra (com Retry-After: 7) | drop (fecha sem responder) | 500 | 400."""
    estado = {}

    @classmethod
    def reiniciar(cls):
        cls.estado = {"movs": [], "caminhos": [], "auth": [], "corpos": [], "sequencia": [],
                      "padrao": "ok", "atraso": 0.0, "ausentes": set(), "ausentes_lote": set(),
                      "max_hits": None, "movs_por_numero": {}}

    def log_message(self, *a):
        pass

    def do_POST(self):
        e = ApiFalsa.estado
        corpo = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        e["caminhos"].append(self.path)
        e["auth"].append(self.headers.get("Authorization"))
        e["corpos"].append(corpo)
        if e["atraso"]:
            time.sleep(e["atraso"])
        acao = e["sequencia"].pop(0) if e["sequencia"] else e["padrao"]
        if acao == "drop":
            return
        if acao in ("429", "429ra", "500", "400"):
            self.send_response(int(acao[:3]))
            if acao == "429ra":
                self.send_header("Retry-After", "7")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        query = corpo.get("query", {})
        em_lote = "terms" in query
        if em_lote:
            numeros = query["terms"]["numeroProcesso"]
        else:
            numeros = [query["match"]["numeroProcesso"]]
        hits = []
        for n in numeros:
            if e["movs"] is None or n in e["ausentes"] or (em_lote and n in e["ausentes_lote"]):
                continue
            hits.append({"_source": {
                "numeroProcesso": n, "grau": "G1", "classe": {"nome": "Procedimento Comum"},
                "orgaoJulgador": {"nome": "1ª Vara"}, "dataHoraUltimaAtualizacao": "2024-05-01T00:00:00Z",
                "movimentos": e["movs_por_numero"].get(n, e["movs"])}})
        if e["max_hits"] is not None:
            hits = hits[:e["max_hits"]]
        meta = {"took": 5, "timed_out": False, "_shards": {"total": 5, "successful": 5, "skipped": 0, "failed": 0}}
        if acao == "parcial":  # HTTP 200, mas o Elasticsearch estourou o tempo e devolveu só parte
            meta = {"took": 30000, "timed_out": True, "_shards": {"total": 5, "successful": 4, "skipped": 0, "failed": 1}}
            hits = hits[:1]
        resp = json.dumps({**meta, "hits": {"hits": hits}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


class DjenFalso(BaseHTTPRequestHandler):
    """Simula a API Comunica PJe (GET /api/v1/comunicacao?numeroProcesso=...), no formato visto no print real."""
    estado = {}

    @classmethod
    def reiniciar(cls):
        cls.estado = {"itens": {}, "sequencia": [], "padrao": "ok", "caminhos": [], "tamanho_pagina": None,
                      "ignora_pagina": False, "oab": {}, "max_pagina": 50, "vazia_na_pagina": None, "texto": {}}

    def log_message(self, *a):
        pass

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        e = DjenFalso.estado
        e["caminhos"].append(self.path)
        acao = e["sequencia"].pop(0) if e["sequencia"] else e["padrao"]
        if acao == "drop":
            return
        if acao in ("429", "403", "500"):
            self.send_response(int(acao))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        q = parse_qs(urlparse(self.path).query)
        if "numeroOab" in q or "texto" in q:
            ini = q.get("dataDisponibilizacaoInicio", ["0000-00-00"])[0]
            if "texto" in q:
                fonte = e["texto"].get(q["texto"][0], [])
            else:
                fonte = e["oab"].get(f"{q['numeroOab'][0]}/{q.get('ufOab', [''])[0]}", [])
            todos = [i for i in fonte if i["data_disponibilizacao"] >= ini]
            tam = min(int(q.get("itensPorPagina", ["50"])[0]), e["max_pagina"])
            pag = int(q.get("pagina", ["1"])[0])
            fatia = [] if e["vazia_na_pagina"] and pag >= e["vazia_na_pagina"] else todos[(pag - 1) * tam: pag * tam]
            corpo = json.dumps({"status": "success", "message": "Sucesso", "count": len(todos), "items": fatia}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
            return
        numero = q.get("numeroProcesso", [""])[0]
        itens = e["itens"].get(numero, [])
        total = len(itens)
        if e["tamanho_pagina"]:
            pagina = 1 if e["ignora_pagina"] else int(q.get("pagina", ["1"])[0])
            t = e["tamanho_pagina"]
            itens = itens[(pagina - 1) * t: pagina * t]
        corpo = b"lixo" if acao == "lixo" else json.dumps(
            {"status": "success", "message": "Sucesso", "count": total, "items": itens}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)


def item_dje(pub_id, data, numero, texto="Intime-se.", **extra):
    base = {
        "id": pub_id, "data_disponibilizacao": data, "siglaTribunal": "TJRJ", "tipoComunicacao": "Intimação",
        "nomeOrgao": "Regional de Jacarepaguá 1- Cartório da 6ª Vara Cível", "idOrgao": 52893, "texto": texto,
        "numero_processo": numero, "meio": "D", "link": None, "tipoDocumento": "Decisão",
        "nomeClasse": "EXECUÇÃO DE TÍTULO EXTRAJUDICIAL", "codigoClasse": "12154", "ativo": True,
        "hash": f"hash{pub_id}", "status": "P", "motivo_cancelamento": None, "data_cancelamento": None,
        "meiocompleto": "Diário de Justiça Eletrônico Nacional",
        "destinatarios": [{"nome": "ESCRITÓRIO EXEMPLO", "polo": "A", "comunicacao_id": pub_id},
                          {"nome": "PARTE EXEMPLO", "polo": "P", "comunicacao_id": pub_id}],
        "destinatarioadvogados": [
            {"id": 1, "advogado": {"id": 2751993, "nome": "ADVOGADO EXEMPLO", "numero_oab": "123456", "uf_oab": "RJ"}},
            {"id": 2, "advogado": {"id": 1608460, "nome": "ADVOGADA EXEMPLO", "numero_oab": "654321", "uf_oab": "RJ"}},
            {"id": 3, "advogado": {"id": 2751993, "nome": "ADVOGADO EXEMPLO", "numero_oab": "123456", "uf_oab": "RJ"}}],
    }
    base.update(extra)
    return base


def dias_atras(n):
    import datetime as _dt
    return (_dt.date.today() - _dt.timedelta(days=n)).isoformat()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), ApiFalsa)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.srv_dje = ThreadingHTTPServer(("127.0.0.1", 0), DjenFalso)
        threading.Thread(target=cls.srv_dje.serve_forever, daemon=True).start()
        cls.url_dje = f"http://127.0.0.1:{cls.srv_dje.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv_dje.shutdown()

    def setUp(self):
        ApiFalsa.reiniciar()
        DjenFalso.reiniciar()
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cfg = Config(base / "config.json")
        self.cfg.dados["base_url"] = self.url
        self.cfg.dados["dje_base_url"] = self.url_dje
        self.cfg.dados["varredura"]["ativa"] = False  # testes antigos usam o modo clássico (ciclo completo); a varredura em blocos tem testes próprios
        self.banco = Banco(base / "p.db")
        self.mon = Monitor(self.banco, self.cfg, pausa_entre_consultas=0, espera_429=(1, 2, 3), pausa_limite=900)
        self.esperas = []
        self.mon._dormir = self.esperas.append  # não dorme de verdade; registra o que dormiria

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, seq, origem="0001", j=8, tr="19", alias="tjrj", apelido=""):
        info = datajud.interpretar_numero(cnj(seq, "2023", j, tr, origem))
        return self.banco.adicionar(info["numero"], info["limpo"], alias, apelido)

    def status(self):
        return {p["id"]: p["status"] for p in self.banco.listar()}


MOV1 = {"codigo": 1, "nome": "Distribuição", "dataHora": "2024-01-01T10:00:00.000Z"}
MOV2 = {"codigo": 2, "nome": "Juntada de petição", "dataHora": "2024-05-02T09:00:00.000Z",
        "complementosTabelados": [{"nome": "tipo", "descricao": "contestação"}]}


class TesteCiclo(Base):
    def test_baseline_e_novidade(self):
        pid = self.add("0001234", apelido="Teste")
        ApiFalsa.estado["movs"] = [MOV1]
        r = self.mon.executar()
        self.assertEqual((r["consultados"], r["novos"], r["erros"]), (1, 0, 0))
        self.assertEqual(ApiFalsa.estado["caminhos"][-1], "/api_publica_tjrj/_search")
        self.assertTrue(ApiFalsa.estado["auth"][-1].startswith("APIKey "))
        ApiFalsa.estado["movs"] = [MOV1, MOV2]
        self.assertEqual(self.mon.executar()["novos"], 1)
        p = self.banco.listar()[0]
        self.assertEqual((p["novos"], p["ultimo_mov"]), (1, "Juntada de petição"))
        self.assertEqual(self.mon.executar()["novos"], 0)
        self.banco.marcar_visto(pid)
        self.assertEqual(self.banco.listar()[0]["novos"], 0)

    def test_nao_encontrado(self):
        pid = self.add("0001234")
        ApiFalsa.estado["movs"] = None
        self.mon.executar()
        self.assertEqual(self.banco.obter(pid)["status"], "nao_encontrado")

    def test_erro_chave_interrompe_e_nao_repete(self):
        for i in range(3):
            self.add(f"000100{i}")
        ApiFalsa.estado["padrao"] = "400"  # 400 no lote -> cai para individual; força 401 abaixo
        ApiFalsa.estado["sequencia"] = []
        # simula chave recusada
        ApiFalsa.estado["padrao"] = "ok"
        orig = datajud._post

        def post_401(url, payload, api_key, timeout):
            raise datajud.ErroAPI("Chave da API recusada (401/403).", 401)
        datajud._post = post_401
        try:
            r = self.mon.executar()
        finally:
            datajud._post = orig
        self.assertIn("Chave", r["aviso"])
        self.assertTrue(all(s == "pendente" for s in self.status().values()))

    def test_config_nao_expoe_senha_e_preserva(self):
        self.cfg.atualizar({"email": {"senha": "segredo", "usuario": "a@b.c"}})
        self.assertNotIn("senha", self.cfg.publico()["email"])
        self.cfg.atualizar({"email": {"senha": "", "usuario": "x@y.z"}})
        self.assertEqual(self.cfg.dados["email"]["senha"], "segredo")
        with self.assertRaises(ValueError):
            self.cfg.atualizar({"intervalo_horas": 0.5})

    def test_email_texto(self):
        pid = self.add("0001234")
        txt = Monitor.montar_email([(self.banco.obter(pid), [{"nome": "X", "data_hora": "2024-05-02T09:00:00", "complementos": ""}])])
        self.assertIn("NÃO substitui", txt)

    def test_duplicado(self):
        self.add("0001234")
        info = datajud.interpretar_numero(cnj("0001234", "2023", 8, "19", "0001"))
        self.assertIsNone(self.banco.adicionar(info["numero"], info["limpo"], "tjrj"))


class TesteFalhasDoPdf(Base):
    """Cenários observados no teste real: 429, quedas de conexão, ciclo que não alcança todos."""

    def test_queda_de_conexao_nao_mata_o_ciclo(self):
        ids = [self.add(f"000200{i}", origem=f"000{i}", alias=a) for i, a in enumerate(["tjrj", "trt1", "tjba", "tjsp"])]
        ApiFalsa.estado["movs"] = [MOV1]
        ApiFalsa.estado["sequencia"] = ["drop", "ok", "drop", "ok"]  # v0.1.0 morria já na 1ª queda
        r = self.mon.executar()
        self.assertEqual(r["consultados"], 4)
        self.assertEqual(r["erros"], 2)
        st = list(self.status().values())
        self.assertEqual(sorted(st), ["erro", "erro", "ok", "ok"])
        erro = [p for p in self.banco.listar() if p["status"] == "erro"][0]
        self.assertIn("RemoteDisconnected", erro["mensagem"])

    def test_429_com_retry_after_e_recupera(self):
        pid = self.add("0003001")
        ApiFalsa.estado["movs"] = [MOV1]
        ApiFalsa.estado["sequencia"] = ["429ra", "ok"]
        r = self.mon.executar()
        self.assertEqual((r["consultados"], r["erros"]), (1, 0))
        self.assertEqual(self.banco.obter(pid)["status"], "ok")
        self.assertIn(7, self.esperas)  # respeitou o Retry-After

    def test_429_persistente_nao_marca_erro_e_pausa(self):
        ids = [self.add(f"000400{i}", origem=f"000{i}", alias=a) for i, a in enumerate(["tjrj", "trt1", "tjba"])]
        ApiFalsa.estado["padrao"] = "429"
        r = self.mon.executar()
        self.assertEqual(r["erros"], 0)                       # não é falha do processo
        self.assertEqual(r["pendentes"], 3)
        self.assertTrue(all(s == "pendente" for s in self.status().values()))
        self.assertGreater(self.mon.pausado_ate, time.time() + 800)
        self.assertEqual(len(ApiFalsa.estado["caminhos"]), 1 + 3)  # parou após esgotar as esperas; não varreu todos
        self.assertEqual(self.mon.agendar_pendentes(), 0)     # respeita a pausa
        msg = self.banco.obter(ids[0])["mensagem"]
        self.assertIn("nova tentativa automática", msg)

    def test_pendentes_voltam_sozinhos_apos_pausa(self):
        ids = [self.add(f"000500{i}", origem=f"000{i}", alias=a) for i, a in enumerate(["tjrj", "trt1"])]
        ApiFalsa.estado["padrao"] = "429"
        self.mon.executar()
        ApiFalsa.estado["padrao"] = "ok"
        ApiFalsa.estado["movs"] = [MOV1]
        self.mon.pausado_ate = 0
        self.mon.proxima.clear()
        self.assertEqual(self.mon.agendar_pendentes(), 2)
        for _ in range(100):
            if not self.mon.rodando:
                break
            time.sleep(0.05)
        self.assertEqual(sorted(self.status().values()), ["ok", "ok"])

    def test_erro_volta_sozinho_com_backoff(self):
        pid = self.add("0006001")
        ApiFalsa.estado["movs"] = [MOV1]
        ApiFalsa.estado["sequencia"] = ["500"]
        self.mon.executar()
        self.assertEqual(self.banco.obter(pid)["status"], "erro")
        self.assertEqual(self.mon.agendar_pendentes(), 0)         # ainda aguardando o backoff
        self.mon.proxima[pid] = 0
        self.assertEqual(self.mon.agendar_pendentes(), 1)
        for _ in range(100):
            if not self.mon.rodando:
                break
            time.sleep(0.05)
        self.assertEqual(self.banco.obter(pid)["status"], "ok")

    def test_fila_nao_descarta_pedido_durante_consulta(self):
        a = self.add("0007001", origem="0001")
        b = self.add("0007002", origem="0002", alias="trt1", j=5, tr="01")
        ApiFalsa.estado["movs"] = [MOV1]
        ApiFalsa.estado["atraso"] = 0.3
        self.mon.solicitar({a})
        time.sleep(0.1)
        self.assertTrue(self.mon.rodando)
        self.mon.solicitar({b})            # v0.1.0 descartava este pedido
        for _ in range(200):
            if not self.mon.rodando:
                break
            time.sleep(0.05)
        self.assertEqual(sorted(self.status().values()), ["ok", "ok"])

    def test_alerta_nao_se_perde_apos_erro_passageiro(self):
        pid = self.add("0008001")
        ApiFalsa.estado["movs"] = [MOV1]
        self.mon.executar()                                   # base
        ApiFalsa.estado["sequencia"] = ["500"]
        self.mon.executar()                                   # erro passageiro
        self.assertEqual(self.banco.obter(pid)["status"], "erro")
        ApiFalsa.estado["movs"] = [MOV1, MOV2]
        r = self.mon.executar()                               # v0.1.0 tratava isto como base e NÃO alertava
        self.assertEqual(r["novos"], 1)
        self.assertEqual(self.banco.listar()[0]["novos"], 1)


class TesteLote(Base):
    def test_lote_usa_uma_requisicao_por_tribunal(self):
        ids = [self.add(f"000900{i}", origem="0001") for i in range(4)]
        ApiFalsa.estado["movs"] = [MOV1]
        r = self.mon.executar()
        self.assertEqual(r["consultados"], 4)
        self.assertEqual(len(ApiFalsa.estado["caminhos"]), 1)   # 4 processos, 1 requisição
        self.assertIn("terms", ApiFalsa.estado["corpos"][0]["query"])
        self.assertTrue(all(s == "ok" for s in self.status().values()))

    def test_lote_distingue_ausente(self):
        a, b, c = (self.add(f"001000{i}") for i in range(3))
        ApiFalsa.estado["movs"] = [MOV1]
        ApiFalsa.estado["ausentes"] = {self.banco.obter(b)["numero_limpo"]}
        self.mon.executar()
        st = self.status()
        self.assertEqual((st[a], st[b], st[c]), ("ok", "nao_encontrado", "ok"))

    def test_lote_recusado_cai_para_individual(self):
        ids = [self.add(f"001100{i}") for i in range(3)]
        ApiFalsa.estado["movs"] = [MOV1]
        ApiFalsa.estado["sequencia"] = ["400"]       # o lote é recusado; as consultas individuais passam
        r = self.mon.executar()
        self.assertEqual(r["consultados"], 3)
        self.assertTrue(all(s == "ok" for s in self.status().values()))
        self.assertEqual(len(ApiFalsa.estado["caminhos"]), 1 + 3)

    def test_lote_possivelmente_truncado_cai_para_individual(self):
        ids = [self.add(f"001200{i}") for i in range(3)]
        ApiFalsa.estado["movs"] = [MOV1]
        orig = datajud.consultar_lote

        def lote_pequeno(numeros, alias, api_key, base_url, timeout=60, tamanho=100):
            return orig(numeros, alias, api_key, base_url, timeout, tamanho=2)  # 3 hits >= 2 -> truncado
        datajud.consultar_lote = lote_pequeno
        try:
            self.mon.executar()
        finally:
            datajud.consultar_lote = orig
        self.assertTrue(all(s == "ok" for s in self.status().values()))

    def test_lote_com_erro_marca_todos_do_bloco(self):
        ids = [self.add(f"001300{i}") for i in range(3)]
        ApiFalsa.estado["sequencia"] = ["500"]
        r = self.mon.executar()
        self.assertEqual(r["erros"], 3)

    def test_respeita_tamanho_do_lote(self):
        for i in range(25):
            self.add(f"00140{i:02d}")
        ApiFalsa.estado["movs"] = [MOV1]
        self.mon.executar()
        self.assertEqual(len(ApiFalsa.estado["caminhos"]), 3)    # 10 + 10 + 5


class TesteRespostaParcial(Base):
    """O que os PDFs do teste real revelaram: o mesmo processo ora aparecia, ora 'não encontrado'."""

    def test_lote_parcial_cai_para_individual_e_nao_gera_nao_encontrado(self):
        ids = [self.add(f"002000{i}") for i in range(4)]
        ApiFalsa.estado["movs"] = [MOV1]
        ApiFalsa.estado["sequencia"] = ["parcial"]            # só o lote vem incompleto
        r = self.mon.executar()
        self.assertEqual(r["consultados"], 4)
        self.assertTrue(all(s == "ok" for s in self.status().values()))
        self.assertEqual(len(ApiFalsa.estado["caminhos"]), 1 + 4)

    def test_individual_parcial_repete_e_recupera(self):
        pid = self.add("0021001")
        ApiFalsa.estado["movs"] = [MOV1]
        ApiFalsa.estado["sequencia"] = ["parcial", "ok"]
        self.mon.executar()
        self.assertEqual(self.banco.obter(pid)["status"], "ok")
        self.assertIn(15, self.esperas)

    def test_parcial_persistente_vira_erro_e_nao_cria_base(self):
        pid = self.add("0022001")
        ApiFalsa.estado["movs"] = [MOV1]
        ApiFalsa.estado["padrao"] = "parcial"
        r = self.mon.executar()
        p = self.banco.obter(pid)
        self.assertEqual(p["status"], "erro")                 # não "não encontrado"
        self.assertEqual(p["baseline_feito"], 0)              # histórico-base só nasce de resposta completa
        self.assertIn("parcial", p["mensagem"].lower())

    def test_ausente_no_lote_e_confirmado_individualmente(self):
        a, b, c = (self.add(f"002300{i}") for i in range(3))
        ApiFalsa.estado["movs"] = [MOV1]
        ApiFalsa.estado["ausentes_lote"] = {self.banco.obter(b)["numero_limpo"]}   # omitido só no lote
        self.mon.executar()
        self.assertEqual(list(self.status().values()), ["ok", "ok", "ok"])

    def test_ausencia_confirmada_vira_nao_encontrado(self):
        pid = self.add("0024001")
        self.add("0024002")
        ApiFalsa.estado["movs"] = [MOV1]
        ApiFalsa.estado["ausentes"] = {self.banco.obter(pid)["numero_limpo"]}
        self.mon.executar()
        p = self.banco.obter(pid)
        self.assertEqual(p["status"], "nao_encontrado")
        self.assertIn("confirmado", p["mensagem"])

    def test_processo_ja_encontrado_nao_e_rebaixado(self):
        pid = self.add("0025001")
        ApiFalsa.estado["movs"] = [MOV1]
        self.mon.executar()
        ApiFalsa.estado["ausentes"] = {self.banco.obter(pid)["numero_limpo"]}
        r = self.mon.executar()
        p = self.banco.obter(pid)
        self.assertEqual(p["status"], "ok")                   # v0.1.1 mostrava "não encontrado" aqui
        self.assertIn("Atenção", p["mensagem"])
        self.assertEqual(len(self.banco.movimentos(pid)), 1)  # dados anteriores preservados
        self.assertEqual(r["novos"], 0)

    def test_historico_antigo_que_aparece_tarde_nao_gera_alerta(self):
        pid = self.add("0026001")
        recente = {"codigo": 9, "nome": "Publicação", "dataHora": "2026-08-24T18:15:00.000Z"}
        ApiFalsa.estado["movs"] = [recente]
        self.mon.executar()                                    # base: só a movimentação recente
        antigos = [{"codigo": i, "nome": f"Antigo {i}", "dataHora": f"2012-03-{i:02d}T10:00:00.000Z"} for i in range(1, 6)]
        ApiFalsa.estado["movs"] = [recente] + antigos          # resposta completa revela 5 movimentos de 2012
        r = self.mon.executar()
        self.assertEqual(r["novos"], 0)                        # v0.1.1 alertava "407 novas" num caso assim
        self.assertEqual(self.banco.listar()[0]["novos"], 0)
        self.assertEqual(len(self.banco.movimentos(pid)), 6)
        novo = {"codigo": 10, "nome": "Decisão", "dataHora": "2026-09-01T09:00:00.000Z"}
        ApiFalsa.estado["movs"] = [recente, novo] + antigos
        self.assertEqual(self.mon.executar()["novos"], 1)      # movimentação realmente nova continua alertando

    def test_meta_da_resposta_vai_para_o_log(self):
        import logging
        registros = []

        class H(logging.Handler):
            def emit(self, r):
                registros.append(r.getMessage())
        h = H()
        logging.getLogger("monitor").addHandler(h)
        logging.getLogger("monitor").setLevel(logging.INFO)
        try:
            self.add("0027001")
            ApiFalsa.estado["movs"] = [MOV1]
            self.mon.executar()
        finally:
            logging.getLogger("monitor").removeHandler(h)
        self.assertTrue(any("timed_out=False" in m and "shards=5/5" in m for m in registros))


class TesteDjen(Base):
    def numero(self, pid):
        return self.banco.obter(pid)["numero_limpo"]

    def test_normalizar_item_no_formato_real(self):
        r = dje.normalizar_item(item_dje(767492809, "2026-10-26", "00454593720168190203",
                                         datadisponibilizacao="26/10/2026"))
        self.assertEqual((r["pub_id"], r["data_disp"], r["tipo_doc"]), ("767492809", "2026-10-26", "Decisão"))
        advs = json.loads(r["advogados"])
        self.assertEqual(len(advs), 2)                       # o mesmo advogado repetido aparece uma vez
        self.assertIn({"nome": "ADVOGADO EXEMPLO", "oab": "123456/RJ"}, advs)
        self.assertEqual(json.loads(r["destinatarios"])[1], {"nome": "PARTE EXEMPLO", "polo": "P"})
        self.assertEqual(r["cancelada"], 0)
        # data só no formato dd/mm/aaaa
        it = item_dje(1, "", "x", datadisponibilizacao="05/06/2025")
        it["data_disponibilizacao"] = None
        self.assertEqual(dje.normalizar_item(it)["data_disp"], "2025-06-05")

    def test_texto_html_vira_texto_simples(self):
        html = ("<html>\n<head><style>.x{color:red}</style></head><body><article><section><b>Execu&ccedil;&atilde;o</b></section>"
                "<section><table><tr><td>EXEQUENTE(S)</td><td>: COOPERATIVA</td></tr><tr><td>ADVOGADO(A)</td><td>: FELIPPE</td></tr></table></section>"
                "<p align=\"center\">ATO ORDINAT&Oacute;RIO</p><p>Providencie&nbsp;a parte, em 15 dias.</p></article></body></html>")
        r = dje.limpar_texto(html)
        self.assertNotIn("<", r)
        self.assertNotIn("color:red", r)
        self.assertIn("Execução", r)
        self.assertIn("EXEQUENTE(S) : COOPERATIVA", r)
        self.assertIn("ATO ORDINATÓRIO", r)
        self.assertIn("Providencie a parte, em 15 dias.", r)
        self.assertEqual(dje.limpar_texto("Texto  simples sem tags."), "Texto  simples sem tags.")
        self.assertEqual(dje.limpar_texto(None), "")

    def test_primeira_consulta_marca_antigas_como_vistas_e_recentes_como_novas(self):
        pid = self.add("0050001")
        n = self.numero(pid)
        DjenFalso.estado["itens"][n] = [item_dje(11, dias_atras(3), n), item_dje(10, dias_atras(200), n)]
        r = self.mon.executar(usar_datajud=False)
        self.assertEqual(r["publicacoes_novas"], 1)
        p = self.banco.listar()[0]
        self.assertEqual((p["pub_total"], p["pub_novas"], p["dje_status"]), (2, 1, "ok"))
        self.assertEqual(self.mon.executar(usar_datajud=False)["publicacoes_novas"], 0)  # nada repetido

    def test_publicacao_nova_gera_email_com_trecho_do_texto(self):
        pid = self.add("0050002")
        n = self.numero(pid)
        self.cfg.dados["email"].update({"ativo": True, "smtp_host": "x", "usuario": "u", "senha": "s",
                                        "destinatario": "d@e.f"})
        enviados = []
        orig = monitor_mod.mailer.enviar
        monitor_mod.mailer.enviar = lambda cfg, assunto, corpo: enviados.append((assunto, corpo))
        try:
            DjenFalso.estado["itens"][n] = [item_dje(20, dias_atras(100), n)]
            self.mon.executar(usar_datajud=False)               # base (antiga): sem e-mail
            self.assertEqual(enviados, [])
            texto = "Defiro a penhora. " + "palavra " * 200
            DjenFalso.estado["itens"][n].insert(0, item_dje(21, dias_atras(0), n, texto=texto))
            r = self.mon.executar(usar_datajud=False)
        finally:
            monitor_mod.mailer.enviar = orig
        self.assertEqual(r["email"], "enviado")
        assunto, corpo = enviados[0]
        self.assertIn("1 publicação(ões) no DJEN", assunto)
        self.assertIn("Disponibilizada em", corpo)
        self.assertIn("Defiro a penhora.", corpo)
        self.assertIn("…", corpo)                                # texto longo é abreviado
        self.assertIn("NÃO substitui", corpo)

    def test_ignora_publicacao_de_outro_processo(self):
        pid = self.add("0050003")
        n = self.numero(pid)
        DjenFalso.estado["itens"][n] = [item_dje(30, dias_atras(1), "99999999999999999999")]
        self.mon.executar(usar_datajud=False)
        self.assertEqual(self.banco.listar()[0]["pub_total"], 0)

    def test_publicacao_cancelada_nao_alerta(self):
        pid = self.add("0050004")
        n = self.numero(pid)
        DjenFalso.estado["itens"][n] = [item_dje(40, dias_atras(1), n, data_cancelamento="2026-11-01T10:00:00",
                                                 motivo_cancelamento="erro", ativo=False)]
        r = self.mon.executar(usar_datajud=False)
        self.assertEqual(r["publicacoes_novas"], 0)
        self.assertEqual(self.banco.publicacoes()[0]["cancelada"], 1)
        self.assertEqual(self.banco.listar()[0]["pub_novas"], 0)

    def test_paginacao_carrega_todas(self):
        pid = self.add("0050005")
        n = self.numero(pid)
        DjenFalso.estado["itens"][n] = [item_dje(100 + i, dias_atras(300 + i), n) for i in range(5)]
        DjenFalso.estado["tamanho_pagina"] = 2
        self.mon.executar(usar_datajud=False)
        self.assertEqual(self.banco.listar()[0]["pub_total"], 5)

    def test_paginacao_nao_suportada_nao_entra_em_loop(self):
        pid = self.add("0050006")
        n = self.numero(pid)
        DjenFalso.estado["itens"][n] = [item_dje(200 + i, dias_atras(300 + i), n) for i in range(5)]
        DjenFalso.estado["tamanho_pagina"] = 2
        DjenFalso.estado["ignora_pagina"] = True
        self.mon.executar(usar_datajud=False)
        self.assertEqual(self.banco.listar()[0]["pub_total"], 2)           # só a 1ª página; registrado em log
        self.assertLessEqual(len(DjenFalso.estado["caminhos"]), 3)

    def test_429_persistente_pausa_djen_sem_afetar_datajud(self):
        ids = [self.add(f"005100{i}", origem=f"000{i}") for i in range(3)]
        ApiFalsa.estado["movs"] = [MOV1]
        DjenFalso.estado["padrao"] = "429"
        r = self.mon.executar()
        self.assertTrue(all(s == "ok" for s in self.status().values()))      # DataJud normal
        self.assertGreater(self.mon.dje_pausado_ate, time.time() + 800)
        self.assertTrue(r["dje_aviso"])
        self.assertTrue(all(p["dje_status"] == "pendente" for p in self.banco.listar()))
        self.assertEqual(self.mon.agendar_pendentes(), 0)                     # respeita a pausa
        DjenFalso.estado["padrao"] = "ok"
        self.mon.dje_pausado_ate = 0
        self.assertEqual(self.mon.agendar_pendentes(), 3)                     # volta só pelo DJEN
        for _ in range(100):
            if not self.mon.rodando:
                break
            time.sleep(0.05)
        self.assertTrue(all(p["dje_status"] == "ok" for p in self.banco.listar()))

    def test_erro_do_djen_nao_afeta_datajud_e_tenta_de_novo(self):
        pid = self.add("0052001")
        ApiFalsa.estado["movs"] = [MOV1]
        DjenFalso.estado["sequencia"] = ["500"]
        r = self.mon.executar()
        self.assertEqual(self.banco.obter(pid)["status"], "ok")
        p = self.banco.obter(pid)
        self.assertEqual(p["dje_status"], "erro")
        self.assertEqual(r["dje_erros"], 1)
        self.assertEqual(self.mon.agendar_pendentes(), 0)                     # aguardando o backoff
        self.mon.proxima_dje[pid] = 0
        self.assertEqual(self.mon.agendar_pendentes(), 1)
        for _ in range(100):
            if not self.mon.rodando:
                break
            time.sleep(0.05)
        self.assertEqual(self.banco.obter(pid)["dje_status"], "ok")

    def test_acesso_negado_403_interrompe_so_o_djen(self):
        ids = [self.add(f"005300{i}", origem=f"000{i}") for i in range(3)]
        ApiFalsa.estado["movs"] = [MOV1]
        DjenFalso.estado["padrao"] = "403"
        r = self.mon.executar()
        self.assertEqual(len(DjenFalso.estado["caminhos"]), 1)                # não insiste nos demais
        self.assertIn("403", r["dje_aviso"])
        self.assertTrue(all(s == "ok" for s in self.status().values()))

    def test_resposta_invalida_vira_erro(self):
        pid = self.add("0054001")
        DjenFalso.estado["sequencia"] = ["lixo"]
        self.mon.executar(usar_datajud=False)
        self.assertEqual(self.banco.obter(pid)["dje_status"], "erro")

    def test_desativado_nao_consulta(self):
        self.add("0055001")
        ApiFalsa.estado["movs"] = [MOV1]
        self.cfg.dados["dje_ativo"] = False
        self.mon.executar()
        self.assertEqual(DjenFalso.estado["caminhos"], [])
        self.assertEqual(self.mon.agendar_pendentes(), 0)

    def test_processo_em_segredo_no_datajud_ainda_mostra_publicacoes(self):
        pid = self.add("0056001")
        n = self.numero(pid)
        ApiFalsa.estado["movs"] = None                                        # DataJud: não encontrado
        DjenFalso.estado["itens"][n] = [item_dje(60, dias_atras(2), n)]
        self.mon.executar()
        p = self.banco.obter(pid)
        self.assertEqual(p["status"], "nao_encontrado")
        self.assertEqual(self.banco.listar()[0]["pub_novas"], 1)


class TesteOab(Base):
    CH = "123456/RJ"

    def nova_oab(self, numero="123456", uf="RJ"):
        return self.banco.adicionar_oab(numero, uf, "Exemplo")

    def pub(self, pid, dias, numero="00012345620268190001", **kw):
        return item_dje(pid, dias_atras(dias), numero, **kw)

    def test_normaliza_e_cadastra_sem_duplicar(self):
        self.assertIsNotNone(self.nova_oab())
        self.assertIsNone(self.nova_oab())
        self.assertIsNotNone(self.nova_oab("123456", "SP"))       # mesma OAB em outra UF é outra
        self.assertEqual(len(self.banco.oabs()), 2)

    def test_primeira_consulta_so_traz_janela_recente(self):
        oid = self.nova_oab()
        DjenFalso.estado["oab"][self.CH] = [self.pub(1, 0), self.pub(2, 1), self.pub(3, 30), self.pub(4, 400)]
        r = self.mon.executar_oab()
        self.assertEqual(r["novas"], 2)                           # antigas nem são buscadas nem guardadas
        o = self.banco.oabs()[0]
        self.assertEqual((o["pub_total"], o["pub_novas"], o["status"]), (2, 2, "ok"))
        self.assertIn("dataDisponibilizacaoInicio=", DjenFalso.estado["caminhos"][0])
        self.assertEqual(self.mon.executar_oab()["novas"], 0)     # nada repetido

    def test_publicacao_nova_depois_gera_alerta_e_email(self):
        self.nova_oab()
        self.cfg.dados["email"].update({"ativo": True, "smtp_host": "x", "usuario": "u", "senha": "s",
                                        "destinatario": "d@e.f"})
        enviados = []
        orig = monitor_mod.mailer.enviar
        monitor_mod.mailer.enviar = lambda cfg, assunto, corpo: enviados.append((assunto, corpo))
        try:
            DjenFalso.estado["oab"][self.CH] = [self.pub(1, 1)]
            self.mon.executar_oab()
            enviados.clear()
            DjenFalso.estado["oab"][self.CH].insert(0, self.pub(9, 0, "00098765420268190001", texto="Cite-se. " * 100))
            r = self.mon.executar_oab()
        finally:
            monitor_mod.mailer.enviar = orig
        self.assertEqual(r["email"], "enviado")
        assunto, corpo = enviados[0]
        self.assertIn("1 publicação(ões) nova(s) nos filtros de pesquisa", assunto)
        self.assertIn("0009876-54.2026.8.19.0001", corpo)         # número formatado
        self.assertIn("…", corpo)
        self.assertIn("NÃO substitui", corpo)

    def test_paginacao_carrega_tudo(self):
        self.nova_oab()
        DjenFalso.estado["oab"][self.CH] = [self.pub(100 + i, 0, f"{i:07d}20268190001") for i in range(120)]
        r = self.mon.executar_oab()
        self.assertEqual(r["novas"], 120)
        self.assertEqual(len(DjenFalso.estado["caminhos"]), 3)    # 50 + 50 + 20

    def test_pagina_menor_que_a_pedida_tambem_funciona(self):
        self.nova_oab()
        DjenFalso.estado["max_pagina"] = 10                       # servidor ignora o 50 e devolve 10
        DjenFalso.estado["oab"][self.CH] = [self.pub(100 + i, 0, f"{i:07d}20268190001") for i in range(25)]
        self.assertEqual(self.mon.executar_oab()["novas"], 25)

    def test_pagina_vazia_com_total_maior_avisa_e_nao_avanca_janela(self):
        oid = self.nova_oab()
        DjenFalso.estado["vazia_na_pagina"] = 2
        DjenFalso.estado["oab"][self.CH] = [self.pub(100 + i, 0, f"{i:07d}20268190001") for i in range(80)]
        desde_antes = self.banco.obter_oab(oid)["desde"]
        self.mon.executar_oab()
        o = self.banco.obter_oab(oid)
        self.assertIn("Atenção", o["mensagem"])
        self.assertEqual(o["desde"], desde_antes)                 # repete o período na próxima

    def test_ignora_processo_ja_cadastrado_e_cancelada_e_repetida(self):
        pid = self.add("0070001")
        n = self.banco.obter(pid)["numero_limpo"]
        self.nova_oab()
        DjenFalso.estado["oab"][self.CH] = [
            self.pub(1, 0, n),                                                       # já cadastrado
            self.pub(2, 0, data_cancelamento="2026-11-01T10:00:00", ativo=False),     # cancelada
            self.pub(3, 0, texto="Texto igual"), self.pub(4, 0, texto="Texto igual"),  # repetida p/ outro destinatário
        ]
        r = self.mon.executar_oab()
        self.assertEqual(r["novas"], 1)
        o = self.banco.oabs()[0]
        self.assertEqual(o["pub_novas"], 1)

    def test_erro_429_e_retentativa_automatica(self):
        oid = self.nova_oab()
        DjenFalso.estado["padrao"] = "429"
        r = self.mon.executar_oab()
        self.assertTrue(r["aviso"])
        self.assertGreater(self.mon.dje_pausado_ate, time.time() + 800)
        self.assertEqual(self.banco.obter_oab(oid)["status"], "pendente")
        DjenFalso.estado["padrao"] = "ok"
        self.mon.dje_pausado_ate = 0
        self.assertEqual(self.mon.agendar_pendentes(), 1)
        for _ in range(100):
            if not self.mon.rodando:
                break
            time.sleep(0.05)
        self.assertEqual(self.banco.obter_oab(oid)["status"], "ok")

    def test_erro_500_marca_erro_e_tenta_de_novo(self):
        oid = self.nova_oab()
        DjenFalso.estado["sequencia"] = ["500"]
        self.mon.executar_oab()
        self.assertEqual(self.banco.obter_oab(oid)["status"], "erro")
        self.assertEqual(self.mon.agendar_pendentes(), 0)
        self.mon.proxima_oab[oid] = 0
        self.assertEqual(self.mon.agendar_pendentes(), 1)
        for _ in range(100):
            if not self.mon.rodando:
                break
            time.sleep(0.05)

    def test_remover_oab_apaga_publicacoes_e_marcar_vistas(self):
        oid = self.nova_oab()
        DjenFalso.estado["oab"][self.CH] = [self.pub(1, 0)]
        self.mon.executar_oab()
        self.banco.marcar_oab_vistas(oid)
        self.assertEqual(self.banco.oabs()[0]["pub_novas"], 0)
        self.assertEqual(len(self.banco.oab_publicacoes(oid)), 1)
        self.banco.remover_oab(oid)
        self.assertEqual(self.banco.oab_publicacoes(), [])

    def test_dje_desativado_nao_consulta_oab(self):
        self.nova_oab()
        self.cfg.dados["dje_ativo"] = False
        self.mon.executar_oab()
        self.assertEqual(DjenFalso.estado["caminhos"], [])


class TesteFiltrosDocumento(Base):
    CNPJ = "11222333000181"          # CNPJ de exemplo com dígitos verificadores válidos
    MASC = "11.222.333/0001-81"

    def pub(self, pid, dias, numero="00012345620268190001", **kw):
        return item_dje(pid, dias_atras(dias), numero, **kw)

    def test_validadores(self):
        self.assertTrue(dje.cnpj_valido(self.MASC))
        self.assertFalse(dje.cnpj_valido("42.873.828/0001-03"))
        self.assertFalse(dje.cnpj_valido("11111111111111"))
        self.assertTrue(dje.cpf_valido("529.982.247-25"))
        self.assertFalse(dje.cpf_valido("529.982.247-24"))
        self.assertEqual(dje.formas_documento("52998224725"), ["529.982.247-25", "52998224725"])
        with self.assertRaises(ValueError):
            dje.formas_documento("123")

    def test_documento_consulta_as_duas_formas_e_junta_sem_repetir(self):
        oid = self.banco.adicionar_oab(self.CNPJ, "", "Cliente X", "doc")
        DjenFalso.estado["texto"][self.MASC] = [self.pub(1, 0), self.pub(2, 1, "00022345620268190001", texto="B")]
        DjenFalso.estado["texto"][self.CNPJ] = [self.pub(2, 1, "00022345620268190001", texto="B"),
                                                self.pub(3, 0, "00032345620268190001", texto="C")]
        r = self.mon.executar_oab()
        self.assertEqual(r["novas"], 3)                          # id 2 veio nas duas formas: uma só vez
        formas = {c.split("texto=")[1].split("&")[0] for c in DjenFalso.estado["caminhos"]}
        self.assertEqual(len(formas), 2)

    def test_so_digitos_traz_o_que_a_mascara_nao_traz(self):
        self.banco.adicionar_oab(self.CNPJ, "", "", "doc")
        DjenFalso.estado["texto"][self.CNPJ] = [self.pub(7, 0)]
        self.assertEqual(self.mon.executar_oab()["novas"], 1)

    def test_filtro_por_texto_e_data_de_corte(self):
        self.banco.adicionar_oab("Condomínio Vivae", "", "", "texto")
        DjenFalso.estado["texto"]["Condomínio Vivae"] = [self.pub(1, 0), self.pub(2, 60)]
        self.assertEqual(self.mon.executar_oab()["novas"], 1)    # a de 60 dias fica fora da janela

    def test_email_descreve_o_filtro(self):
        self.banco.adicionar_oab(self.CNPJ, "", "Cliente X", "doc")
        self.cfg.dados["email"].update({"ativo": True, "smtp_host": "x", "usuario": "u", "senha": "s",
                                        "destinatario": "d@e.f"})
        enviados = []
        orig = monitor_mod.mailer.enviar
        monitor_mod.mailer.enviar = lambda cfg, assunto, corpo: enviados.append(corpo)
        try:
            DjenFalso.estado["texto"][self.MASC] = [self.pub(1, 0)]
            self.mon.executar_oab()
        finally:
            monitor_mod.mailer.enviar = orig
        self.assertIn("CPF/CNPJ 11.222.333/0001-81 — Cliente X", enviados[0])

    def test_migracao_de_oabs_sem_coluna_tipo(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            arq = Path(d) / "velho.db"
            c = sqlite3.connect(arq)
            c.executescript("CREATE TABLE oabs(id INTEGER PRIMARY KEY AUTOINCREMENT, numero TEXT NOT NULL, uf TEXT NOT NULL,"
                            "rotulo TEXT DEFAULT '', ativo INTEGER NOT NULL DEFAULT 1, criado_em TEXT NOT NULL, desde TEXT NOT NULL,"
                            "ultima_consulta TEXT, status TEXT NOT NULL DEFAULT 'pendente', mensagem TEXT DEFAULT '',"
                            "UNIQUE(numero, uf)); INSERT INTO oabs(numero,uf,criado_em,desde) VALUES('123456','RJ','x','2026-11-01');")
            c.commit(); c.close()
            banco = Banco(arq)
            self.assertEqual(banco.oabs()[0]["tipo"], "oab")
            banco.conn.close()


class TesteMigracao(unittest.TestCase):
    def test_banco_v010_recebe_coluna_e_preserva_base(self):
        with tempfile.TemporaryDirectory() as d:
            caminho = Path(d) / "p.db"
            c = sqlite3.connect(str(caminho))
            c.executescript("""
            CREATE TABLE processos(id INTEGER PRIMARY KEY AUTOINCREMENT, numero TEXT NOT NULL UNIQUE,
              numero_limpo TEXT NOT NULL, alias TEXT NOT NULL, apelido TEXT DEFAULT '', cliente TEXT DEFAULT '',
              pasta TEXT DEFAULT '', ativo INTEGER NOT NULL DEFAULT 1, criado_em TEXT NOT NULL,
              ultima_consulta TEXT, status TEXT NOT NULL DEFAULT 'pendente', mensagem TEXT DEFAULT '',
              classe TEXT DEFAULT '', orgao TEXT DEFAULT '', ultima_atualizacao TEXT DEFAULT '');
            CREATE TABLE movimentos(id INTEGER PRIMARY KEY AUTOINCREMENT, processo_id INTEGER NOT NULL,
              chave TEXT NOT NULL, grau TEXT DEFAULT '', codigo TEXT DEFAULT '', nome TEXT DEFAULT '',
              data_hora TEXT DEFAULT '', complementos TEXT DEFAULT '', visto INTEGER NOT NULL DEFAULT 0,
              detectado_em TEXT NOT NULL, UNIQUE(processo_id, chave));
            INSERT INTO processos(numero,numero_limpo,alias,criado_em,status) VALUES('a','1','tjrj','x','ok');
            INSERT INTO processos(numero,numero_limpo,alias,criado_em,status) VALUES('b','2','tjrj','x','pendente');
            INSERT INTO processos(numero,numero_limpo,alias,criado_em,status) VALUES('c','3','tjrj','x','erro');
            """)
            c.commit()
            c.close()
            banco = Banco(caminho)
            valores = {p["numero"]: p["baseline_feito"] for p in banco.listar()}
            self.assertEqual(valores, {"a": 1, "b": 0, "c": 0})
            colunas = {r["name"] for r in banco.conn.execute("PRAGMA table_info(processos)").fetchall()}
            self.assertTrue({"dje_status", "dje_consulta", "dje_mensagem", "dje_baseline"} <= colunas)
            self.assertEqual(banco.publicacoes(), [])                        # tabela criada em banco antigo
            Banco(caminho)  # reabrir não repete a migração


if __name__ == "__main__":
    unittest.main()
