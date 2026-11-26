"""v0.8: painel de gestão (carga da equipe, carteira, parados, alertas, publicações do DJEN)."""
import datetime as dt
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402
import auth as auth_mod  # noqa: E402
import gestao  # noqa: E402
import test_equipe  # noqa: E402
from config import Config  # noqa: E402
from db import Banco  # noqa: E402
from monitor import Monitor  # noqa: E402

HOJE = dt.date(2026, 11, 26)


def d(n):
    return (HOJE - dt.timedelta(days=n)).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        app.DADOS = base
        app.CTX.config = Config(base / "c.json")
        app.CTX.config.dados["dje_ativo"] = False
        self.b = app.CTX.banco = Banco(base / "p.db")
        app.CTX.auth = auth_mod.Auth(self.b)
        app.CTX.monitor = Monitor(self.b, app.CTX.config, pausa_entre_consultas=0)
        app.CTX.sobre = {"versao": "t"}
        app.CTX.rede = app.CTX.proxy = False
        app.CTX.auth.criar("chefe", "Chefe", "", "admin", test_equipe.SENHA, trocar_senha=False)
        app.CTX.auth.criar("maria", "Maria", "", "usuario", test_equipe.SENHA, trocar_senha=False)
        self.maria_id = self.b._linhas("SELECT id FROM usuarios WHERE usuario='maria'")[0]["id"]
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.b.conn.close()
        self.tmp.cleanup()

    def proc(self, n, cliente="Sicoob", alias="api_publica_tjrj", monitorar=True, resp=None, status="ok", classe="Execução", **kw):
        num = "%07d-00.2026.8.19.0001" % n
        pid = self.b.adicionar(num, num.replace("-", "").replace(".", ""), alias, kw.get("apelido", ""), cliente, "", monitorar=monitorar)
        self.b.conn.execute("UPDATE processos SET status=?, responsavel_id=?, classe=? WHERE id=?", (status, resp, classe, pid))
        self.b.conn.commit()
        return pid

    def mov(self, pid, dia):
        self.b.conn.execute("INSERT INTO movimentos(processo_id,chave,nome,data_hora,detectado_em) VALUES(?,?,?,?,?)",
                            (pid, "k" + dia, "Despacho", dia + "T10:00:00.000Z", "2026-01-01T00:00:00Z"))
        self.b.conn.commit()

    def pub(self, pid, grupo, disp, sit="nova", detectado="2026-11-26T12:00:00Z", cancelada=0):
        self.b.conn.execute("INSERT INTO publicacoes(processo_id,pub_id,data_disp,detectado_em,situacao,grupo,cancelada) VALUES(?,?,?,?,?,?,?)",
                            (pid, "%s-%s-%d" % (pid, grupo, self.b.conn.execute("SELECT COUNT(*) FROM publicacoes").fetchone()[0]), disp, detectado, sit, grupo, cancelada))
        self.b.conn.commit()

    def evento(self, titulo, data, tipo="Prazo fatal", resp=None, sit="aberto", pid=None):
        t = self.b._linhas("SELECT id FROM tipos_evento WHERE nome=?", (tipo,))[0]["id"]
        self.b.conn.execute("INSERT INTO eventos(processo_id,tipo_id,titulo,data,responsavel_id,situacao) VALUES(?,?,?,?,?,?)",
                            (pid, t, titulo, data, resp, sit))
        self.b.conn.commit()

    def painel(self, **kw):
        return gestao.painel(self.b, None, hoje=HOJE, inicio_dia_utc="2026-11-26T03:00:00Z", **kw)


