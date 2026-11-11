"""Cliente da API Pública do DataJud (CNJ).

Funções principais:
  - interpretar_numero(): valida o número CNJ (inclusive dígito verificador).
  - alias_tribunal(): descobre o índice (endpoint) do tribunal pelos campos J.TR.
  - consultar(): consulta a API e devolve os dados já normalizados.

ATENÇÃO: os nomes de índice de cada tribunal seguem o padrão da wiki do DataJud
(api_publica_<tribunal>). Os de TJRJ, TRT3 e TJPE foram vistos em fontes públicas;
os demais são derivados do padrão e devem ser confirmados no primeiro uso.
"""
import hashlib
import http.client
import json
import logging
import re
import urllib.error
import urllib.request

log = logging.getLogger("monitor.datajud")

BASE_URL_PADRAO = "https://api-publica.datajud.cnj.jus.br"

# Chave pública divulgada pelo CNJ (vigente em 01/11/2026). O CNJ pode trocá-la a
# qualquer momento: a versão atual fica em https://datajud-wiki.cnj.jus.br/api-publica/acesso
CHAVE_PUBLICA_INICIAL = "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw=="

_RE_CNJ = re.compile(r"^\s*(\d{7})-?(\d{2})\.?(\d{4})\.?(\d)\.?(\d{2})\.?(\d{4})\s*$")
RE_CNJ_BUSCA = re.compile(r"\d{7}-?\d{2}\.?\d{4}\.?\d\.?\d{2}\.?\d{4}")

# Código TR (justiça estadual/eleitoral) -> sigla da UF
_UFS = {1: "ac", 2: "al", 3: "ap", 4: "am", 5: "ba", 6: "ce", 7: "dft", 8: "es",
        9: "go", 10: "ma", 11: "mt", 12: "ms", 13: "mg", 14: "pa", 15: "pb",
        16: "pr", 17: "pe", 18: "pi", 19: "rj", 20: "rn", 21: "rs", 22: "ro",
        23: "rr", 24: "sc", 25: "se", 26: "sp", 27: "to"}


class ErroNumero(ValueError):
    pass


class TribunalNaoSuportado(ValueError):
    pass


class ErroAPI(Exception):
    def __init__(self, mensagem, codigo=None, retry_after=None, transitorio=False):
        super().__init__(mensagem)
        self.codigo = codigo
        self.retry_after = retry_after  # segundos sugeridos pela API (cabeçalho Retry-After)
        self.transitorio = transitorio


class LoteIncompleto(Exception):
    """A resposta em lote pode estar truncada ou fora do formato esperado."""


def calcular_dv(sequencial, ano, j, tr, origem):
    """Dígito verificador do número CNJ (Resolução CNJ 65/2008, módulo 97)."""
    base = f"{sequencial}{ano}{j}{tr}{origem}00"
    return 98 - (int(base) % 97)


def interpretar_numero(texto):
    m = _RE_CNJ.match(texto or "")
    if not m:
        raise ErroNumero("Número fora do padrão CNJ (NNNNNNN-DD.AAAA.J.TR.OOOO).")
    seq, dv, ano, j, tr, origem = m.groups()
    if calcular_dv(seq, ano, j, tr, origem) != int(dv):
        raise ErroNumero("Dígito verificador inválido: confira se há erro de digitação.")
    return {
        "numero": f"{seq}-{dv}.{ano}.{j}.{tr}.{origem}",
        "limpo": f"{seq}{dv}{ano}{j}{tr}{origem}",
        "j": int(j),
        "tr": int(tr),
    }


def alias_tribunal(j, tr):
    """Devolve o sufixo do índice (ex.: 'tjrj'). Levanta TribunalNaoSuportado."""
    if j == 3 and tr == 0:
        return "stj"
    if j == 4 and 1 <= tr <= 6:
        return f"trf{tr}"
    if j == 5:
        if tr == 0:
            return "tst"
        if 1 <= tr <= 24:
            return f"trt{tr}"
    if j == 6:
        if tr == 0:
            return "tse"
        if tr in _UFS:
            return f"tre-{_UFS[tr]}"
    if j == 7 and tr == 0:
        return "stm"
    if j == 8 and tr in _UFS:
        return f"tj{_UFS[tr]}"
    if j == 9 and tr in (13, 21, 26):
        return {13: "tjmmg", 21: "tjmrs", 26: "tjmsp"}[tr]
    if j == 1:
        raise TribunalNaoSuportado("STF: não há índice na API pública do DataJud.")
    raise TribunalNaoSuportado(f"Tribunal não mapeado (J={j}, TR={tr:02d}).")


def normalizar_data(valor):
    s = str(valor or "").strip()
    if re.fullmatch(r"\d{14}", s):
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}:{s[12:14]}"
    return s


def _complementos(mov):
    partes = []
    for c in mov.get("complementosTabelados") or []:
        nome = str(c.get("nome") or "").strip()
        desc = str(c.get("descricao") or c.get("valor") or "").strip()
        if nome or desc:
            partes.append(f"{nome}: {desc}" if nome and desc else (nome or desc))
    return "; ".join(partes)


