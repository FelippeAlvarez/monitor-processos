"""Cálculo de prazo em dias úteis (v0.6) — FERRAMENTA DE APOIO, não substitui a conferência nos portais.

Regras (conferidas no texto legal em 23/11/2026):
- CPC art. 219: prazo em dias conta só dias úteis (prazos processuais).
- CPC art. 224, caput: exclui o dia do começo e inclui o do vencimento.
- CPC art. 224, § 2º e Lei 11.419/2006 art. 4º, § 3º: considera-se publicação o primeiro dia útil seguinte ao da disponibilização.
- CPC art. 224, § 3º e Lei 11.419/2006 art. 4º, § 4º: a contagem começa no primeiro dia útil que seguir ao da publicação.
- CPC art. 220: suspende-se o curso do prazo de 20/12 a 20/01, inclusive. PADRÃO ADOTADO: 20/12 a 19/01 (decisão do titular, 26/11/2026; prazo mais curto na dúvida; 20/01 conta como dia útil nacional). Configurável.
- Feriados nacionais: Lei 662/1949 (1/1, 21/4, 1/5, 7/9, 2/11, 15/11, 25/12), Lei 6.802/1980 (12/10) e Lei 14.759/2023 (20/11).
Só entram feriados nacionais (e os "extras" que a administração cadastrar). Na dúvida, o programa escolhe o prazo MAIS CURTO.

Interpretação adotada (não está escrita na lei): a data da publicação usa dias úteis comuns (fim de semana e feriados);
o recesso de fim de ano suspende só a CONTAGEM do prazo, não adia a data de publicação.
"""
import datetime as dt

FERIADOS_FIXOS = {
    (1, 1): "Confraternização Universal (Lei 662/1949)",
    (4, 21): "Tiradentes (Lei 662/1949)",
    (5, 1): "Dia do Trabalho (Lei 662/1949)",
    (9, 7): "Independência (Lei 662/1949)",
    (10, 12): "Nossa Senhora Aparecida (Lei 6.802/1980)",
    (11, 2): "Finados (Lei 662/1949)",
    (11, 15): "Proclamação da República (Lei 662/1949)",
    (11, 20): "Dia Nacional de Zumbi e da Consciência Negra (Lei 14.759/2023)",
    (12, 25): "Natal (Lei 662/1949)",
}
DIAS_SEMANA = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
RECESSO_PADRAO = {"ativo": True, "inicio": "12-20", "fim": "01-19"}
MAX_DIAS = 400
AVISO = ("Ferramenta de apoio: NÃO substitui a conferência nos portais do tribunal nem a análise do advogado. "
         "Considera apenas feriados nacionais (e os extras cadastrados em Configurações). Não considera feriados estaduais ou municipais, "
         "suspensões ou prorrogações por ato do tribunal, indisponibilidade do sistema, prazo em dobro ou diferenciado, nem o prazo que a lei "
         "ou o juiz fixou para o caso: confira tudo antes de se basear nesta data.")


class ErroPrazo(ValueError):
    pass


def _mmdd(txt):
    try:
        m, d = str(txt).split("-")
        m, d = int(m), int(d)
        dt.date(2024, m, d)  # valida (2024 é bissexto)
        return m, d
    except (ValueError, TypeError):
        raise ErroPrazo(f"Data de recesso inválida: {txt!r} (use MM-DD, ex.: 12-20).")


def data_de_texto(txt):
    try:
        return dt.date.fromisoformat(str(txt)[:10])
    except ValueError:
        raise ErroPrazo("Data inválida (use AAAA-MM-DD).")


