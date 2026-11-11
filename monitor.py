"""Consulta ao DataJud: fila de processos, ritmo controlado, tratamento de falhas e e-mail.

Princípios (v0.1.1):
  - Pedidos de consulta entram numa FILA; nunca são descartados por haver outra consulta rodando.
  - Falha em um processo (timeout, queda de conexão, 429...) NUNCA interrompe os demais.
  - Erro 429: espera (respeitando Retry-After) e tenta de novo; se persistir, pausa o ritmo e deixa
    o restante na fila para nova tentativa automática, em vez de marcar tudo como erro.
  - Processos do mesmo tribunal são consultados em lote (menos requisições).
"""
import datetime as dt
import logging
import threading
import time

import datajud
import dje
import mailer
from db import inicio_do_dia_utc

log = logging.getLogger("monitor")

AVISO_RODAPE = ("Aviso: o DataJud é uma base de metadados, com defasagem em relação ao tribunal, e o DJEN pode "
                "não refletir todas as comunicações. Este alerta NÃO substitui intimações, publicações oficiais "
                "nem o controle de prazos: confira o teor nos autos e conte os prazos pelas regras aplicáveis.")

BACKOFF_MIN = (2, 5, 15, 30, 60)  # minutos entre novas tentativas automáticas de um processo com erro


class LimiteExcedido(Exception):
    pass


