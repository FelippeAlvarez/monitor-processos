"""Cliente do DJEN (Diário de Justiça Eletrônico Nacional) via API Comunica PJe (CNJ).

Verificado em 10/11/2026 pelo usuário, no navegador: GET sem login
    https://comunicaapi.pje.jus.br/api/v1/comunicacao?numeroProcesso=<20 dígitos>
devolve JSON {"status","message","count","items":[...]} com o TEXTO das publicações.
Não encontrei documentação oficial; o formato abaixo foi deduzido dessa resposta real.
Parâmetros de paginação ("pagina") são presumidos e tratados de forma defensiva.
"""
import datetime as dt
import json
import logging
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import http.client
from html.parser import HTMLParser

from datajud import ErroAPI, _retry_after

log = logging.getLogger("monitor.dje")

BASE_URL = "https://comunicaapi.pje.jus.br"
MAX_PAGINAS = 5


def _get(url, timeout):
    req = urllib.request.Request(url, method="GET", headers={
        "Accept": "application/json", "User-Agent": "MonitorProcessos/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        ra = _retry_after(e.headers.get("Retry-After") if e.headers else None)
        if e.code in (401, 403):
            raise ErroAPI(f"A API do DJEN recusou o acesso ({e.code}).", e.code)
        if e.code == 429:
            raise ErroAPI("Limite de requisições do DJEN atingido (429).", 429, retry_after=ra, transitorio=True)
        raise ErroAPI(f"Erro HTTP {e.code} da API do DJEN.", e.code, retry_after=ra, transitorio=e.code >= 500)
    except urllib.error.URLError as e:
        raise ErroAPI(f"Sem conexão com a API do DJEN: {e.reason}", transitorio=True)
    except (OSError, http.client.HTTPException) as e:
        raise ErroAPI(f"Falha de comunicação com a API do DJEN ({type(e).__name__}): {e}", transitorio=True)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ErroAPI("Resposta inválida da API do DJEN.", transitorio=True)


class _Texto(HTMLParser):
    """Converte o HTML das publicações em texto simples (ignora head/style/script)."""
    QUEBRA = {"br", "p", "div", "tr", "section", "article", "header", "footer", "li", "h1", "h2", "h3", "hr", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes, self.ignorar = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head", "title"):
            self.ignorar += 1
        elif tag in self.QUEBRA:
            self.partes.append("\n")
        elif tag in ("td", "th"):
            self.partes.append(" ")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head", "title"):
            self.ignorar = max(0, self.ignorar - 1)
        elif tag in self.QUEBRA:
            self.partes.append("\n")

    def handle_data(self, data):
        if not self.ignorar:
            self.partes.append(data)


def limpar_texto(bruto):
    """Texto simples a partir do campo 'texto' (que pode vir em HTML ou já em texto puro)."""
    bruto = str(bruto or "")
    if not re.search(r"<\s*/?\s*[a-zA-Z][^>]*>", bruto):
        return bruto.strip()
    p = _Texto()
    try:
        p.feed(bruto)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", bruto).strip()
    t = "".join(p.partes).replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" ?\n ?", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _data_iso(item):
    d = str(item.get("data_disponibilizacao") or "")[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        return d
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", str(item.get("datadisponibilizacao") or ""))
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def normalizar_item(it):
    destinatarios = [{"nome": str(d.get("nome") or ""), "polo": str(d.get("polo") or "")}
                     for d in (it.get("destinatarios") or []) if isinstance(d, dict)]
    advogados, vistos = [], set()
    for da in (it.get("destinatarioadvogados") or []):
        adv = (da or {}).get("advogado") or {}
        chave = (adv.get("numero_oab"), adv.get("uf_oab"))
        if adv.get("nome") and chave not in vistos:
            vistos.add(chave)
            advogados.append({"nome": str(adv["nome"]), "oab": f"{adv.get('numero_oab', '')}/{adv.get('uf_oab', '')}"})
    cancelada = bool(it.get("data_cancelamento") or it.get("motivo_cancelamento")) or it.get("ativo") is False
    return {
        "pub_id": str(it.get("id")),
        "data_disp": _data_iso(it),
        "tipo_com": str(it.get("tipoComunicacao") or ""),
        "orgao": str(it.get("nomeOrgao") or ""),
        "tipo_doc": str(it.get("tipoDocumento") or ""),
        "classe": str(it.get("nomeClasse") or ""),
        "texto": limpar_texto(it.get("texto")),
        "meio": str(it.get("meiocompleto") or it.get("meio") or ""),
        "hash": str(it.get("hash") or ""),
        "destinatarios": json.dumps(destinatarios, ensure_ascii=False),
        "advogados": json.dumps(advogados, ensure_ascii=False),
        "cancelada": 1 if cancelada else 0,
        "numero": re.sub(r"\D", "", str(it.get("numero_processo") or "")),
        "tribunal": str(it.get("siglaTribunal") or ""),
    }


def consultar(numero_limpo, base_url=BASE_URL, timeout=60):
    """Devolve a lista de publicações (normalizadas) do processo, mais recentes primeiro."""
    url = f"{base_url.rstrip('/')}/api/v1/comunicacao?" + urllib.parse.urlencode({"numeroProcesso": numero_limpo})
    dados = _get(url, timeout)
    if not isinstance(dados, dict) or "items" not in dados:
        raise ErroAPI("Resposta do DJEN fora do formato esperado.", transitorio=True)
    itens = list(dados.get("items") or [])
    total = dados.get("count")
    log.info("dje %s: count=%s itens=%d", numero_limpo, total, len(itens))
    # Se há mais itens que os devolvidos, tenta as páginas seguintes ("pagina" é presumido; para se não avançar).
    pagina = 1
    vistos = {str(i.get("id")) for i in itens}
    while isinstance(total, int) and len(vistos) < total and pagina < MAX_PAGINAS:
        pagina += 1
        try:
            mais = _get(url + f"&pagina={pagina}", timeout).get("items") or []
        except ErroAPI as e:
            log.warning("dje %s: página %d falhou (%s); %d de %s publicações carregadas",
                        numero_limpo, pagina, e, len(vistos), total)
            break
        novos = [i for i in mais if str(i.get("id")) not in vistos]
        if not novos:
            log.warning("dje %s: paginação não avançou; %d de %s publicações carregadas",
                        numero_limpo, len(vistos), total)
            break
        itens += novos
        vistos |= {str(i.get("id")) for i in novos}
    resultado = [normalizar_item(i) for i in itens]
    resultado = [r for r in resultado if r["pub_id"] != "None" and (not r["numero"] or r["numero"] == numero_limpo)]
    resultado.sort(key=lambda r: (r["data_disp"], r["pub_id"]), reverse=True)
    return resultado


def consultar_bruto(numero_limpo, inicio="", fim="", base_url=BASE_URL, timeout=60):
    """Diagnóstico (v0.5.4): devolve o JSON da API do DJEN COMO CHEGOU (sem normalizar), só a 1ª página,
    junto com a versão normalizada de cada item para comparação. Não grava nada no banco."""
    params = {"numeroProcesso": numero_limpo}
    if inicio:
        params["dataDisponibilizacaoInicio"] = inicio
    if fim:
        params["dataDisponibilizacaoFim"] = fim
    url = f"{base_url.rstrip('/')}/api/v1/comunicacao?" + urllib.parse.urlencode(params)
    dados = _get(url, timeout)
    if not isinstance(dados, dict) or "items" not in dados:
        raise ErroAPI("Resposta do DJEN fora do formato esperado.", transitorio=True)
    itens = [i for i in (dados.get("items") or []) if isinstance(i, dict)]
    return {"count": dados.get("count"), "itens_brutos": itens, "itens_normalizados": [normalizar_item(i) for i in itens]}


def formatar_cnj(limpo):
    """20 dígitos -> NNNNNNN-DD.AAAA.J.TR.OOOO (devolve como veio se não tiver 20 dígitos)."""
    n = re.sub(r"\D", "", str(limpo))
    if len(n) != 20:
        return str(limpo)
    return f"{n[0:7]}-{n[7:9]}.{n[9:13]}.{n[13]}.{n[14:16]}.{n[16:20]}"


def so_digitos(v):
    return re.sub(r"\D", "", str(v))


def cpf_valido(v):
    n = so_digitos(v)
    if len(n) != 11 or len(set(n)) == 1:
        return False
    for tam in (9, 10):
        soma = sum(int(n[i]) * (tam + 1 - i) for i in range(tam))
        if int(n[tam]) != (soma * 10 % 11) % 10:
            return False
    return True


def cnpj_valido(v):
    n = so_digitos(v)
    if len(n) != 14 or len(set(n)) == 1:
        return False
    for tam, pesos in ((12, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]), (13, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])):
        soma = sum(int(n[i]) * pesos[i] for i in range(tam))
        resto = soma % 11
        if int(n[tam]) != (0 if resto < 2 else 11 - resto):
            return False
    return True


def formas_documento(v):
    """CPF/CNPJ -> [forma mascarada, só dígitos]: a busca é textual e as duas formas aparecem nas publicações."""
    n = so_digitos(v)
    if len(n) == 11:
        return [f"{n[0:3]}.{n[3:6]}.{n[6:9]}-{n[9:11]}", n]
    if len(n) == 14:
        return [f"{n[0:2]}.{n[2:5]}.{n[5:8]}/{n[8:12]}-{n[12:14]}", n]
    raise ValueError("Documento deve ter 11 (CPF) ou 14 (CNPJ) dígitos.")


def descrever_filtro(tipo, termo, uf=""):
    if tipo == "oab":
        return f"OAB {termo}/{uf}"
    if tipo == "doc":
        return "CPF/CNPJ " + formas_documento(termo)[0]
    return f'texto "{termo}"'


def _paginar(base, base_url, timeout, por_pagina, max_paginas, rotulo, limite_seg=None):
    """Percorre as páginas de uma consulta. Devolve (itens brutos, truncado). `limite_seg`: tempo total máximo (opcional)."""
    base = dict(base, itensPorPagina=por_pagina)
    itens, vistos, total, truncado = [], set(), None, False
    inicio = time.monotonic()
    for pagina in range(1, max_paginas + 1):
        if limite_seg and pagina > 1 and time.monotonic() - inicio > limite_seg:
            truncado = True
            log.warning("dje %s: tempo limite de %ss atingido na página %d", rotulo, limite_seg, pagina)
            break
        url = f"{base_url.rstrip('/')}/api/v1/comunicacao?" + urllib.parse.urlencode(dict(base, pagina=pagina))
        dados = _get(url, timeout)
        if not isinstance(dados, dict) or "items" not in dados:
            raise ErroAPI("Resposta do DJEN fora do formato esperado.", transitorio=True)
        total = dados.get("count") if isinstance(dados.get("count"), int) else total
        pag = list(dados.get("items") or [])
        novos = [i for i in pag if str(i.get("id")) not in vistos]
        log.info("dje %s pág %d: itens=%d count=%s", rotulo, pagina, len(pag), total)
        if not pag:
            truncado = isinstance(total, int) and len(vistos) < total
            if truncado:
                log.warning("dje %s: página vazia com %d de %s carregadas", rotulo, len(vistos), total)
            break
        if not novos:
            truncado = True
            log.warning("dje %s: paginação não avançou", rotulo)
            break
        itens += novos
        vistos |= {str(i.get("id")) for i in novos}
        if isinstance(total, int) and len(vistos) >= total:
            break
    else:
        truncado = True
        log.warning("dje %s: limite de %d páginas atingido", rotulo, max_paginas)
    return itens, truncado


def consultar_busca(tipo, termo, uf="", base_url=BASE_URL, desde=None, timeout=60, por_pagina=50, max_paginas=20):
    """Publicações por filtro de pesquisa a partir de `desde` (AAAA-MM-DD). Devolve (publicações, truncado).

    tipo 'oab'   -> numeroOab/ufOab (comparado como texto exato pela API; pode haver sufixos -A/-O);
    tipo 'doc'   -> CPF/CNPJ buscado no TEXTO da publicação, nas formas mascarada e só dígitos;
    tipo 'texto' -> trecho livre buscado no texto.
    Busca por texto só acha publicações que citam o termo."""
    if tipo == "oab":
        variantes = [{"numeroOab": termo, "ufOab": uf}]
    elif tipo == "doc":
        variantes = [{"texto": f} for f in formas_documento(termo)]
    elif tipo == "texto":
        variantes = [{"texto": termo}]
    else:
        raise ValueError(f"Tipo de filtro desconhecido: {tipo}")
    brutos, vistos, truncado = [], set(), False
    for v in variantes:
        if desde:
            v = dict(v, dataDisponibilizacaoInicio=desde)
        itens, trunc = _paginar(v, base_url, timeout, por_pagina, max_paginas, descrever_filtro(tipo, termo, uf))
        truncado = truncado or trunc
        for i in itens:
            if str(i.get("id")) not in vistos:
                vistos.add(str(i.get("id")))
                brutos.append(i)
    resultado = [r for r in (normalizar_item(i) for i in brutos) if r["pub_id"] != "None"]
    resultado.sort(key=lambda r: (r["data_disp"], r["pub_id"]), reverse=True)
    return resultado, truncado


def consultar_oab(numero, uf, base_url=BASE_URL, desde=None, timeout=60, por_pagina=50, max_paginas=20):
    return consultar_busca("oab", numero, uf, base_url, desde, timeout, por_pagina, max_paginas)


# ---------------------------------------------------------------- v0.7.5: pesquisa avulsa (não grava nada)
PESQ_MAX_DIAS = 366
PESQ_TEXTO_MAX = 6000


class ErroPesquisa(ValueError):
    pass


def _norm(t):
    t = unicodedata.normalize("NFKD", str(t or ""))
    return re.sub(r"[^A-Z0-9]+", " ", "".join(c for c in t if not unicodedata.combining(c)).upper()).strip()


def _data_pesq(v, rotulo):
    v = str(v or "").strip()
    try:
        return dt.date.fromisoformat(v)
    except ValueError:
        raise ErroPesquisa(f"{rotulo}: use uma data válida (AAAA-MM-DD).")


def validar_pesquisa(oab="", uf="", numero="", nome="", texto="", tribunal="", inicio="", fim="", hoje=None):
    """Confere e normaliza os filtros. Devolve dict limpo ou levanta ErroPesquisa."""
    hoje = hoje or dt.date.today()
    f = {"oab": re.sub(r"[^0-9A-Za-z]", "", str(oab or "")).upper().lstrip("0"), "uf": str(uf or "").strip().upper(),
         "numero": so_digitos(numero), "nome": re.sub(r"\s+", " ", str(nome or "")).strip(),
         "texto": re.sub(r"\s+", " ", str(texto or "")).strip(), "tribunal": re.sub(r"[^0-9A-Za-z]", "", str(tribunal or "")).upper()}
    if f["oab"] and not re.fullmatch(r"[0-9]{1,7}[A-Z]?", f["oab"]):
        raise ErroPesquisa("Número da OAB inválido (só dígitos; letra final opcional).")
    if f["oab"] and not re.fullmatch(r"[A-Z]{2}", f["uf"]):
        raise ErroPesquisa("Informe a UF da OAB.")
    if str(numero or "").strip() and len(f["numero"]) != 20:
        raise ErroPesquisa("O número do processo deve ter 20 dígitos (formato CNJ).")
    if f["nome"] and len(_norm(f["nome"])) < 3:
        raise ErroPesquisa("O nome da parte deve ter pelo menos 3 letras.")
    if f["texto"] and len(f["texto"]) < 3:
        raise ErroPesquisa("O trecho de texto deve ter pelo menos 3 caracteres.")
    if not (f["oab"] or f["numero"] or f["nome"] or f["texto"]):
        raise ErroPesquisa("Informe pelo menos um filtro: OAB, número do processo, nome da parte ou trecho de texto.")
    if f["tribunal"] and not re.fullmatch(r"[A-Z0-9]{2,10}", f["tribunal"]):
        raise ErroPesquisa("Sigla de tribunal inválida (ex.: TJRJ, TRT1).")
    di, df = _data_pesq(inicio, "Data inicial"), _data_pesq(fim, "Data final")
    if di > df:
        raise ErroPesquisa("A data inicial não pode ser depois da data final.")
    if df > hoje:
        df = hoje
        if di > df:
            raise ErroPesquisa("A data inicial está no futuro.")
    if (df - di).days > PESQ_MAX_DIAS:
        raise ErroPesquisa(f"Período máximo: {PESQ_MAX_DIAS} dias.")
    f["inicio"], f["fim"] = di.isoformat(), df.isoformat()
    return f


def _confere(item, f):
    """Confere LOCALMENTE se a publicação atende aos filtros. Protege contra a API ignorar um parâmetro sem avisar:
    nunca mostramos como resultado algo que não contenha o que foi pedido."""
    destinos = " ".join(str(d.get("nome") or "") for d in _lista_json(item["destinatarios"]))
    advs = _lista_json(item["advogados"])
    if f["numero"] and item["numero"] != f["numero"]:
        return False
    if f["tribunal"] and item["tribunal"].upper() != f["tribunal"]:
        return False
    if f["oab"]:
        dig = re.sub(r"\D", "", f["oab"]).lstrip("0")
        achou = any(re.sub(r"\D", "", str(a.get("oab", "")).partition("/")[0]).lstrip("0") == dig for a in advs)
        achou = achou or bool(re.search(r"(?<!\d)0*" + re.escape(dig) + r"(?!\d)", item["texto"]))
        if not achou:
            return False
    campo = _norm(destinos + " " + item["texto"])
    if f["nome"] and not all(p in campo.split() for p in _norm(f["nome"]).split()):
        return False
    if f["texto"] and _norm(f["texto"]) not in _norm(item["texto"] + " " + destinos):
        return False
    return True


def _lista_json(s):
    try:
        v = json.loads(s or "[]")
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def pesquisar(filtros, base_url=BASE_URL, timeout=60, por_pagina=50, max_paginas=20, limite_seg=150):
    """Pesquisa avulsa no DJEN com os filtros combinados (E). `filtros` já validado por validar_pesquisa.
    Devolve {"itens", "truncado", "descartados", "modo", "total_api"}. Não grava nada.
    Nome da parte: tenta o parâmetro próprio da API (nomeParte); se a API o recusar (HTTP 400/422), refaz como busca
    no texto e avisa em `modo`. Em qualquer caso, cada resultado é conferido localmente (ver _confere)."""
    f = filtros
    base = {"dataDisponibilizacaoInicio": f["inicio"], "dataDisponibilizacaoFim": f["fim"]}
    if f["oab"]:
        base.update(numeroOab=f["oab"], ufOab=f["uf"])
    if f["numero"]:
        base["numeroProcesso"] = f["numero"]
    if f["tribunal"]:
        base["siglaTribunal"] = f["tribunal"]
    if f["texto"]:
        base["texto"] = f["texto"]
    modo = "padrao"
    rotulo = "pesquisa"
    if f["nome"]:
        try:
            brutos, trunc = _paginar(dict(base, nomeParte=f["nome"]), base_url, timeout, por_pagina, max_paginas, rotulo, limite_seg)
        except ErroAPI as e:
            if e.codigo not in (400, 422):
                raise
            log.warning("dje pesquisa: a API recusou nomeParte (%s); refazendo como busca no texto", e.codigo)
            modo = "nome_no_texto"
            alt = dict(base)
            alt["texto"] = (f["texto"] + " " + f["nome"]).strip() if f["texto"] else f["nome"]
            brutos, trunc = _paginar(alt, base_url, timeout, por_pagina, max_paginas, rotulo, limite_seg)
    else:
        brutos, trunc = _paginar(base, base_url, timeout, por_pagina, max_paginas, rotulo, limite_seg)
    links = {str(i.get("id")): str(i.get("link") or "") for i in brutos}
    itens, descartados = [], 0
    for r in (normalizar_item(i) for i in brutos):
        if r["pub_id"] == "None":
            continue
        if not _confere(r, f):
            descartados += 1
            continue
        r["link"] = links.get(r["pub_id"], "")
        r["texto"] = r["texto"][:PESQ_TEXTO_MAX]
        itens.append(r)
    itens.sort(key=lambda r: (r["data_disp"], r["pub_id"]), reverse=True)
    return {"itens": itens, "truncado": bool(trunc), "descartados": descartados, "modo": modo, "total_api": len(brutos)}
