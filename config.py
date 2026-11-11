"""Configuração local (arquivo JSON na pasta de dados)."""
import copy
import json
import os
import re
import threading
from pathlib import Path

import datajud

PADRAO = {
    "api_key": datajud.CHAVE_PUBLICA_INICIAL,
    "base_url": datajud.BASE_URL_PADRAO,
    "intervalo_horas": 6,
    "porta": 8765,
    "endpoints_override": {},
    "dje_ativo": True,
    "dje_base_url": "https://comunicaapi.pje.jus.br",
    # v0.5: varredura diária dos processos em blocos (cada processo consultado 1x por dia, dentro da janela de horário)
    "varredura": {"ativa": True, "bloco": 100, "intervalo_min": 30, "inicio": "07:00", "fim": "20:00"},
    "pasta_backup": "",          # vazio = <pasta de dados>/backups (pode ser uma pasta do Dropbox)
    "backup_ativo": True,
    "backup_hora": "02:00",
    "backup_manter": 30,
    "email": {
        "ativo": False,
        "smtp_host": "smtp.gmail.com",
        "smtp_porta": 465,
        "usuario": "",
        "senha": "",
        "remetente": "",
        "destinatario": "",
    },
}


def _mesclar(base, novo):
    for k, v in (novo or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _mesclar(base[k], v)
        else:
            base[k] = v
    return base


class Config:
    def __init__(self, caminho):
        self.caminho = Path(caminho)
        self.lock = threading.Lock()
        self.dados = copy.deepcopy(PADRAO)
        if self.caminho.exists():
            try:
                _mesclar(self.dados, json.loads(self.caminho.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass

    def salvar(self):
        with self.lock:
            self.caminho.parent.mkdir(parents=True, exist_ok=True)
            self.caminho.write_text(json.dumps(self.dados, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                os.chmod(self.caminho, 0o600)
            except OSError:
                pass

    def publico(self):
        """Visão para a interface: nunca devolve a senha do e-mail."""
        d = copy.deepcopy(self.dados)
        d["email"]["senha_definida"] = bool(d["email"].get("senha"))
        d["email"].pop("senha", None)
        d.pop("codigo_acesso", None)
        return d

    def atualizar(self, novo):
        """Aplica alterações vindas da interface (valida e preserva a senha se vier vazia)."""
        with self.lock:
            d = self.dados
            if "api_key" in novo:
                chave = str(novo["api_key"]).strip()
                if not chave:
                    raise ValueError("A chave da API não pode ficar vazia.")
                d["api_key"] = chave
            if "intervalo_horas" in novo:
                h = float(novo["intervalo_horas"])
                if h != 0 and not (1 <= h <= 168):
                    raise ValueError("Intervalo deve ser 0 (desligado) ou entre 1 e 168 horas.")
                d["intervalo_horas"] = h
            if "dje_ativo" in novo:
                d["dje_ativo"] = bool(novo["dje_ativo"])
            v = novo.get("varredura")
            if isinstance(v, dict):
                nv = dict(d["varredura"])
                if "ativa" in v:
                    nv["ativa"] = bool(v["ativa"])
                if "bloco" in v:
                    nv["bloco"] = int(v["bloco"])
                    if not 10 <= nv["bloco"] <= 500:
                        raise ValueError("O tamanho do bloco deve ficar entre 10 e 500 processos.")
                if "intervalo_min" in v:
                    nv["intervalo_min"] = int(v["intervalo_min"])
                    if not 5 <= nv["intervalo_min"] <= 240:
                        raise ValueError("O intervalo entre blocos deve ficar entre 5 e 240 minutos.")
                for campo in ("inicio", "fim"):
                    if campo in v:
                        hora = str(v[campo]).strip()
                        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", hora):
                            raise ValueError("Horário da varredura inválido (use HH:MM).")
                        nv[campo] = hora
                if nv["inicio"] >= nv["fim"]:
                    raise ValueError("O horário de início da varredura deve ser anterior ao de fim.")
                d["varredura"] = nv
            if "pasta_backup" in novo:
                d["pasta_backup"] = str(novo["pasta_backup"]).strip()[:300]
            if "backup_ativo" in novo:
                d["backup_ativo"] = bool(novo["backup_ativo"])
            if "backup_hora" in novo:
                hora = str(novo["backup_hora"]).strip()
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", hora):
                    raise ValueError("Hora do backup inválida (use HH:MM, ex.: 02:00).")
                d["backup_hora"] = hora
            if "backup_manter" in novo:
                n = int(novo["backup_manter"])
                if not 1 <= n <= 365:
                    raise ValueError("Quantidade de backups guardados deve ficar entre 1 e 365.")
                d["backup_manter"] = n
            em = novo.get("email")
            if isinstance(em, dict):
                for campo in ("smtp_host", "usuario", "remetente", "destinatario"):
                    if campo in em:
                        d["email"][campo] = str(em[campo]).strip()
                if "smtp_porta" in em:
                    d["email"]["smtp_porta"] = int(em["smtp_porta"])
                if "ativo" in em:
                    d["email"]["ativo"] = bool(em["ativo"])
                if em.get("senha"):
                    d["email"]["senha"] = str(em["senha"])
        self.salvar()
