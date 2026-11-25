"""v0.7.4: partes, advogados e dados editáveis do processo."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import dje  # noqa: E402
import partes  # noqa: E402
from agenda import ConflitoVersao, ErroAgenda  # noqa: E402

NUM = "32194991920268190001"
SICOOB = "COOPERATIVA DE CREDITO CLASSICA DO ESTADO DO RIO DE JANEIRO - SICOOB EMPRESAS RJ"
ADVS = [{"id": 1, "advogado": {"nome": "FELIPPE ALVAREZ DE SÁ", "numero_oab": "125449", "uf_oab": "RJ"}},
        {"id": 2, "advogado": {"nome": "ADRIANA REZENDE DE FRANÇA", "numero_oab": "87843", "uf_oab": "RJ"}}]
CAB = ("<b>Execução de Título Extrajudicial  Nº 3219499-19.2026.8.19.0001/RJ</b></br><b><table><tr><td>EXEQUENTE</td><td>: "
       + SICOOB + "</td></tr><tr><td>ADVOGADO(A)</td><td>: FELIPPE ALVAREZ DE SÁ (OAB RJ125449)</td></tr>"
       "<tr><td>ADVOGADO(A)</td><td>: ADRIANA REZENDE DE FRANÇA (OAB RJ087843)</td></tr></table></b></br>"
       "<p align=\"center\">SENTENÇA</p></br>Pelo exposto, DETERMINO o cancelamento da distribuição.")


def item(pub_id, data, tipo, texto, destinatarios, com_advs=True):
    return dje.normalizar_item({
        "id": pub_id, "data_disponibilizacao": data, "tipoComunicacao": tipo, "nomeOrgao": "7ª Vara Cível da Regional da Barra da Tijuca",
        "texto": texto, "numero_processo": NUM, "tipoDocumento": "Outros", "nomeClasse": "EXECUçãO DE TíTULO EXTRAJUDICIAL",
        "destinatarios": destinatarios, "destinatarioadvogados": ADVS if com_advs else []})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = db.Banco(Path(self.tmp.name) / "t.db")
        self.b.conn.execute("INSERT INTO oabs(numero,uf,tipo,criado_em,desde) VALUES('125449','RJ','oab','2026-01-01','2026-01-01')")
        self.b.conn.commit()
        self.pid = self.b.adicionar("3219499-19.2026.8.19.0001", NUM, "tjrj", "", "Sicoob Empresas RJ", "")

    def tearDown(self):
        self.b.conn.close()
        self.tmp.cleanup()

    def carrega_log_real(self):
        """As 3 publicações do log enviado pelo usuário (24/11 sentença, 23/10 ato ordinatório, 22/10 lista de distribuição)."""
        self.b.registrar_publicacoes(self.pid, [
            item("796009948", "2026-11-24", "Intimação", CAB, [{"nome": SICOOB, "polo": "A"}]),
            item("765278480", "2026-10-23", "Intimação", CAB, [{"nome": SICOOB, "polo": "A"}]),
            item("763685670", "2026-10-22", "Lista de distribuição", "Processo 3219499-19.2026.8.19.0001 distribuido para 7ª Vara.",
                 [{"nome": SICOOB, "polo": "A"}, {"nome": "EQUILIBRIO ESPACO TERAPEUTICO LTDA", "polo": "P"},
                  {"nome": "NARA DIAS BARRETO", "polo": "P"}])])

    def dados(self):
        return partes.obter_dados(self.b, self.pid)


class TesteLeituraAutomatica(Base):
    def test_log_real_acha_a_parte_contraria_e_o_lado_do_cliente(self):
        self.carrega_log_real()
        d = self.dados()
        por_nome = {p["nome"]: p for p in d["partes"]}
        self.assertEqual(len(por_nome), 3)
        self.assertEqual(por_nome[SICOOB]["lado"], "cliente")
        self.assertEqual(por_nome["EQUILIBRIO ESPACO TERAPEUTICO LTDA"]["lado"], "contraria")
        self.assertEqual(por_nome["NARA DIAS BARRETO"]["polo"], "P")

    def test_advogados_e_nossa_oab_marcada_como_cliente(self):
        self.carrega_log_real()
        advs = {a["oab"]: a for a in self.dados()["advogados"]}
        self.assertEqual(advs["125449/RJ"]["lado"], "cliente")
        self.assertEqual(advs["87843/RJ"]["lado"], "cliente")

    def test_so_intimacao_so_o_cliente_aparece(self):
        """Sem a lista de distribuição, só o cliente é conhecido (limite real da fonte)."""
        self.b.registrar_publicacoes(self.pid, [item("1", "2026-11-24", "Intimação", CAB, [{"nome": SICOOB, "polo": "A"}])])
        nomes = [p["nome"] for p in self.dados()["partes"]]
        self.assertEqual(nomes, [SICOOB])

    def test_cabecalho_do_texto_traz_o_polo_passivo(self):
        texto = CAB.replace("</table>", "<tr><td>EXECUTADO</td><td>: FULANO DE TAL LTDA</td></tr></table>")
        self.b.registrar_publicacoes(self.pid, [item("2", "2026-11-24", "Intimação", texto, [{"nome": SICOOB, "polo": "A"}])])
        por_nome = {p["nome"]: p for p in self.dados()["partes"]}
        self.assertEqual(por_nome["FULANO DE TAL LTDA"]["polo"], "P")
        self.assertEqual(por_nome["FULANO DE TAL LTDA"]["lado"], "contraria")

    def test_lado_pelo_nome_do_cliente_quando_nao_ha_oab(self):
        self.b.conn.execute("DELETE FROM oabs")
        self.b.conn.commit()
        self.b.registrar_publicacoes(self.pid, [item("3", "2026-10-22", "Lista de distribuição", "x",
                                                      [{"nome": SICOOB, "polo": "A"}, {"nome": "BELTRANO SA", "polo": "P"}], com_advs=False)])
        por_nome = {p["nome"]: p for p in self.dados()["partes"]}
        self.assertEqual(por_nome[SICOOB]["lado"], "cliente")   # "Sicoob Empresas RJ" está no nome da parte
        self.assertEqual(por_nome["BELTRANO SA"]["lado"], "contraria")

    def test_sem_pista_do_cliente_o_lado_fica_vazio(self):
        self.b.conn.execute("DELETE FROM oabs")
        self.b.conn.execute("UPDATE processos SET cliente='' WHERE id=?", (self.pid,))
        self.b.conn.commit()
        self.b.registrar_publicacoes(self.pid, [item("4", "2026-10-22", "Lista de distribuição", "x",
                                                      [{"nome": "A SA", "polo": "A"}, {"nome": "B SA", "polo": "P"}], com_advs=False)])
        self.assertEqual({p["lado"] for p in self.dados()["partes"]}, {""})
        r = partes.resumo(self.dados()["partes"])
        self.assertEqual((r["ativo"], r["passivo"]), (["A SA"], ["B SA"]))

    def test_publicacao_cancelada_nao_gera_parte(self):
        it = item("5", "2026-10-22", "Lista de distribuição", "x", [{"nome": "FANTASMA SA", "polo": "P"}])
        it["cancelada"] = 1
        self.b.registrar_publicacoes(self.pid, [it])
        self.assertEqual(self.dados()["partes"], [])


class TesteEdicao(Base):
    def corpo(self, d, **extra):
        c = {"versao": d["processo"]["versao"], "apelido": d["processo"]["apelido"], "cliente": d["processo"]["cliente"],
             "pasta": "", "vara": "", "comarca": "", "tipo_acao": "", "partes": d["partes"], "advogados": d["advogados"]}
        c.update(extra)
        return c

    def test_editar_inverter_mover_incluir_e_excluir(self):
        self.carrega_log_real()
        d = self.dados()
        ps = [dict(p) for p in d["partes"]]
        for p in ps:
            if p["nome"] == "NARA DIAS BARRETO":
                p["lado"] = "cliente"        # litisconsorte passiva movida para o nosso lado
            if p["nome"].startswith("EQUILIBRIO"):
                p["polo"] = "A"              # polo corrigido
        ps = [p for p in ps if p["nome"] != SICOOB] + [{"nome": "TERCEIRA PARTE LTDA", "polo": "P", "lado": "contraria"}]
        partes.salvar(self.b, self.pid, self.corpo(d, partes=ps, vara="7ª Vara Cível Regional da Barra", comarca="Capital",
                                                   tipo_acao="Execução"))
        d2 = self.dados()
        por = {p["nome"]: p for p in d2["partes"]}
        self.assertNotIn(SICOOB, por)
        self.assertEqual(por["NARA DIAS BARRETO"]["lado"], "cliente")
        self.assertEqual(por["EQUILIBRIO ESPACO TERAPEUTICO LTDA"]["polo"], "A")
        self.assertEqual(por["TERCEIRA PARTE LTDA"]["origem"], "manual")
        self.assertEqual((d2["processo"]["vara"], d2["processo"]["comarca"], d2["processo"]["tipo_acao"]),
                         ("7ª Vara Cível Regional da Barra", "Capital", "Execução"))
        self.assertEqual(d2["processo"]["versao"], d["processo"]["versao"] + 1)

    def test_leitura_automatica_nao_desfaz_edicao_nem_recria_excluida(self):
        self.carrega_log_real()
        d = self.dados()
        ps = [dict(p, lado="cliente") if p["nome"].startswith("NARA") else dict(p) for p in d["partes"] if p["nome"] != SICOOB]
        partes.salvar(self.b, self.pid, self.corpo(d, partes=ps))
        # chega publicação nova: a leitura automática roda de novo
        self.b.registrar_publicacoes(self.pid, [item("9", "2026-11-25", "Intimação", CAB, [{"nome": SICOOB, "polo": "A"}])])
        por = {p["nome"]: p for p in self.dados()["partes"]}
        self.assertNotIn(SICOOB, por)                         # excluída não volta
        self.assertEqual(por["NARA DIAS BARRETO"]["lado"], "cliente")   # edição preservada

    def test_conflito_de_versao(self):
        d = self.dados()
        partes.salvar(self.b, self.pid, self.corpo(d, apelido="Primeiro"))
        with self.assertRaises(ConflitoVersao):
            partes.salvar(self.b, self.pid, self.corpo(d, apelido="Segundo"))   # versão velha

    def test_validacoes(self):
        d = self.dados()
        for ruim in ({"partes": [{"nome": "", "polo": "A", "lado": ""}]},
                     {"partes": [{"nome": "X", "polo": "Z", "lado": ""}]},
                     {"partes": [{"nome": "X", "polo": "A", "lado": "outro"}]},
                     {"partes": [{"nome": "Ação SA", "polo": "A", "lado": ""}, {"nome": "acao sa", "polo": "P", "lado": ""}]},
                     {"advogados": [{"nome": "Dr", "oab": "abc", "polo": "", "lado": ""}]},
                     {"vara": "x" * 161}, {"partes": [{"nome": "n%d" % i, "polo": "", "lado": ""} for i in range(41)]}):
            with self.assertRaises(ErroAgenda, msg=str(ruim)[:60]):
                partes.salvar(self.b, self.pid, self.corpo(d, **ruim))
        self.assertEqual(self.dados()["processo"]["versao"], d["processo"]["versao"])   # nada foi gravado

    def test_restaurar_volta_ao_automatico(self):
        self.carrega_log_real()
        d = self.dados()
        partes.salvar(self.b, self.pid, self.corpo(d, partes=[{"nome": "SÓ ESTA", "polo": "A", "lado": "cliente"}], vara="Outra"))
        partes.restaurar_automatico(self.b, self.pid)
        d2 = self.dados()
        self.assertEqual(len(d2["partes"]), 3)
        self.assertEqual(d2["processo"]["vara"], "")
        self.assertIn(SICOOB, [p["nome"] for p in d2["partes"]])

    def test_advogado_manual_com_oab_valida(self):
        d = self.dados()
        partes.salvar(self.b, self.pid, self.corpo(d, advogados=[{"nome": "Dr. Contrário", "oab": "RJ 555", "polo": "", "lado": "contraria"}]))
        a = self.dados()["advogados"][0]
        self.assertEqual((a["oab"], a["lado"]), ("555/RJ", "contraria"))


class TesteExibicao(Base):
    def test_enriquecer_processos_e_publicacoes(self):
        self.carrega_log_real()
        lista = partes.enriquecer(self.b, self.b.listar())
        p = lista[0]
        self.assertEqual(p["resumo_partes"]["cliente"], [SICOOB])
        self.assertEqual(sorted(p["resumo_partes"]["contraria"]), ["EQUILIBRIO ESPACO TERAPEUTICO LTDA", "NARA DIAS BARRETO"])
        self.assertEqual(p["vara_exib"], "7ª Vara Cível da Regional da Barra da Tijuca")
        self.assertEqual(p["tipo_exib"], "Execução de título extrajudicial")   # caixa do DJEN normalizada
        pubs = partes.enriquecer_publicacoes(self.b, self.b.publicacoes())
        self.assertTrue(all(x["resumo_partes"]["contraria"] for x in pubs))
        self.assertEqual(pubs[0]["tipo_exib"], "Execução de título extrajudicial")

    def test_titulo_classe(self):
        self.assertEqual(partes.titulo_classe("EXECUçãO DE TíTULO EXTRAJUDICIAL"), "Execução de título extrajudicial")
        self.assertEqual(partes.titulo_classe("Procedimento Comum Cível"), "Procedimento Comum Cível")
        self.assertEqual(partes.titulo_classe(""), "")

    def test_migracao_le_banco_antigo_uma_vez(self):
        self.carrega_log_real()
        self.b.conn.execute("DELETE FROM processo_partes")
        self.b.conn.execute("DELETE FROM partes_meta")
        self.b.conn.commit()
        partes.migrar(self.b)
        self.assertEqual(len(self.dados()["partes"]), 3)
        self.b.conn.execute("DELETE FROM processo_partes")
        self.b.conn.commit()
        partes.migrar(self.b)          # já migrado: não relê
        self.assertEqual(self.dados()["partes"], [])


if __name__ == "__main__":
    unittest.main()