class TesteEquipe(Base):
    def test_carga_por_responsavel(self):
        p1 = self.proc(1, resp=self.maria_id)
        self.proc(2, resp=None)
        self.evento("vencido", d(2), resp=self.maria_id)
        self.evento("hoje", d(0), resp=self.maria_id)
        self.evento("daqui 3 dias", d(-3), resp=self.maria_id)
        self.evento("daqui 20 dias", d(-20), resp=self.maria_id)
        self.evento("concluído", d(5), resp=self.maria_id, sit="concluido")
        self.pub(p1, "g1", d(1))
        self.pub(p1, "g1", d(1))            # cópia do mesmo grupo: conta uma vez
        self.pub(p1, "g2", d(1), sit="analise")
        pe = {x["nome"]: x for x in self.painel()["equipe"]}
        m = pe["Maria"]
        self.assertEqual((m["processos"], m["abertos"], m["vencidos"], m["hoje"], m["prox7"]), (1, 4, 1, 1, 1))
        self.assertEqual((m["pub_novas"], m["pub_analise"]), (1, 1))
        self.assertEqual(pe[gestao.SEM_RESP]["processos"], 1)
        self.assertEqual(pe["Chefe"]["processos"] + pe["Chefe"]["abertos"], 0)    # usuário ativo sem carga aparece com zeros

    def test_processo_arquivado_nao_conta(self):
        pid = self.proc(1, resp=self.maria_id)
        self.b.conn.execute("UPDATE processos SET ativo=0 WHERE id=?", (pid,))
        self.b.conn.commit()
        eq = self.painel()["equipe"]
        self.assertEqual({x["nome"] for x in eq}, {"Chefe", "Maria"})            # só usuários ativos; sem "(sem responsável)"
        self.assertEqual(sum(x["processos"] for x in eq), 0)


class TesteCarteira(Base):
    def test_totais_e_agrupamentos(self):
        self.proc(1, cliente="Sicoob")
        self.proc(2, cliente="Sicoob", monitorar=False)
        self.proc(3, cliente="Vivae", alias="api_publica_trt1", classe="")
        self.proc(4, cliente="", alias="api_publica_tjrj")
        arq = self.proc(5)
        self.b.conn.execute("UPDATE processos SET ativo=0 WHERE id=?", (arq,))
        self.b.conn.commit()
        c = self.painel()["carteira"]
        self.assertEqual((c["ativos"], c["monitorados"], c["nao_monitorados"], c["arquivados"]), (4, 3, 1, 1))
        cli = {x["nome"]: x for x in c["por_cliente"]}
        self.assertEqual((cli["Sicoob"]["n"], cli["Sicoob"]["monitorados"]), (2, 1))
        self.assertIn("(sem cliente)", cli)
        self.assertEqual({x["nome"]: x["n"] for x in c["por_tribunal"]}, {"TJRJ": 3, "TRT1": 1})
        self.assertEqual(c["n_clientes"], 3)


class TesteParados(Base):
    def test_usa_a_data_mais_recente_entre_andamento_e_publicacao(self):
        a = self.proc(1)
        self.mov(a, d(200))
        b = self.proc(2)
        self.mov(b, d(200))
        self.pub(b, "g", d(10))               # publicação recente: não é parado
        c = self.proc(3)
        self.mov(c, d(95))
        e = self.proc(4)
        self.mov(e, d(30))
        p = self.painel(parados_dias=90)["parados"]
        self.assertEqual([x["dias"] for x in p["lista"]], [200, 95])
        self.assertEqual(p["total"], 2)
        self.assertEqual(self.painel(parados_dias=30)["parados"]["total"], 3)

    def test_sem_dados_nao_e_parado_e_nao_monitorado_fica_de_fora(self):
        self.proc(1)                                    # monitorado, consultado, mas sem nenhuma data: "sem dados"
        n = self.proc(2, monitorar=False)
        self.mov(n, d(500))                              # não monitorado: dado pode estar velho por falta de consulta
        e = self.proc(3, status="erro")
        self.mov(e, d(500))                              # nunca consultado com sucesso
        p = self.painel()["parados"]
        self.assertEqual((p["total"], p["sem_dados"]), (0, 1))

    def test_opcao_invalida_volta_ao_padrao(self):
        self.assertEqual(self.painel(parados_dias=7)["parados"]["dias"], gestao.PARADOS_PADRAO)


