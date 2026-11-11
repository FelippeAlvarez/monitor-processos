"""Importação de listas de processos (planilha .xlsx, .csv, .txt ou .html/.xls exportado como página web).

Procura números CNJ em qualquer célula. Se a planilha tiver um cabeçalho com colunas chamadas "Cliente", "Pasta" ou "Apelido",
os valores dessas colunas acompanham os processos da mesma linha. Só biblioteca padrão. O formato .xls binário antigo
(Excel 97-2003) não é lido: salve como .xlsx ou copie e cole as células na aba Adicionar."""
import csv
import io
import re
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

import datajud

LIMITE_DESCOMPACTADO = 60 * 1024 * 1024  # proteção contra arquivos .xlsx "bomba"
MAX_LINHAS = 50000


class ErroImportacao(ValueError):
    pass


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _col(ref):
    letras = re.match(r"[A-Za-z]+", ref or "")
    n = 0
    for c in (letras.group(0).upper() if letras else "A"):
        n = n * 26 + ord(c) - 64
    return n - 1


def _texto_no(no):
    return "".join(t.text or "" for t in no.iter() if _local(t.tag) == "t")


def _ler_xlsx(dados):
    try:
        z = zipfile.ZipFile(io.BytesIO(dados))
    except zipfile.BadZipFile:
        raise ErroImportacao("Arquivo .xlsx inválido ou corrompido.")
    total = sum(i.file_size for i in z.infolist())
    if total > LIMITE_DESCOMPACTADO:
        raise ErroImportacao("Planilha grande demais.")
    compartilhadas = []
    if "xl/sharedStrings.xml" in z.namelist():
        raiz = ET.fromstring(z.read("xl/sharedStrings.xml"))
        compartilhadas = [_texto_no(si) for si in raiz if _local(si.tag) == "si"]
    linhas = []
    for nome in sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
        raiz = ET.fromstring(z.read(nome))
        for row in raiz.iter():
            if _local(row.tag) != "row":
                continue
            celulas = {}
            for c in row:
                if _local(c.tag) != "c":
                    continue
                tipo = c.get("t")
                v = next((x for x in c if _local(x.tag) == "v"), None)
                if tipo == "inlineStr":
                    valor = _texto_no(c)
                elif tipo == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(compartilhadas):
                    valor = compartilhadas[int(v.text)]
                else:
                    valor = (v.text or "") if v is not None else ""
                celulas[_col(c.get("r"))] = valor.strip()
            if celulas:
                linhas.append([celulas.get(i, "") for i in range(max(celulas) + 1)])
            if len(linhas) > MAX_LINHAS:
                raise ErroImportacao("Linhas demais na planilha.")
    return linhas


class _Tabela(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.linhas, self._linha, self._cel = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._linha = []
        elif tag in ("td", "th") and self._linha is not None:
            self._cel = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cel is not None and self._linha is not None:
            self._linha.append(" ".join("".join(self._cel).split()))
            self._cel = None
        elif tag == "tr" and self._linha is not None:
            if any(self._linha):
                self.linhas.append(self._linha)
            self._linha = None

    def handle_data(self, data):
        if self._cel is not None:
            self._cel.append(data)


def _decodificar(dados):
    if dados[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return dados.decode("utf-16")
    try:
        return dados.decode("utf-8-sig")
    except UnicodeDecodeError:
        return dados.decode("latin-1")


def ler_linhas(dados):
    """Devolve a planilha como lista de linhas (listas de textos)."""
    if not dados:
        raise ErroImportacao("Arquivo vazio.")
    if dados[:4] == b"PK\x03\x04":
        return _ler_xlsx(dados)
    if dados[:4] == b"\xd0\xcf\x11\xe0":
        raise ErroImportacao("Este é o formato .xls antigo (Excel 97-2003), que o programa não lê. Abra no Excel e salve como "
                             ".xlsx (ou .csv), ou copie as células e cole na aba Adicionar.")
    texto = _decodificar(dados)
    if re.search(r"<\s*(table|tr)\b", texto, re.I):
        p = _Tabela()
        p.feed(texto)
        return p.linhas
    amostra = "\n".join(texto.splitlines()[:20])
    try:
        dialeto = csv.Sniffer().sniff(amostra, delimiters=";,\t|")
    except csv.Error:
        dialeto = csv.excel
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(texto), dialeto) if any(c.strip() for c in r)]


def _colunas(linhas):
    """Procura, nas 5 primeiras linhas, um cabeçalho com Cliente/Pasta/Apelido. Devolve (índices, índice da linha de cabeçalho)."""
    for i, lin in enumerate(linhas[:5]):
        mapa = {}
        for j, c in enumerate(lin):
            t = c.strip().lower()
            if "cliente" in t and "cliente" not in mapa:
                mapa["cliente"] = j
            elif t.startswith("pasta") and "pasta" not in mapa:
                mapa["pasta"] = j
            elif t in ("apelido", "título", "titulo") and "apelido" not in mapa:
                mapa["apelido"] = j
        if mapa:
            return mapa, i
    return {}, -1


def analisar(dados):
    """Lê o arquivo e devolve {'validos': [...], 'invalidos': [...], 'colunas': [...], 'linhas': n}.
    validos: dicts {numero, limpo, alias, cliente, pasta, apelido} sem repetição (vale a 1ª ocorrência)."""
    linhas = ler_linhas(dados)
    mapa, ini = _colunas(linhas)
    validos, invalidos, vistos = [], [], set()
    for i, lin in enumerate(linhas):
        if i <= ini:
            continue
        achados = datajud.RE_CNJ_BUSCA.findall("\n".join(lin))
        if not achados:
            continue
        extra = {k: (lin[j].strip()[:120] if j < len(lin) else "") for k, j in mapa.items()}
        if len(achados) > 1:
            extra["apelido"] = ""  # apelido só faz sentido para um processo por linha
        for n in achados:
            try:
                info = datajud.interpretar_numero(n)
                alias = datajud.alias_tribunal(info["j"], info["tr"])
            except (datajud.ErroNumero, datajud.TribunalNaoSuportado) as e:
                if n not in {x["numero"] for x in invalidos}:
                    invalidos.append({"numero": n, "motivo": str(e)})
                continue
            if info["limpo"] in vistos:
                continue
            vistos.add(info["limpo"])
            validos.append({"numero": info["numero"], "limpo": info["limpo"], "alias": alias,
                            "cliente": extra.get("cliente", ""), "pasta": extra.get("pasta", "")[:60],
                            "apelido": extra.get("apelido", "")})
    return {"validos": validos, "invalidos": invalidos, "colunas": sorted(mapa), "linhas": len(linhas)}
