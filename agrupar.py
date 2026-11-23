"""Agrupamento de publicações repetidas (v0.6).

Na Justiça do Trabalho o mesmo despacho costuma sair em várias cópias no mesmo dia, uma por parte, e as cópias
diferem só nos intimados. Aqui decidimos quais publicações são "a mesma" para mostrá-las uma vez só.

REGRA (conservadora): só agrupa se TUDO abaixo for igual — processo, data de disponibilização, tipo de comunicação,
órgão, tipo de documento, situação (cancelada ou não) e o TEXTO, depois de retirar apenas (a) o bloco
"Intimado(s) / Citado(s) - ...", (b) o marcador "|comunicacao_id: N|" e (c) a frase fixa do cabeçalho do DJEN.
Espaços e quebras de linha são normalizados; maiúsculas/minúsculas e acentos NÃO: texto "parecido" nunca agrupa.
Texto base muito curto (menos de MIN_BASE caracteres) também não agrupa.
"""
import hashlib
import re

MIN_BASE = 40

_RE_ID = re.compile(r"\|\s*comunicacao_id\s*:\s*\d+\s*\|", re.I)
_RE_INTIMADOS = re.compile(r"Intimado\(s\)\s*/\s*Citado\(s\)\s*-", re.I)
_RE_CABECALHO = re.compile(r"DI[AÁ]RIO DE JUSTI[CÇ]A ELETR[OÔ]NICO NACIONAL\s*\(DJEN\)", re.I)


def _espacos(t):
    return " ".join(str(t or "").split())


def texto_base(texto):
    """Texto sem o bloco de intimados, sem o marcador de id e sem a frase fixa do cabeçalho do DJEN."""
    t = _espacos(texto)
    t = _RE_ID.sub(" ", t)
    m = _RE_INTIMADOS.search(t)
    if m:
        t = t[:m.start()]
    t = _RE_CABECALHO.sub(" ", t)
    return _espacos(t)


def intimados_do_texto(texto):
    """O bloco 'Intimado(s) / Citado(s) - ...' como veio (sem tentar separar os nomes: há nomes com ' - ' dentro)."""
    t = _RE_ID.sub(" ", _espacos(texto))
    m = _RE_INTIMADOS.search(t)
    return _espacos(t[m.end():]) if m else ""


def chave(chave_processo, it):
    """Chave do grupo de uma publicação (dict normalizado de dje.normalizar_item ou linha do banco).
    chave_processo: identifica o processo (id do banco ou número). Devolve 'g:<hash>' se agrupável, senão 'u:<pub_id>'."""
    base = texto_base(it.get("texto"))
    if len(base) < MIN_BASE:
        return "u:" + str(it.get("pub_id"))
    partes = [str(chave_processo), str(it.get("data_disp") or ""), str(it.get("tipo_com") or ""),
              str(it.get("orgao") or ""), str(it.get("tipo_doc") or ""), "c" if it.get("cancelada") else "a", base]
    return "g:" + hashlib.sha1("\x1f".join(partes).encode("utf-8")).hexdigest()


def resumo_grupo(linhas):
    """Junta as cópias de um grupo (linhas do banco, mais recentes primeiro) numa única publicação para exibir.
    Devolve o dict da 1ª cópia acrescido de: copias (int), ids (todos os ids do banco), intimados (um texto por cópia,
    sem repetição), destinatarios_uniao (nomes vindos do campo 'destinatarios' da API, sem repetição)."""
    import json
    base = dict(linhas[0])
    intim, nomes, vistos_n = [], [], set()
    for b in linhas:
        t = intimados_do_texto(b.get("texto"))
        if t and t not in intim:
            intim.append(t)
        try:
            for d in json.loads(b.get("destinatarios") or "[]"):
                n = str(d.get("nome") or "").strip()
                if n and n not in vistos_n:
                    vistos_n.add(n)
                    nomes.append({"nome": n, "polo": str(d.get("polo") or "")})
        except (ValueError, AttributeError):
            pass
    if len(linhas) > 1:   # várias cópias: mostra o texto comum (sem o bloco de intimados de uma só delas); os intimados vão à parte
        base["texto"] = texto_base(base.get("texto"))
    base["copias"] = len(linhas)
    base["ids"] = [b["id"] for b in linhas]
    base["intimados"] = intim
    base["destinatarios_uniao"] = nomes
    return base