class TesteAlertas(Base):
    def test_prazos_fatais_falhas_e_filtros(self):
        self.evento("fatal vencido", d(1))
        self.evento("fatal em 5 dias", d(-5))
        self.evento("fatal em 30 dias", d(-30))
        self.evento("interno", d(0), tipo="Prazo interno")
        self.evento("fatal concluído", d(1), sit="concluido")
        e = self.proc(1, status="erro")
        self.b.conn.execute("UPDATE processos SET mensagem='CNJ fora do ar' WHERE id=?", (e,))
        self.proc(2, status="nao_encontrado")
        j = self.proc(3)
        self.b.conn.execute("UPDATE processos SET dje_status='erro', dje_mensagem='DJEN recusou' WHERE id=?", (j,))
        self.proc(4, status="erro", monitorar=False)   # não monitorado: não é falha de varredura
        self.b.conn.execute("INSERT INTO oabs(numero,uf,tipo,ativo,criado_em,desde,status,mensagem) VALUES('1','RJ','oab',1,'x','x','erro','sem resposta')")
        self.b.conn.commit()
        a = self.painel()["alertas"]
        self.assertEqual([(f["titulo"], f["atrasado"]) for f in a["prazos_fatais"]], [("fatal vencido", True), ("fatal em 5 dias", False)])
        self.assertEqual(a["falhas_total"], 3)
        motivos = {f["motivo"] for f in a["falhas"]}
        self.assertIn("CNJ fora do ar", motivos)
        self.assertIn("DJEN recusou", motivos)
        self.assertEqual(a["oabs_com_erro"][0]["oab"], "1/RJ")
        self.assertIn("executado", a["penhora"])


class TestePublicacoes(Base):
    def test_contagens_do_dia_e_pendencias(self):
        p = self.proc(1)
        self.pub(p, "g1", d(0), detectado="2026-11-26T12:00:00Z")                       # hoje, detectada hoje
        self.pub(p, "g2", d(3), sit="nova", detectado="2026-11-23T12:00:00Z")           # nova antiga
        self.pub(p, "g3", d(0), sit="tratada", detectado="2026-11-25T12:00:00Z")        # hoje, já tratada
        self.pub(p, "g4", d(0), cancelada=1)                                             # cancelada: ignorada
        self.pub(p, "g5", d(1), sit="analise", detectado="2026-11-25T12:00:00Z")
        r = self.painel()["publicacoes"]
        self.assertEqual(r["disponibilizadas_hoje"], 2)
        self.assertEqual(r["detectadas_hoje"], 1)
        self.assertEqual((r["nao_tratadas"], r["em_analise"]), (2, 1))
        self.assertEqual((r["mais_antiga_nao_tratada"], r["dias_mais_antiga"]), (d(3), 3))
        self.assertEqual(len(r["serie_7d"]), 7)
        self.assertEqual(r["serie_7d"][-1], {"dia": d(0), "n": 2})
        self.assertEqual(r["serie_7d"][-2]["n"], 1)

    def test_banco_vazio(self):
        r = self.painel()["publicacoes"]
        self.assertEqual((r["nao_tratadas"], r["mais_antiga_nao_tratada"], r["dias_mais_antiga"]), (0, "", 0))


class TesteRota(Base):
    def entrar(self, usuario):
        c = test_equipe.Cliente(self.srv.server_port)
        self.assertEqual(c.req("POST", "/api/login", {"usuario": usuario, "senha": test_equipe.SENHA})[0], 200)
        return c

    def test_admin_ve_e_usuario_comum_nao(self):
        self.proc(1)
        st, r, _ = self.entrar("chefe").req("GET", "/api/gestao?parados=60")
        self.assertEqual(st, 200)
        self.assertEqual((r["parados"]["dias"], r["carteira"]["ativos"]), (60, 1))
        self.assertEqual(self.entrar("maria").req("GET", "/api/gestao")[0], 403)
        self.assertEqual(test_equipe.Cliente(self.srv.server_port).req("GET", "/api/gestao")[0], 401)

    def test_parametro_invalido_nao_quebra(self):
        st, r, _ = self.entrar("chefe").req("GET", "/api/gestao?parados=abc")
        self.assertEqual((st, r["parados"]["dias"]), (200, gestao.PARADOS_PADRAO))


if __name__ == "__main__":
    unittest.main()
