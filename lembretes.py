"""Resumo diário da agenda por e-mail (v0.7.2).

Quem recebe o quê:
- o responsável pelo evento recebe os eventos dele (se tiver e-mail cadastrado);
- o(s) e-mail(s) gerais das configurações recebem tudo (opção `geral_recebe_tudo`, ligada por padrão) ou, se a opção estiver
  desligada, só os eventos sem responsável com e-mail;
- ninguém recebe o mesmo aviso duas vezes.

O que entra:
- VENCIDOS: eventos em aberto com data passada — uma vez por dia por destinatário, até serem concluídos ou cancelados;
- LEMBRETES: para cada antecedência escolhida no evento (ex.: 7, 3, 1 dia ou no dia), o aviso sai na primeira
  oportunidade a partir de "data − N dias". Se o computador estava desligado, o aviso atrasado sai ao ligar (um único e-mail
  reúne tudo). Cada aviso é registrado por (evento, antecedência, data, destinatário): mudar a data do evento reabilita os lembretes.

O programa só envia com o computador ligado e o Monitor aberto: para prazo fatal, use também o .ics no celular e confira os autos.
"""
import datetime as dt
import logging
import re
import time

import agenda
import mailer

log = logging.getLogger("monitor")
DIAS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
ESPERA_FALHA = 30 * 60     # após falha de envio, nova tentativa só depois de 30 min
_falhas = {}               # destinatário -> epoch da última falha


def _br(iso):
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}"


def _lista_emails(txt):
    return [x.strip() for x in re.split(r"[;,]", txt or "") if x.strip()]


def _destinos(b, e, gerais, geral_tudo):
    """Conjunto de e-mails (minúsculos) que devem receber este evento, com o nome para saudação."""
    out = {}
    u = b.email_do_responsavel(e["responsavel_id"]) if e.get("responsavel_id") else None
    if u and u.get("email"):
        out[u["email"].strip().lower()] = u["nome"]
    if geral_tudo or not out:
        for g in gerais:
            out.setdefault(g.lower(), "")
    return out


def _candidatos(b, hoje):
    """Eventos em aberto que podem gerar aviso: vencidos ou dentro da maior antecedência (30 dias)."""
    lim = (dt.date.fromisoformat(hoje) + dt.timedelta(days=max(agenda.LEMBRETES_OK))).isoformat()
    return agenda.listar(b, ate=lim, situacao="aberto", hoje=hoje, limite=agenda.MAX_LISTA)


def _enviados(b, dest):
    return {(r[0], r[1], r[2]) for r in b.conn.execute(
        "SELECT evento_id, dias_antes, data_ref FROM lembretes_enviados WHERE destinatario=?", (dest,)).fetchall()}


def pendentes(b, dest, eventos, hoje):
    """Itens que `dest` ainda não recebeu: (vencidos, lembretes). lembretes: lista de (evento, [antecedências devidas])."""
    ja = _enviados(b, dest)
    h = dt.date.fromisoformat(hoje)
    vencidos, lembs = [], []
    for e in eventos:
        d = dt.date.fromisoformat(e["data"])
        if d < h:
            vencidos.append(e)
            continue
        devidos = [n for n in e["lembretes"] if d - dt.timedelta(days=n) <= h and (e["id"], n, e["data"]) not in ja]
        if devidos:
            lembs.append((e, devidos))
    return vencidos, lembs


def _linha(e, extra=""):
    d = dt.date.fromisoformat(e["data"])
    proc = ""
    if e.get("processo_numero"):
        proc = f" — {e.get('processo_apelido') or e.get('processo_cliente') or ''} ({e['processo_numero']})".replace(" ()", "")
    resp = f" — resp.: {e['responsavel_nome']}" if e.get("responsavel_nome") else ""
    hora = f" {e['hora']}" if e.get("hora") else ""
    calc = "  [data calculada pelo programa — confira]" if e.get("calculo") else ""
    return f"• {_br(e['data'])} ({DIAS[d.weekday()]}){hora}{extra} — {e['titulo']} [{e['tipo_nome']}]{proc}{resp}{calc}"


