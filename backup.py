"""Backup diário do banco (cópia consistente via API de backup do SQLite) e trava de instância única."""
import datetime as dt
import json
import os
import re
import socket
import sqlite3
import time
from pathlib import Path

PADRAO_NOME = re.compile(r"processos-(\d{8})-(\d{6})\.db")


def pasta_backup(config_dados, pasta_dados):
    p = (config_dados.get("pasta_backup") or "").strip()
    return Path(p) if p else Path(pasta_dados) / "backups"


def fazer_backup(banco, pasta, manter=30):
    """Copia o banco para <pasta>/processos-AAAAMMDD-HHMMSS.db e apaga os mais antigos além de 'manter'.
    Não copia config.json (contém a senha do e-mail). Devolve o caminho do arquivo criado."""
    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    nome = "processos-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".db"
    final = pasta / nome
    tmp = pasta / (nome + ".tmp")
    dst = sqlite3.connect(str(tmp))
    try:
        with banco.lock:
            banco.conn.backup(dst)
    finally:
        dst.close()
    os.replace(tmp, final)
    try:
        os.chmod(final, 0o600)
    except OSError:
        pass
    antigos = sorted(p for p in pasta.iterdir() if PADRAO_NOME.fullmatch(p.name))
    for p in antigos[:-max(1, int(manter))]:
        try:
            p.unlink()
        except OSError:
            pass
    return final


def listar_backups(pasta):
    pasta = Path(pasta)
    if not pasta.exists():
        return []
    itens = []
    for p in sorted((x for x in pasta.iterdir() if PADRAO_NOME.fullmatch(x.name)), reverse=True):
        m = PADRAO_NOME.fullmatch(p.name)
        quando = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        itens.append({"arquivo": p.name, "quando": quando.strftime("%Y-%m-%dT%H:%M:%S"), "bytes": p.stat().st_size})
    return itens


def backup_devido(pasta, hora="02:00", agora=None):
    """True se ainda não há backup de hoje e já passou da hora configurada."""
    agora = agora or dt.datetime.now()
    try:
        h, m = (int(x) for x in str(hora).split(":"))
    except ValueError:
        h, m = 2, 0
    if (agora.hour, agora.minute) < (h, m):
        return False
    hoje = agora.strftime("%Y%m%d")
    return not any(b["arquivo"].startswith("processos-" + hoje) for b in listar_backups(pasta))


class TravaInstancia:
    """Impede que dois computadores rodem o programa sobre a MESMA pasta de dados (o que corromperia o banco,
    principalmente em pasta sincronizada como o Dropbox). Arquivo 'servidor.lock' com batimento a cada 30 s."""
    VALIDADE = 120

    def __init__(self, pasta):
        self.arq = Path(pasta) / "servidor.lock"
        self.meu = {"host": socket.gethostname(), "pid": os.getpid()}

    def _ler(self):
        try:
            return json.loads(self.arq.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def adquirir(self, ignorar=False):
        """Devolve None se conseguiu; senão, texto explicando quem está usando a pasta."""
        atual = self._ler()
        if atual and not ignorar and atual.get("pid") != self.meu["pid"]:
            recente = time.time() - float(atual.get("quando", 0)) < self.VALIDADE
            mesma_maquina = atual.get("host") == self.meu["host"]
            vivo = True
            if mesma_maquina and os.name == "posix":  # no Windows, os.kill(pid, 0) ENCERRARIA o processo: só o batimento vale
                try:
                    os.kill(int(atual["pid"]), 0)
                except (OSError, ValueError, KeyError):
                    vivo = False
                except SystemError:
                    vivo = True
            if recente and vivo:
                return (f"Esta pasta de dados já está em uso pelo computador '{atual.get('host')}' "
                        f"(processo {atual.get('pid')}). Feche o programa lá antes de abrir aqui. "
                        "Dois computadores na mesma pasta corrompem o banco de dados.")
        self.batimento()
        return None

    def batimento(self):
        try:
            self.arq.write_text(json.dumps({**self.meu, "quando": time.time()}), encoding="utf-8")
        except OSError:
            pass

    def liberar(self):
        atual = self._ler()
        if atual and atual.get("pid") == self.meu["pid"] and atual.get("host") == self.meu["host"]:
            try:
                self.arq.unlink()
            except OSError:
                pass
