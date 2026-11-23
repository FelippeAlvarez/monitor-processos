// Instalar-MonitorProcessos.exe — instala/atualiza o Monitor de Processos em %LOCALAPPDATA%\MonitorProcessos.
//
// Regras que protegem o atalho e os dados (ver MANUAL, "Atualizações"):
//   - o atalho aponta sempre para <raiz>\MonitorProcessos.exe, que nunca muda de nome nem de lugar;
//   - a atualização troca só a pasta <raiz>\app (a versão anterior fica em <raiz>\app.anterior);
//   - <raiz>\dados e <raiz>\local.json nunca são apagados nem sobrescritos;
//   - no fim, o instalador confere se os atalhos existem e se o alvo existe; se faltar, recria.
package main

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"asa/internal/inst"
)

//go:embed payload/app.zip
var pacoteApp []byte

//go:embed payload/MonitorProcessos.exe
var launcherExe []byte

//go:embed payload/monitor.ico
var icone []byte

// Versao é preenchida na compilação (-ldflags "-X main.Versao=0.6.1").
var Versao = "0.0.0"

const (
	nomeApp   = "Monitor de Processos"
	titulo    = "Monitor de Processos — Instalação"
	exeNome   = "MonitorProcessos.exe"
	instNome  = "Instalador.exe"
	arqVersao = "instalacao.json"
)

func raiz() string {
	if r := os.Getenv("MONITOR_INSTALL_DIR"); r != "" { // usado nos testes
		return r
	}
	base := os.Getenv("LOCALAPPDATA")
	if base == "" {
		h, _ := os.UserHomeDir()
		base = filepath.Join(h, "AppData", "Local")
	}
	return filepath.Join(base, "MonitorProcessos")
}

func versaoInstalada(r string) string {
	b, err := os.ReadFile(filepath.Join(r, arqVersao))
	if err != nil {
		return ""
	}
	var v struct {
		Versao string `json:"versao"`
	}
	if json.Unmarshal(b, &v) != nil {
		return ""
	}
	return v.Versao
}

func emUso(r string) error {
	exe := filepath.Join(r, exeNome)
	if _, err := os.Stat(exe); err != nil {
		return nil
	}
	f, err := os.OpenFile(exe, os.O_WRONLY, 0)
	if err != nil {
		return err
	}
	return f.Close()
}

func executa(args []string) (string, error) {
	c := execCmd(args)
	out, err := c.CombinedOutput()
	return string(out), err
}

func main() {
	if len(os.Args) > 1 && strings.EqualFold(os.Args[1], "/desinstalar") {
		if err := desinstalar(); err != nil {
			msgErro(titulo, "A remoção não terminou:\n\n"+err.Error())
			os.Exit(1)
		}
		return
	}
	if err := instalar(); err != nil {
		msgErro(titulo, "A instalação não terminou:\n\n"+err.Error()+"\n\nNada foi apagado dos seus dados. Você pode tentar de novo.")
		os.Exit(1)
	}
}