class Calendario:
    def __init__(self, extras=None, recesso=None):
        self.extras = {}
        for e in (extras or []):
            if isinstance(e, dict):
                d, nome = e.get("data"), e.get("nome") or "feriado cadastrado"
            else:
                d, nome = (str(e).split(None, 1) + [""])[:2] if e else ("", "")
                nome = nome or "feriado cadastrado"
            try:
                self.extras[data_de_texto(d)] = nome
            except ErroPrazo:
                continue
        r = dict(RECESSO_PADRAO)
        r.update(recesso or {})
        self.recesso_ativo = bool(r["ativo"])
        self.rec_ini = _mmdd(r["inicio"])
        self.rec_fim = _mmdd(r["fim"])

    def feriado(self, d):
        nome = FERIADOS_FIXOS.get((d.month, d.day))
        if nome:
            return nome
        return self.extras.get(d)

    def em_recesso(self, d):
        if not self.recesso_ativo:
            return False
        atual, ini, fim = (d.month, d.day), self.rec_ini, self.rec_fim
        return (atual >= ini or atual <= fim) if ini > fim else (ini <= atual <= fim)

    def motivo_nao_util(self, d, contagem=True):
        """None se for dia útil. contagem=False ignora o recesso (usado só para achar a data da publicação)."""
        if d.weekday() >= 5:
            return DIAS_SEMANA[d.weekday()]
        f = self.feriado(d)
        if f:
            return "feriado: " + f
        if contagem and self.em_recesso(d):
            return "recesso forense (CPC art. 220)"
        return None

    def proximo_util(self, d, contagem=True):
        d += dt.timedelta(days=1)
        while self.motivo_nao_util(d, contagem):
            d += dt.timedelta(days=1)
        return d


def _br(d):
    return d.strftime("%d/%m/%Y")


def calcular(disponibilizacao, dias, calendario=None):
    """disponibilizacao: date. dias: int (dias úteis). Devolve dict com datas e a memória de cálculo."""
    if not isinstance(dias, int) or isinstance(dias, bool) or not 1 <= dias <= MAX_DIAS:
        raise ErroPrazo(f"Informe o número de dias úteis (1 a {MAX_DIAS}).")
    cal = calendario or Calendario()
    disp = disponibilizacao
    publicacao = cal.proximo_util(disp, contagem=False)
    inicio = cal.proximo_util(publicacao, contagem=True)
    contados, ignorados, d = [], [], inicio
    # dias ignorados entre a disponibilização e o vencimento (para a memória de cálculo)
    x = disp + dt.timedelta(days=1)
    while x < inicio:
        m = cal.motivo_nao_util(x, contagem=True)
        if m:
            ignorados.append((x, m))
        x += dt.timedelta(days=1)
    while len(contados) < dias:
        m = cal.motivo_nao_util(d, contagem=True)
        if m:
            ignorados.append((d, m))
        else:
            contados.append(d)
        d += dt.timedelta(days=1)
    venc = contados[-1]
    passos = [
        f"Disponibilização no DJEN: {_br(disp)} ({DIAS_SEMANA[disp.weekday()]}).",
        f"Publicação (1º dia útil seguinte à disponibilização — CPC art. 224, § 2º; Lei 11.419/2006, art. 4º, § 3º): {_br(publicacao)} ({DIAS_SEMANA[publicacao.weekday()]}).",
        f"Início da contagem (1º dia útil seguinte à publicação — CPC art. 224, § 3º; Lei 11.419/2006, art. 4º, § 4º): {_br(inicio)} ({DIAS_SEMANA[inicio.weekday()]}) = dia 1.",
        f"Prazo: {dias} dia(s) útil(eis), excluído o dia do começo e incluído o do vencimento (CPC arts. 219 e 224, caput).",
        f"Vencimento: {_br(venc)} ({DIAS_SEMANA[venc.weekday()]}) = dia {dias}.",
    ]
    if ignorados:
        passos.append("Dias não contados no caminho: " + "; ".join(f"{_br(x)} ({m})" for x, m in ignorados) + ".")
    if cal.recesso_ativo:
        passos.append(f"Recesso considerado: {cal.rec_ini[1]:02d}/{cal.rec_ini[0]:02d} a {cal.rec_fim[1]:02d}/{cal.rec_fim[0]:02d}, inclusive (CPC art. 220).")
    else:
        passos.append("Recesso de fim de ano DESLIGADO nas configurações.")
    return {"disponibilizacao": disp.isoformat(), "publicacao": publicacao.isoformat(), "inicio": inicio.isoformat(),
            "vencimento": venc.isoformat(), "dias": dias, "memoria": passos, "aviso": AVISO}
