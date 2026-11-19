"""Testes da v0.5.4: diagnóstico do DJEN (JSON bruto de um processo, só administrador, sem gravar nada)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dje  # noqa: E402
from datajud import ErroAPI  # noqa: E402
from test_equipe import Base  # noqa: E402

NUM = "0021000-84.2003.5.01.0006"
ITEM = {"id": 792049160, "numero_processo": "00210008420035010006", "texto": "<p>INTIMACAO  Fica V. Sa.</p>",
        "data_disponibilizacao": "2026-11-19", "tipoComunicacao": "Intimação", "nomeOrgao": "CAEX REEF",
        "siglaTribunal": "TRT1", "destinatarios": [{"nome": "COMISSAO DE CREDORES", "polo": "A"}],
        "campo_novo_da_api": "x"}


class Diagnostico(Base):
    def test_devolve_o_bruto_sem_alterar_e_o_normalizado(self):
        c = self.admin()
        with mock.patch.object(dje, "_get", return_value={"count": 1, "items": [ITEM]}) as g:
            st, d, _ = c.req("GET", f"/api/diagnostico/dje?numero={NUM}&inicio=2026-11-19&fim=2026-11-19")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["itens_brutos"][0], ITEM)                      # exatamente como chegou
        self.assertEqual(d["itens_normalizados"][0]["pub_id"], "792049160")
        self.assertIn("INTIMACAO", d["itens_normalizados"][0]["texto"])
        url = g.call_args[0][0]
        self.assertIn("numeroProcesso=00210008420035010006", url)
        self.assertIn("dataDisponibilizacaoInicio=2026-11-19", url)

    def test_nao_grava_publicacoes_no_banco(self):
        c = self.admin()
        with mock.patch.object(dje, "_get", return_value={"count": 1, "items": [ITEM]}):
            c.req("GET", f"/api/diagnostico/dje?numero={NUM}")
        self.assertEqual(c.req("GET", "/api/publicacoes")[1], [])

    def test_so_administrador(self):
        adm = self.admin()
        maria = self.comum(adm)
        self.assertEqual(maria.req("GET", f"/api/diagnostico/dje?numero={NUM}")[0], 403)
        self.assertEqual(self.novo().req("GET", f"/api/diagnostico/dje?numero={NUM}")[0], 401)

    def test_entradas_invalidas(self):
        c = self.admin()
        self.assertEqual(c.req("GET", "/api/diagnostico/dje?numero=123")[0], 400)
        self.assertEqual(c.req("GET", "/api/diagnostico/dje")[0], 400)
        self.assertEqual(c.req("GET", f"/api/diagnostico/dje?numero={NUM}&inicio=19/11/2026")[0], 400)

    def test_erro_da_api_vira_mensagem(self):
        c = self.admin()
        with mock.patch.object(dje, "_get", side_effect=ErroAPI("Sem conexão com a API do DJEN: x", transitorio=True)):
            st, d, _ = c.req("GET", f"/api/diagnostico/dje?numero={NUM}")
        self.assertEqual(st, 502)
        self.assertIn("Sem conexão", d["erro"])

    def test_resposta_fora_do_formato(self):
        c = self.admin()
        with mock.patch.object(dje, "_get", return_value={"outra": 1}):
            self.assertEqual(c.req("GET", f"/api/diagnostico/dje?numero={NUM}")[0], 502)

    def test_pagina_tem_o_painel(self):
        html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
        for tok in ("dg-go", "dg-copia", "/api/diagnostico/dje"):
            self.assertIn(tok, html)


if __name__ == "__main__":
    unittest.main()