func instalar() error {
	r := raiz()
	anterior := versaoInstalada(r)
	var pergunta string
	if anterior == "" {
		pergunta = fmt.Sprintf("Instalar o %s %s?\n\nLocal: %s\n\nSerá criado um atalho na Área de Trabalho e no Menu Iniciar.", nomeApp, Versao, r)
	} else {
		pergunta = fmt.Sprintf("Atualizar o %s da versão %s para a %s?\n\nSeus dados e o atalho serão preservados. A versão atual fica guardada para o caso de ser preciso voltar.", nomeApp, anterior, Versao)
	}
	if !msgSimNao(titulo, pergunta) {
		return nil
	}

	py := inst.EncontrarPython(executa, pythonsConhecidos())
	if py == nil {
		if !msgSimNao(titulo, "O Python 3.9 ou superior não foi encontrado neste computador, e o Monitor precisa dele para rodar.\n\nVocê pode instalar o Python depois (https://www.python.org/downloads/ — marque \"Add python.exe to PATH\"). Continuar a instalação mesmo assim?") {
			return nil
		}
	}

	if err := os.MkdirAll(r, 0o755); err != nil {
		return fmt.Errorf("não consegui criar %s: %w", r, err)
	}
	for {
		err := emUso(r)
		if err == nil {
			break
		}
		if !msgTentar(titulo, "O Monitor de Processos parece estar aberto.\n\nFeche a janela preta do programa (e o navegador não precisa fechar) e clique em Tentar novamente.") {
			return nil
		}
	}

	novo := filepath.Join(r, "app.novo")
	_ = os.RemoveAll(novo)
	if err := inst.ExtractZip(pacoteApp, novo); err != nil {
		_ = os.RemoveAll(novo)
		return fmt.Errorf("pacote do programa: %w", err)
	}
	for {
		err := inst.SwapApp(r, novo)
		if err == nil {
			break
		}
		if !msgTentar(titulo, "Não consegui trocar os arquivos do programa:\n\n"+err.Error()+"\n\nFeche o Monitor de Processos e tente de novo.") {
			_ = os.RemoveAll(novo)
			return nil
		}
	}

	if _, err := inst.EscreverSeDiferente(filepath.Join(r, exeNome), launcherExe, 0o755); err != nil {
		return fmt.Errorf("não consegui gravar %s: %w", exeNome, err)
	}
	if _, err := inst.EscreverSeDiferente(filepath.Join(r, "monitor.ico"), icone, 0o644); err != nil {
		return err
	}
	if self, err := os.Executable(); err == nil {
		if b, err := os.ReadFile(self); err == nil && !strings.EqualFold(self, filepath.Join(r, instNome)) {
			_, _ = inst.EscreverSeDiferente(filepath.Join(r, instNome), b, 0o755)
		}
	}

	avisoDados := migrarDados(r)

	atalhos, err := criarAtalhos(r, filepath.Join(r, exeNome))
	if err != nil {
		return fmt.Errorf("não consegui criar os atalhos: %w", err)
	}
	var faltam []string
	for _, a := range atalhos {
		if _, e := os.Stat(a); e != nil {
			faltam = append(faltam, a)
		}
	}
	if _, e := os.Stat(filepath.Join(r, exeNome)); e != nil {
		return fmt.Errorf("o programa não ficou em %s", r)
	}
	_ = registrarDesinstalacao(r, Versao)
	j, _ := json.Marshal(map[string]string{"versao": Versao, "atualizado_em": time.Now().Format(time.RFC3339)})
	_ = os.WriteFile(filepath.Join(r, arqVersao), j, 0o644)

	fim := fmt.Sprintf("%s %s instalado.\n\nAbra pelo atalho \"%s\" na Área de Trabalho.", nomeApp, Versao, nomeApp)
	if anterior != "" {
		fim = fmt.Sprintf("%s atualizado para a %s.\n\nO atalho continua o mesmo.", nomeApp, Versao)
	}
	if avisoDados != "" {
		fim += "\n\n" + avisoDados
	}
	if len(faltam) > 0 {
		fim += "\n\nATENÇÃO: não consegui confirmar este atalho:\n" + strings.Join(faltam, "\n") + "\nAbra o programa direto em: " + filepath.Join(r, exeNome)
	}
	if py == nil {
		fim += "\n\nLembrete: falta instalar o Python 3.9+ (python.org) antes do primeiro uso."
	}
	fim += "\n\nAbrir o programa agora?"
	if msgSimNao(titulo, fim) {
		abrir(filepath.Join(r, exeNome), r)
	}
	return nil
}