class Monitor:
    def __init__(self, banco, config, pausa_entre_consultas=3.0, tamanho_lote=10,
                 espera_429=(30, 90, 180), pausa_limite=900, espera_parcial=(15, 45)):
        self.espera_parcial = espera_parcial
        self.banco = banco
        self.config = config
        self.pausa = pausa_entre_consultas
        self.tamanho_lote = tamanho_lote
        self.espera_429 = espera_429
        self.pausa_limite = pausa_limite
        self._lock = threading.Lock()
        self.fila = set()
        self.rodando = False
        self.progresso = ""
        self.resumo = None
        self.ultimo_ciclo = None   # epoch do fim do último ciclo
        self.pausado_ate = 0.0     # epoch: não consultar antes disso (após 429 persistente)
        self.proxima = {}          # id -> epoch da próxima tentativa automática
        self.tentativas = {}       # id -> nº de falhas seguidas
        self._ultima_req = 0.0
        self._ultima_req_dje = 0.0
        self.fila_dje = set()      # processos a consultar só no DJEN (retentativas)
        self.fila_oab = set()      # OABs a consultar no DJEN
        self.proxima_oab = {}
        self.tentativas_oab = {}
        self.resumo_oab = None
        self.fila_desc = {}        # oab_id -> dias (descoberta de processos pela OAB)
        self.ultimo_bloco = 0.0    # epoch do início do último bloco da varredura diária
        self.ultimo_filtros = None # epoch da última rodada dos filtros (OAB/CPF/texto)
        self.dje_pausado_ate = 0.0
        self.proxima_dje = {}
        self.tentativas_dje = {}
        self._dormir = time.sleep  # substituível em testes

    def _ha_trabalho(self):
        return bool(self.fila or self.fila_dje or self.fila_oab or self.fila_desc)

    # ------------------------------------------------------------------ fila
    def solicitar(self, ids=None, forcar=False):
        """Põe processos na fila (todos os ativos se ids=None) e garante que o trabalhador rode."""
        alvo = {p["id"] for p in self.banco.ativos(ids)}
        with self._lock:
            if forcar:
                self.pausado_ate = 0.0
                self.dje_pausado_ate = 0.0
            self.fila |= alvo
            iniciar = self._ha_trabalho() and not self.rodando
            if iniciar:
                self.rodando = True
        if iniciar:
            threading.Thread(target=self._trabalhar, daemon=True).start()
        return len(alvo)

    def solicitar_dje(self, ids):
        """Fila só do DJEN (para reenviar processos cuja consulta ao DJEN falhou)."""
        alvo = {p["id"] for p in self.banco.ativos(ids)}
        with self._lock:
            self.fila_dje |= alvo
            iniciar = self._ha_trabalho() and not self.rodando
            if iniciar:
                self.rodando = True
        if iniciar:
            threading.Thread(target=self._trabalhar, daemon=True).start()
        return len(alvo)

    def solicitar_oab(self, ids=None, forcar=False):
        """Põe OABs na fila (todas as ativas se ids=None)."""
        alvo = {o["id"] for o in self.banco.oabs_ativas(ids)}
        with self._lock:
            if forcar:
                self.dje_pausado_ate = 0.0
            self.fila_oab |= alvo
            iniciar = self._ha_trabalho() and not self.rodando
            if iniciar:
                self.rodando = True
        if iniciar:
            threading.Thread(target=self._trabalhar, daemon=True).start()
        return len(alvo)

    def solicitar_descoberta(self, oid, dias=365):
        """Busca no DJEN os processos citados nos últimos `dias` dias por um filtro de OAB (descoberta de processos ativos)."""
        if not self.banco.obter_oab(oid):
            return 0
        with self._lock:
            self.fila_desc[oid] = int(dias)
            self.dje_pausado_ate = 0.0
            iniciar = self._ha_trabalho() and not self.rodando
            if iniciar:
                self.rodando = True
        self.banco.registrar_descoberta_status(oid, "na fila", "")
        if iniciar:
            threading.Thread(target=self._trabalhar, daemon=True).start()
        return 1

    # ------------------------------------------------------- varredura diária em blocos
    def _cfg_varredura(self):
        return self.config.dados.get("varredura") or {}

    def varredura_ativa(self):
        return bool(self._cfg_varredura().get("ativa"))

    @staticmethod
    def _hhmm(texto, padrao):
        try:
            h, m = (int(x) for x in str(texto).split(":"))
            return dt.time(h, m)
        except ValueError:
            return padrao

    def na_janela(self, agora=None):
        v = self._cfg_varredura()
        a = (agora or dt.datetime.now()).time()
        return self._hhmm(v.get("inicio"), dt.time(7, 0)) <= a < self._hhmm(v.get("fim"), dt.time(20, 0))

    def candidatos_hoje(self, agora=None):
        """Ids ainda não consultados hoje, na ordem de prioridade (nunca consultados primeiro)."""
        t = time.time()
        return [p["id"] for p in self.banco.candidatos_varredura(inicio_do_dia_utc(agora))
                if self.proxima.get(p["id"], 0) <= t]

    def iniciar_bloco_se_devido(self, agora=None):
        """Chamado pelo agendador. Enfileira o próximo bloco da varredura diária, se estiver na janela de horário, se já
        passou o intervalo desde o último bloco e se há processos não consultados hoje. Devolve quantos foram enfileirados."""
        if not self.varredura_ativa() or self.rodando or not self.na_janela(agora):
            return 0
        v = self._cfg_varredura()
        if time.time() < self.pausado_ate or time.time() - self.ultimo_bloco < int(v.get("intervalo_min", 30)) * 60:
            return 0
        ids = self.candidatos_hoje(agora)[:int(v.get("bloco", 100))]
        if not ids:
            return 0
        self.ultimo_bloco = time.time()
        log.info("Varredura diária: bloco de %d processo(s)", len(ids))
        return self.solicitar(set(ids))

    def resumo_varredura(self, agora=None):
        v = self._cfg_varredura()
        ativos = len(self.banco.ativos())
        feitos = self.banco.consultados_desde(inicio_do_dia_utc(agora))
        intervalo = int(v.get("intervalo_min", 30)) or 30
        ini, fim = self._hhmm(v.get("inicio"), dt.time(7, 0)), self._hhmm(v.get("fim"), dt.time(20, 0))
        minutos = (fim.hour * 60 + fim.minute) - (ini.hour * 60 + ini.minute)
        blocos = max(0, minutos // intervalo)
        return {"ativa": self.varredura_ativa(), "ativos": ativos, "feitos_hoje": feitos,
                "restantes": max(0, ativos - feitos), "bloco": int(v.get("bloco", 100)), "intervalo_min": intervalo,
                "inicio": v.get("inicio"), "fim": v.get("fim"), "na_janela": self.na_janela(agora),
                "capacidade_dia": blocos * int(v.get("bloco", 100)),
                "proximo_bloco": (self.ultimo_bloco + intervalo * 60) if self.ultimo_bloco else None}

    def agendar_pendentes(self):
        """Chamado pelo agendador: recoloca na fila processos pendentes/com erro cuja espera acabou."""
        agora = time.time()
        if self.rodando:
            return 0
        n = 0
        if agora >= self.pausado_ate:
            ids = {p["id"] for p in self.banco.ativos()
                   if p["status"] in ("pendente", "erro") and self.proxima.get(p["id"], 0) <= agora
                   and (p["status"] == "erro" or p["id"] in self.proxima or not self.varredura_ativa())}
            if ids:
                n += self.solicitar(ids)
        if (not self.rodando and self.config.dados.get("dje_ativo", True) and agora >= self.dje_pausado_ate):
            ids = {p["id"] for p in self.banco.ativos()
                   if p["dje_status"] in ("pendente", "erro") and self.proxima_dje.get(p["id"], 0) <= agora
                   and (p["dje_status"] == "erro" or p["id"] in self.proxima_dje or not self.varredura_ativa())}
            if ids:
                n += self.solicitar_dje(ids)
        if (not self.rodando and self.config.dados.get("dje_ativo", True) and agora >= self.dje_pausado_ate):
            ids = {o["id"] for o in self.banco.oabs_ativas()
                   if o["status"] in ("pendente", "erro") and self.proxima_oab.get(o["id"], 0) <= agora}
            if ids:
                n += self.solicitar_oab(ids)
        return n

    def _trabalhar(self):
        try:
            while True:
                with self._lock:
                    if not self._ha_trabalho():
                        self.rodando = False
                        return
                    ids = set(self.fila)
                    self.fila.clear()
                    ids_dje = set(self.fila_dje) - ids
                    self.fila_dje.clear()
                    ids_oab = set(self.fila_oab)
                    self.fila_oab.clear()
                    desc = dict(self.fila_desc)
                    self.fila_desc.clear()
                if ids:
                    self.executar(ids)
                if ids_dje:
                    self.executar(ids_dje, usar_datajud=False)
                if ids_oab:
                    self.executar_oab(ids_oab)
                for oid, dias in desc.items():
                    self.executar_descoberta(oid, dias)
        except Exception:  # noqa: BLE001 - rede de segurança: o trabalhador nunca deve morrer calado
            log.exception("Falha inesperada no trabalhador de consultas")
            with self._lock:
                self.rodando = False

    # ----------------------------------------------------------------- ciclo
    def executar(self, ids=None, usar_datajud=True, usar_dje=True):
        """Consulta os processos indicados (todos os ativos se None). Devolve o resumo do ciclo."""
        processos = self.banco.ativos(ids)
        total = len(processos) if usar_datajud else 0
        ctx = {"feitos": 0, "total": total, "novos": [], "erros": 0, "interrompido": None, "feitos_ids": set(),
               "pubs": [], "dje_erros": 0, "dje_feitos": 0, "dje_aviso": ""}
        grupos = {}
        for p in (processos if usar_datajud else []):
            alias = self.config.dados["endpoints_override"].get(p["alias"], p["alias"])
            grupos.setdefault(alias, []).append(p)
        log.info("Ciclo iniciado: %d processo(s) em %d tribunal(is)", total, len(grupos))
        try:
            for alias, lista in grupos.items():
                for i in range(0, len(lista), self.tamanho_lote):
                    if ctx["interrompido"]:
                        break
                    bloco = lista[i:i + self.tamanho_lote]
                    self.progresso = f"{ctx['feitos']}/{total}"
                    try:
                        self._bloco(alias, bloco, ctx)
                    except Exception as e:  # noqa: BLE001 - um bloco com defeito não derruba o ciclo
                        log.exception("Erro inesperado ao consultar %s", alias)
                        for p in bloco:
                            if p["id"] not in ctx["feitos_ids"]:
                                self._falha(p, f"Erro inesperado: {e}", ctx)
        finally:
            self.progresso = ""
        # o que não foi alcançado por interrupção segue pendente, com aviso e nova tentativa automática
        if ctx["interrompido"]:
            tipo, msg = ctx["interrompido"]
            restantes = [p for p in processos if p["id"] not in ctx["feitos_ids"]]
            self._adiar(restantes, ctx, msg, limite=(tipo == "limite"))
            log.warning("Ciclo interrompido (%s): %s — %d processo(s) não alcançado(s)", tipo, msg, len(restantes))
        if usar_dje and self.config.dados.get("dje_ativo", True):
            self._dje(processos, ctx)
        novos_por_proc = [(p, n) for p, n in ctx["novos"] if n]
        total_novos = sum(len(n) for _, n in novos_por_proc)
        pubs_por_proc = [(p, n) for p, n in ctx["pubs"] if n]
        total_pubs = sum(len(n) for _, n in pubs_por_proc)
        email = "desativado"
        cfg_email = self.config.dados["email"]
        if cfg_email.get("ativo") and (novos_por_proc or pubs_por_proc):
            partes = []
            if total_pubs:
                partes.append(f"{total_pubs} publicação(ões) no DJEN")
            if total_novos:
                partes.append(f"{total_novos} movimentação(ões) no DataJud")
            try:
                mailer.enviar(cfg_email, "[Monitor de Processos] " + " e ".join(partes),
                              self.montar_email(novos_por_proc, pubs_por_proc))
                email = "enviado"
            except Exception as e:  # noqa: BLE001
                email = f"falhou: {e}"
                log.warning("Falha ao enviar e-mail: %s", e)
        elif cfg_email.get("ativo"):
            email = "sem novidades"
        pendentes_restantes = ctx["total"] - ctx["feitos"]
        self.resumo = {"consultados": ctx["feitos"], "novos": total_novos, "erros": ctx["erros"],
                       "pendentes": max(0, pendentes_restantes), "email": email,
                       "aviso": ctx["interrompido"][1] if ctx["interrompido"] else "",
                       "publicacoes_novas": total_pubs, "dje_consultados": ctx["dje_feitos"],
                       "dje_erros": ctx["dje_erros"], "dje_aviso": ctx["dje_aviso"]}
        self.ultimo_ciclo = time.time()
        log.info("Ciclo concluído: %s", self.resumo)
        return self.resumo

    # ------------------------------------------------------------------ OAB
    def executar_oab(self, ids=None):
        """Consulta o DJEN pelas OABs cadastradas e alerta só publicações NOVAS de processos não cadastrados."""
        oabs = self.banco.oabs_ativas(ids)
        resumo = {"consultadas": 0, "erros": 0, "novas": 0, "aviso": "", "email": "desativado"}
        if not self.config.dados.get("dje_ativo", True):
            resumo["aviso"] = "DJEN desativado em Configurações."
            self.resumo_oab = resumo
            return resumo
        base = self.config.dados.get("dje_base_url", dje.BASE_URL)
        novas_por_oab = []
        try:
            for i, o in enumerate(oabs, 1):
                if time.time() < self.dje_pausado_ate:
                    resumo["aviso"] = "DJEN em pausa por limite de requisições; nova tentativa automática."
                    break
                self.progresso = f"OAB {i}/{len(oabs)}"
                try:
                    itens, truncado = self._com_espera(
                        f"filtro {dje.descrever_filtro(o['tipo'], o['numero'], o['uf'])}",
                        lambda o=o: dje.consultar_busca(o["tipo"], o["numero"], o["uf"], base, desde=o["desde"]),
                        canal="dje")
                except LimiteExcedido as e:
                    self.dje_pausado_ate = time.time() + self.pausa_limite
                    resumo["aviso"] = str(e)
                    break
                except datajud.ErroAPI as e:
                    self._falha_oab(o, str(e), resumo)
                    if e.codigo in (401, 403):
                        resumo["aviso"] = str(e)
                        break
                    continue
                except Exception as e:  # noqa: BLE001
                    log.exception("Erro inesperado na consulta por OAB")
                    self._falha_oab(o, f"Erro inesperado: {e}", resumo)
                    continue
                novas = self.banco.registrar_oab_pubs(o["id"], itens)
                aviso = ("Atenção: não foi possível carregar todas as publicações do período (limite da API); "
                         "pode haver outras. Confira no portal do DJEN." if truncado else "")
                # só avança a janela quando a carga foi completa; se truncou, a próxima consulta repete o período
                self.banco.registrar_oab_status(o["id"], "ok", aviso, avancar_desde=not truncado)
                self.tentativas_oab.pop(o["id"], None)
                self.proxima_oab.pop(o["id"], None)
                resumo["consultadas"] += 1
                if novas:
                    novas_por_oab.append((o, novas))
        finally:
            self.progresso = ""
        resumo["novas"] = sum(len(n) for _, n in novas_por_oab)
        cfg_email = self.config.dados["email"]
        if cfg_email.get("ativo") and novas_por_oab:
            try:
                mailer.enviar(cfg_email, f"[Monitor de Processos] {resumo['novas']} publicação(ões) nova(s) nos filtros de pesquisa",
                              self.montar_email_oab(novas_por_oab))
                resumo["email"] = "enviado"
            except Exception as e:  # noqa: BLE001
                resumo["email"] = f"falhou: {e}"
                log.warning("Falha ao enviar e-mail (OAB): %s", e)
        elif cfg_email.get("ativo"):
            resumo["email"] = "sem novidades"
        self.resumo_oab = resumo
        self.ultimo_ciclo = time.time()
        self.ultimo_filtros = time.time()
        log.info("Consulta por OAB concluída: %s", resumo)
        return resumo

    def executar_descoberta(self, oid, dias=365):
        """Descobre os processos citados no DJEN pelo filtro nos últimos `dias` dias (com 1+ publicação no período).
        Guarda só número, tribunal, classe, órgão, contagem e data da última publicação — não guarda o texto."""
        o = self.banco.obter_oab(oid)
        if not o:
            return None
        desde = (dt.date.today() - dt.timedelta(days=dias)).isoformat()
        if not self.config.dados.get("dje_ativo", True):
            self.banco.registrar_descoberta_status(oid, "erro", "DJEN desativado em Configurações.", desde)
            return None
        base = self.config.dados.get("dje_base_url", dje.BASE_URL)
        rotulo = dje.descrever_filtro(o["tipo"], o["numero"], o["uf"])
        self.banco.registrar_descoberta_status(oid, "buscando", "", desde)
        self.progresso = f"Descoberta {rotulo}"
        try:
            itens, truncado = self._com_espera(
                f"descoberta {rotulo}",
                lambda: dje.consultar_busca(o["tipo"], o["numero"], o["uf"], base, desde=desde, max_paginas=100),
                canal="dje")
        except LimiteExcedido as e:
            self.dje_pausado_ate = time.time() + self.pausa_limite
            self.banco.registrar_descoberta_status(oid, "erro", f"{e} Tente de novo mais tarde.", desde)
            return None
        except datajud.ErroAPI as e:
            self.banco.registrar_descoberta_status(oid, "erro", str(e), desde)
            return None
        except Exception as e:  # noqa: BLE001
            log.exception("Erro inesperado na descoberta")
            self.banco.registrar_descoberta_status(oid, "erro", f"Erro inesperado: {e}", desde)
            return None
        finally:
            self.progresso = ""
        grupos, ignorados = {}, 0
        for it in itens:
            try:
                info = datajud.interpretar_numero(it["numero"])
                datajud.alias_tribunal(info["j"], info["tr"])
            except (datajud.ErroNumero, datajud.TribunalNaoSuportado):
                ignorados += 1
                continue
            g = grupos.setdefault(info["limpo"], {"numero": info["numero"], "numero_limpo": info["limpo"],
                                                  "tribunal": it["tribunal"], "classe": "", "orgao": "",
                                                  "n_pubs": 0, "ultima_data": ""})
            g["n_pubs"] += 1
            if it["data_disp"] >= g["ultima_data"]:
                g["ultima_data"] = it["data_disp"]
                g["classe"] = it["classe"] or g["classe"]
                g["orgao"] = it["orgao"] or g["orgao"]
        self.banco.registrar_descobertos(oid, list(grupos.values()))
        aviso = ("Resultado parcial: a API limita a quantidade de publicações por consulta; pode haver outros processos. "
                 "Repita depois ou use filtros mais específicos." if truncado else "")
        if ignorados:
            aviso = (aviso + " " if aviso else "") + f"{ignorados} publicação(ões) de processo(s) com número ou tribunal não suportado foram ignoradas."
        self.banco.registrar_descoberta_status(oid, "ok", aviso, desde, truncado, ignorados)
        log.info("Descoberta %s: %d processo(s) em %d publicação(ões) desde %s", rotulo, len(grupos), len(itens), desde)
        return len(grupos)

    def _falha_oab(self, o, mensagem, resumo):
        self.banco.registrar_oab_status(o["id"], "erro", mensagem)
        n = self.tentativas_oab.get(o["id"], 0)
        self.tentativas_oab[o["id"]] = n + 1
        self.proxima_oab[o["id"]] = time.time() + BACKOFF_MIN[min(n, len(BACKOFF_MIN) - 1)] * 60
        resumo["erros"] += 1
        log.warning("Filtro %s: falha: %s", dje.descrever_filtro(o["tipo"], o["numero"], o["uf"]), mensagem)

    @staticmethod
    def montar_email_oab(novas_por_oab):
        linhas = ["PUBLICAÇÕES NOVAS NO DJEN PELOS FILTROS DE PESQUISA (processos ainda NÃO cadastrados no monitor)", ""]
        for o, pubs in novas_por_oab:
            linhas.append(dje.descrever_filtro(o["tipo"], o["numero"], o["uf"]) + (f" — {o['rotulo']}" if o["rotulo"] else ""))
            for b in pubs:
                d = b["data_disp"]
                data = f"{d[8:10]}/{d[5:7]}/{d[0:4]}" if len(d) >= 10 else d
                linhas.append(f"  • {dje.formatar_cnj(b['numero'])} ({b['tribunal']}) — disponibilizada em {data} — "
                              f"{b['orgao']} — {b['tipo_doc'] or b['tipo_com']}")
                texto = " ".join(b["texto"].split())
                linhas.append("    " + (texto[:700] + "…" if len(texto) > 700 else texto))
            linhas.append("")
        linhas.append(AVISO_RODAPE)
        return "\n".join(linhas)

    # ------------------------------------------------------------------ DJEN
    def _dje(self, processos, ctx):
        """Consulta o DJEN (publicações) processo a processo. Falhas aqui nunca afetam o DataJud."""
        base = self.config.dados.get("dje_base_url", dje.BASE_URL)
        total = len(processos)
        for i, p in enumerate(processos, 1):
            if time.time() < self.dje_pausado_ate:
                ctx["dje_aviso"] = "DJEN em pausa por limite de requisições; nova tentativa automática."
                break
            self.progresso = f"DJEN {i}/{total}"
            try:
                itens = self._com_espera(f"dje {p['numero_limpo']}",
                                         lambda p=p: dje.consultar(p["numero_limpo"], base), canal="dje")
            except LimiteExcedido as e:
                self.dje_pausado_ate = time.time() + self.pausa_limite
                ctx["dje_aviso"] = str(e)
                break
            except datajud.ErroAPI as e:
                self._falha_dje(p, str(e), ctx)
                if e.codigo in (401, 403):
                    ctx["dje_aviso"] = str(e)
                    break
                continue
            except Exception as e:  # noqa: BLE001
                log.exception("Erro inesperado no DJEN")
                self._falha_dje(p, f"Erro inesperado: {e}", ctx)
                continue
            novas = self.banco.registrar_publicacoes(p["id"], itens)
            self.banco.registrar_dje_status(p["id"], "ok", "")
            self.tentativas_dje.pop(p["id"], None)
            self.proxima_dje.pop(p["id"], None)
            ctx["dje_feitos"] += 1
            if novas:
                ctx["pubs"].append((p, novas))
        self.progresso = ""

    def _falha_dje(self, p, mensagem, ctx):
        self.banco.registrar_dje_status(p["id"], "erro", mensagem)
        n = self.tentativas_dje.get(p["id"], 0)
        self.tentativas_dje[p["id"]] = n + 1
        self.proxima_dje[p["id"]] = time.time() + BACKOFF_MIN[min(n, len(BACKOFF_MIN) - 1)] * 60
        ctx["dje_erros"] += 1
        log.warning("DJEN: falha em %s: %s", p["numero"], mensagem)

    # ----------------------------------------------------------- um bloco
    def _bloco(self, alias, bloco, ctx):
        d = self.config.dados
        try:
            individuais = bloco
            if len(bloco) > 1:
                try:
                    res = self._com_espera(
                        f"lote {alias} x{len(bloco)}",
                        lambda: datajud.consultar_lote([p["numero_limpo"] for p in bloco], alias,
                                                       d["api_key"], d["base_url"]),
                        repetir_parcial=False)  # resposta parcial: vai direto para consultas individuais
                    individuais = []
                    for p in bloco:
                        dados = res.get(p["numero_limpo"])
                        if dados is None:
                            individuais.append(p)  # ausência NUNCA é aceita sem confirmação individual
                        else:
                            self._registrar(p, dados, ctx)
                except datajud.LoteIncompleto as e:
                    log.info("Lote %s não utilizável (%s); consultando um a um", alias, e)
                except datajud.ErroAPI as e:
                    if e.codigo not in (400, "parcial"):
                        raise
                    log.info("Lote %s recusado/parcial (%s); consultando um a um", alias, e.codigo)
            for p in individuais:
                if ctx["interrompido"]:
                    return
                try:
                    dados = self._com_espera(
                        f"{alias} {p['numero_limpo']}",
                        lambda p=p: datajud.consultar(p["numero_limpo"], alias, d["api_key"], d["base_url"]))
                except datajud.ErroAPI as e:
                    if e.codigo in (401, 403):
                        raise
                    self._falha(p, str(e), ctx)
                    continue
                self._registrar(p, dados, ctx)
        except LimiteExcedido as e:
            self.pausado_ate = time.time() + self.pausa_limite
            ctx["interrompido"] = ("limite", str(e))
            self._adiar(bloco, ctx, str(e), limite=True)
        except datajud.ErroAPI as e:
            if e.codigo in (401, 403):
                ctx["interrompido"] = ("fatal", str(e))
                self._adiar(bloco, ctx, str(e))
                return
            for p in bloco:  # erro do lote inteiro (índice 404, timeout, 5xx...)
                if p["id"] not in ctx["feitos_ids"]:
                    self._falha(p, str(e), ctx)

    def _registrar(self, p, dados, ctx):
        if dados is None:
            if self.banco.obter(p["id"])["baseline_feito"]:
                # já foi encontrado antes: não rebaixa para "não encontrado" por uma resposta isolada
                msg = ("Atenção: não retornou na última consulta (confirmada individualmente); "
                       "dados anteriores mantidos. Se persistir, verifique se entrou em segredo de justiça.")
                self.banco.registrar_resultado(p["id"], "ok", msg)
                log.warning("%s sumiu da base após ter sido encontrado; dados anteriores mantidos", p["numero"])
            else:
                msg = ("Não consta na base pública do DataJud (confirmado em consulta individual). "
                       "Processos em segredo de justiça não aparecem; também pode não ter sido enviado "
                       "ainda pelo tribunal.")
                self.banco.registrar_resultado(p["id"], "nao_encontrado", msg)
            novos = []
        else:
            novos = self.banco.registrar_resultado(p["id"], "ok", "", dados)
        self.tentativas.pop(p["id"], None)
        self.proxima.pop(p["id"], None)
        ctx["feitos"] += 1
        ctx["feitos_ids"].add(p["id"])
        if novos:
            ctx["novos"].append((p, novos))

    def _falha(self, p, mensagem, ctx):
        self.banco.registrar_resultado(p["id"], "erro", mensagem)
        n = self.tentativas.get(p["id"], 0)
        self.tentativas[p["id"]] = n + 1
        self.proxima[p["id"]] = time.time() + BACKOFF_MIN[min(n, len(BACKOFF_MIN) - 1)] * 60
        ctx["feitos"] += 1
        ctx["feitos_ids"].add(p["id"])
        ctx["erros"] += 1
        log.warning("Falha em %s: %s", p["numero"], mensagem)

    def _adiar(self, bloco, ctx, mensagem, limite=False):
        """Mantém o bloco (e o restante do ciclo) como pendente, sem contar como erro."""
        texto = ("Aguardando: limite de requisições da API atingido; nova tentativa automática."
                 if limite else mensagem)
        for p in bloco:
            atual = self.banco.obter(p["id"])
            if atual and atual["status"] == "pendente":
                self.banco.registrar_resultado(p["id"], "pendente", texto)
            self.proxima[p["id"]] = time.time() + self.pausa_limite

    # ------------------------------------------------- ritmo e 429
    def _com_espera(self, rotulo, fn, repetir_parcial=True, canal="datajud"):
        parciais = 0
        limites = 0
        while True:
            self._respeitar_ritmo(canal)
            t0 = time.time()
            try:
                r = fn()
                log.info("%s: ok em %.1fs", rotulo, time.time() - t0)
                return r
            except datajud.ErroAPI as e:
                log.warning("%s: %s (HTTP %s) em %.1fs", rotulo, e, e.codigo, time.time() - t0)
                if e.codigo == "parcial" and repetir_parcial and parciais < len(self.espera_parcial):
                    espera = self.espera_parcial[parciais]
                    parciais += 1
                    log.info("Resposta parcial: aguardando %ss antes de repetir", espera)
                    self._dormir(espera)
                    continue
                if e.codigo == 429:
                    if limites < len(self.espera_429):
                        espera = min(e.retry_after or self.espera_429[limites], 300)
                        limites += 1
                        log.info("429: aguardando %ss antes de repetir", espera)
                        self._dormir(espera)
                        continue
                    raise LimiteExcedido("Limite de requisições da API (429) persistente; "
                                         "nova tentativa automática mais tarde.")
                raise
            finally:
                if canal == "dje":
                    self._ultima_req_dje = time.time()
                else:
                    self._ultima_req = time.time()

    def _respeitar_ritmo(self, canal="datajud"):
        ultima = self._ultima_req_dje if canal == "dje" else self._ultima_req
        pausa_ate = self.dje_pausado_ate if canal == "dje" else self.pausado_ate
        espera = max(ultima + self.pausa - time.time(), pausa_ate - time.time(), 0)
        if espera > 0:
            self._dormir(espera)

    # ---------------------------------------------------------------- e-mail
    @staticmethod
    def montar_email(novos_por_proc, pubs_por_proc=()):
        linhas = []
        if pubs_por_proc:
            linhas += ["PUBLICAÇÕES NO DJEN (Diário de Justiça Eletrônico Nacional)", ""]
            for p, pubs in pubs_por_proc:
                titulo = p["apelido"] or p["numero"]
                linhas.append(f"{titulo} — {p['numero']}" + (f" — cliente: {p['cliente']}" if p["cliente"] else ""))
                for b in pubs:
                    d = b["data_disp"]
                    data = f"{d[8:10]}/{d[5:7]}/{d[0:4]}" if len(d) >= 10 else d
                    linhas.append(f"  • Disponibilizada em {data} — {b['orgao']} — {b['tipo_doc'] or b['tipo_com']}")
                    texto = " ".join(b["texto"].split())
                    linhas.append("    " + (texto[:700] + "…" if len(texto) > 700 else texto))
                linhas.append("")
        if novos_por_proc:
            linhas += ["MOVIMENTAÇÕES NO DATAJUD", ""]
            for p, novos in novos_por_proc:
                titulo = p["apelido"] or p["numero"]
                linhas.append(f"{titulo} — {p['numero']}" + (f" — cliente: {p['cliente']}" if p["cliente"] else ""))
                for m in novos:
                    data = (m["data_hora"] or "")[:16].replace("T", " ")
                    linhas.append(f"  • {data}  {m['nome']}" + (f" ({m['complementos']})" if m["complementos"] else ""))
                linhas.append("")
        linhas.append(AVISO_RODAPE)
        return "\n".join(linhas)