def montar(nome, vencidos, lembs, hoje):
    h = dt.date.fromisoformat(hoje)
    hoje_l = [x for x in lembs if x[0]["data"] == hoje]
    prox = sorted([x for x in lembs if x[0]["data"] != hoje], key=lambda x: (x[0]["data"], x[0]["hora"]))
    L = [f"Olá{', ' + nome if nome else ''}. Agenda do Monitor de Processos — {_br(hoje)} ({DIAS[h.weekday()]}).", ""]
    if vencidos:
        L.append(f"VENCIDOS — em aberto, com data já passada ({len(vencidos)}):")
        for e in sorted(vencidos, key=lambda e: e["data"])[:60]:
            atraso = (h - dt.date.fromisoformat(e["data"])).days
            L.append(_linha(e, f" (há {atraso} dia{'s' if atraso != 1 else ''})"))
        if len(vencidos) > 60:
            L.append(f"… e mais {len(vencidos) - 60}. Veja a aba Calendário.")
        L.append("")
    if hoje_l:
        L.append("HOJE:")
        L += [_linha(e) for e, _ in sorted(hoje_l, key=lambda x: x[0]["hora"])]
        L.append("")
    if prox:
        L.append("PRÓXIMOS (lembretes escolhidos em cada evento):")
        for e, _ in prox:
            falta = (dt.date.fromisoformat(e["data"]) - h).days
            L.append(_linha(e, f" (em {falta} dia{'s' if falta != 1 else ''})"))
        L.append("")
    L += ["Concluir, cancelar ou alterar a data de um evento é feito na aba Calendário do Monitor.",
          "Ferramenta de apoio: não substitui a conferência nos autos, nos portais dos tribunais e na intimação oficial; "
          "datas de prazo calculadas pelo programa são sugestões a serem conferidas por quem agenda."]
    partes = []
    if vencidos:
        partes.append(f"{len(vencidos)} vencido(s)")
    if hoje_l:
        partes.append(f"{len(hoje_l)} para hoje")
    if prox:
        partes.append(f"{len(prox)} próximo(s)")
    return f"[Monitor de Processos] Agenda: {', '.join(partes)}", "\n".join(L)


def enviar_resumos(b, cfg, enviar=None, agora=None, forcar=False):
    """Envia (se devido) o resumo da agenda. `cfg` = dados da configuração. Devolve {'enviados': n, 'motivo': str}."""
    em, ag = cfg.get("email") or {}, cfg.get("agenda") or {}
    if not forcar:
        if not em.get("ativo") or not ag.get("email_ativo", True):
            return {"enviados": 0, "motivo": "desligado"}
        agora_ = agora or dt.datetime.now()
        if agora_.strftime("%H:%M") < ag.get("hora", "07:00"):
            return {"enviados": 0, "motivo": "antes do horário"}
    elif not em.get("smtp_host") or not em.get("destinatario"):
        return {"enviados": 0, "motivo": "e-mail não configurado"}
    agora_ = agora or dt.datetime.now()
    hoje = agora_.date().isoformat()
    gerais = _lista_emails(em.get("destinatario"))
    geral_tudo = ag.get("geral_recebe_tudo", True)
    enviar = enviar or mailer.enviar
    eventos = _candidatos(b, hoje)
    por_dest = {}
    for e in eventos:
        for d, nome in _destinos(b, e, gerais, geral_tudo).items():
            por_dest.setdefault(d, {"nome": nome, "eventos": []})["eventos"].append(e)
    n_env, erros = 0, 0
    for dest, info in por_dest.items():
        venc, lembs = pendentes(b, dest, info["eventos"], hoje)
        if venc and b.conn.execute("SELECT 1 FROM agenda_resumos WHERE dia=? AND destinatario=?", (hoje, dest)).fetchone():
            venc = []   # vencidos: no máximo um e-mail por dia por destinatário
        if not venc and not lembs:
            continue
        if not forcar and time.time() - _falhas.get(dest, 0) < ESPERA_FALHA:
            continue
        assunto, corpo = montar(info["nome"], venc, lembs, hoje)
        try:
            enviar({**em, "destinatario": dest}, assunto, corpo)
        except Exception as ex:  # noqa: BLE001
            erros += 1
            _falhas[dest] = time.time()
            log.warning("Falha ao enviar o resumo da agenda a %s: %s", dest, ex)
            continue
        _falhas.pop(dest, None)
        agora_utc = agenda.agora()
        with b.lock:
            for e, ns in lembs:
                for n in ns:
                    b.conn.execute("INSERT OR IGNORE INTO lembretes_enviados(evento_id,dias_antes,data_ref,destinatario,enviado_em) VALUES(?,?,?,?,?)",
                                   (e["id"], n, e["data"], dest, agora_utc))
            if venc:
                b.conn.execute("INSERT OR IGNORE INTO agenda_resumos(dia,destinatario,enviado_em) VALUES(?,?,?)", (hoje, dest, agora_utc))
            b.conn.commit()
        n_env += 1
        log.info("Resumo da agenda enviado a %s (%d vencido(s), %d lembrete(s)).", dest, len(venc), len(lembs))
    return {"enviados": n_env, "erros": erros, "motivo": "ok" if n_env or erros else "nada a enviar"}