// migrarDados trata a PRIMEIRA instalação: se já existir uma pasta de dados de uma versão antiga (instalada à mão),
// oferece usá-la ou copiá-la. Nunca mexe em <raiz>\dados se já existir, e nunca apaga a origem.
func migrarDados(r string) string {
	if _, err := os.Stat(filepath.Join(r, "dados", "processos.db")); err == nil {
		return ""
	}
	if _, err := os.Stat(filepath.Join(r, "local.json")); err == nil {
		return ""
	}
	externa, banco := procurarDadosAntigos(r)
	if externa != "" {
		if msgSimNao(titulo, "Encontrei uma instalação antiga do Monitor configurada para guardar os dados em:\n\n"+externa+"\n\nUsar essa mesma pasta de dados na nova instalação? (Recomendado se a equipe já usa esses dados.)") {
			j, _ := json.Marshal(map[string]string{"pasta_dados": externa})
			if err := os.WriteFile(filepath.Join(r, "local.json"), j, 0o644); err != nil {
				return "Não consegui gravar o local.json: " + err.Error()
			}
			return "A nova instalação vai usar os dados em: " + externa
		}
	}
	if banco != "" {
		origem := filepath.Dir(banco)
		if msgSimNao(titulo, "Encontrei dados de uma instalação antiga em:\n\n"+origem+"\n\nCopiar esses dados para a nova instalação? (A pasta antiga não é alterada.)\n\nFeche antes o Monitor antigo, se estiver aberto.") {
			if err := inst.CopiarPasta(origem, filepath.Join(r, "dados")); err != nil {
				return "Não consegui copiar os dados antigos: " + err.Error() + "\nOs dados antigos continuam intactos em " + origem
			}
			return "Dados copiados de " + origem
		}
	}
	return ""
}

func procurarDadosAntigos(r string) (externa string, banco string) {
	var bancos []string
	for _, base := range pastasUsuario() {
		for _, pad := range []string{
			filepath.Join(base, "MonitorProcessos*"),
			filepath.Join(base, "MonitorProcessos*", "MonitorProcessos"),
			filepath.Join(base, "Monitor*", "monitor-processos*"),
		} {
			pastas, _ := filepath.Glob(pad)
			for _, p := range pastas {
				if strings.EqualFold(filepath.Clean(p), filepath.Clean(r)) {
					continue
				}
				if b, err := os.ReadFile(filepath.Join(p, "local.json")); err == nil && externa == "" {
					var c struct {
						Pasta string `json:"pasta_dados"`
					}
					if json.Unmarshal(b, &c) == nil && strings.TrimSpace(c.Pasta) != "" {
						if _, e := os.Stat(filepath.Join(c.Pasta, "processos.db")); e == nil {
							externa = c.Pasta
						}
					}
				}
				bd := filepath.Join(p, "dados", "processos.db")
				if _, e := os.Stat(bd); e == nil {
					bancos = append(bancos, bd)
				}
			}
		}
	}
	return externa, inst.MelhorDados(bancos)
}

func desinstalar() error {
	r := raiz()
	if !msgSimNao(titulo, fmt.Sprintf("Remover o %s deste computador?\n\nOs atalhos e os programas serão removidos. Seus DADOS (processos, usuários, configurações) NÃO serão apagados: ficam em\n%s\npara o caso de você reinstalar.", nomeApp, filepath.Join(r, "dados"))) {
		return nil
	}
	for {
		err := emUso(r)
		if err == nil {
			break
		}
		if !msgTentar(titulo, "O Monitor de Processos parece estar aberto. Feche a janela preta do programa e tente novamente.") {
			return nil
		}
	}
	removerAtalhos(r, filepath.Join(r, exeNome))
	removerRegistro()
	for _, n := range []string{"app", "app.anterior", "app.novo"} {
		if err := os.RemoveAll(filepath.Join(r, n)); err != nil {
			return err
		}
	}
	for _, n := range []string{exeNome, exeNome + ".novo", "monitor.ico", arqVersao, "atalhos.txt"} {
		_ = os.Remove(filepath.Join(r, n))
	}
	agendarAutoExclusao(r)
	msgInfo(titulo, "Programa removido.\n\nSeus dados continuam em:\n"+filepath.Join(r, "dados")+"\n(apague essa pasta à mão, se quiser eliminá-los de vez).")
	return nil
}
