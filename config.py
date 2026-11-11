"""Configuração local (arquivo JSON na pasta de dados)."""
import copy
import json
import os
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