def normalizar(fontes):
    """Une os registros (_source) de um mesmo processo (graus/instâncias diferentes)."""
    if not fontes:
        return None
    principal = max(fontes, key=lambda f: str(f.get("dataHoraUltimaAtualizacao") or ""))
    movimentos = {}
    for f in fontes:
        grau = str(f.get("grau") or "")
        for m in f.get("movimentos") or []:
            codigo = str(m.get("codigo") or "")
            nome = str(m.get("nome") or "")
            data = normalizar_data(m.get("dataHora"))
            compl = _complementos(m)
            chave = hashlib.sha1(f"{grau}|{codigo}|{data}|{nome}|{compl}".encode("utf-8")).hexdigest()[:20]
            movimentos.setdefault(chave, {"chave": chave, "grau": grau, "codigo": codigo,
                                          "nome": nome, "data_hora": data, "complementos": compl})
    ordenados = sorted(movimentos.values(), key=lambda x: x["data_hora"], reverse=True)
    return {
        "classe": str((principal.get("classe") or {}).get("nome") or ""),
        "orgao": str((principal.get("orgaoJulgador") or {}).get("nome") or ""),
        "ultima_atualizacao": normalizar_data(principal.get("dataHoraUltimaAtualizacao")),
        "movimentos": ordenados,
    }


def _retry_after(valor):
    try:
        return max(0, int(str(valor).strip()))
    except (TypeError, ValueError):
        return None


def _post(url, payload, api_key, timeout):
    """POST JSON. Qualquer falha de rede/HTTP vira ErroAPI (nunca escapa outra exceção)."""
    corpo = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=corpo, method="POST", headers={
        "Authorization": f"APIKey {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "MonitorProcessos/0.1",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        ra = _retry_after(e.headers.get("Retry-After") if e.headers else None)
        if e.code in (401, 403):
            raise ErroAPI("Chave da API recusada (401/403). O CNJ pode ter trocado a chave: "
                          "atualize em Configurações.", e.code)
        if e.code == 404:
            raise ErroAPI("Índice do tribunal não encontrado (404). "
                          "Confirme o nome do índice na wiki do DataJud.", e.code)
        if e.code == 429:
            raise ErroAPI("Limite de requisições da API atingido (429).", e.code, retry_after=ra, transitorio=True)
        raise ErroAPI(f"Erro HTTP {e.code} da API do DataJud.", e.code, retry_after=ra, transitorio=e.code >= 500)
    except urllib.error.URLError as e:
        raise ErroAPI(f"Sem conexão com a API do DataJud: {e.reason}", transitorio=True)
    except (OSError, http.client.HTTPException) as e:  # timeout, conexão encerrada, etc.
        raise ErroAPI(f"Falha de comunicação com a API ({type(e).__name__}): {e}", transitorio=True)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ErroAPI("Resposta inválida da API do DataJud.", transitorio=True)


def _url(base_url, alias):
    return f"{base_url.rstrip('/')}/api_publica_{alias}/_search"


def _verificar_completa(dados, rotulo=""):
    """O DataJud é um Elasticsearch: sob carga ele pode devolver HTTP 200 com resultado PARCIAL
    (timed_out=true ou fragmentos com falha). Sem esta checagem, processos existentes pareciam
    'não encontrados' e o histórico-base ficava incompleto (gerando centenas de 'novas')."""
    hits = (dados.get("hits") or {}).get("hits") or []
    sh = dados.get("_shards") or {}
    total, ok = sh.get("total"), sh.get("successful")
    log.info("%s: took=%sms timed_out=%s shards=%s/%s falhos=%s hits=%d", rotulo, dados.get("took"),
             dados.get("timed_out"), ok, total, sh.get("failed"), len(hits))
    falhos = int(sh.get("failed") or 0)
    incompleto = isinstance(total, int) and isinstance(ok, int) and (ok + int(sh.get("skipped") or 0)) < total
    if dados.get("timed_out") or falhos or incompleto:
        raise ErroAPI("Resposta parcial do CNJ (a base estourou o tempo interno); será repetida.",
                      codigo="parcial", transitorio=True)


def consultar(numero_limpo, alias, api_key, base_url=BASE_URL_PADRAO, timeout=120):
    """Consulta um processo. Devolve dict normalizado ou None se não encontrado."""
    dados = _post(_url(base_url, alias),
                  {"size": 10, "query": {"match": {"numeroProcesso": numero_limpo}}}, api_key, timeout)
    _verificar_completa(dados, f"{alias} {numero_limpo}")
    hits = (dados.get("hits") or {}).get("hits") or []
    return normalizar([h.get("_source") or {} for h in hits])


def consultar_lote(numeros_limpos, alias, api_key, base_url=BASE_URL_PADRAO, timeout=120, tamanho=100):
    """Consulta vários processos do MESMO tribunal numa só requisição (consulta 'terms').

    Devolve {numero_limpo: dict normalizado ou None}. Levanta LoteIncompleto se a resposta
    puder estar truncada ou sem o campo numeroProcesso (o chamador então consulta um a um).
    """
    dados = _post(_url(base_url, alias),
                  {"size": tamanho, "query": {"terms": {"numeroProcesso": list(numeros_limpos)}}},
                  api_key, timeout)
    _verificar_completa(dados, f"lote {alias} x{len(numeros_limpos)}")
    hits = (dados.get("hits") or {}).get("hits") or []
    if len(hits) >= tamanho:
        raise LoteIncompleto("resposta pode estar truncada")
    grupos = {}
    for h in hits:
        fonte = h.get("_source") or {}
        n = re.sub(r"\D", "", str(fonte.get("numeroProcesso") or ""))
        if not n:
            raise LoteIncompleto("registro sem numeroProcesso")
        grupos.setdefault(n, []).append(fonte)
    desconhecidos = set(grupos) - set(numeros_limpos)
    if desconhecidos:
        raise LoteIncompleto("resposta com processos não solicitados")
    return {n: normalizar(grupos.get(n)) for n in numeros_limpos}
