// MonitorProcessos.exe — launcher estável. O atalho da Área de Trabalho aponta SEMPRE para este arquivo,
// que nunca muda de nome nem de lugar; as atualizações trocam só a pasta "app" ao lado dele.
package main

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"

	"asa/internal/inst"
)

func pausa(msg string) {
	fmt.Println()
	fmt.Println(msg)
	fmt.Println()
	fmt.Print("Pressione Enter para fechar...")
	bufio.NewReader(os.Stdin).ReadString('\n')
}

func executa(args []string) (string, error) {
	c := exec.Command(args[0], args[1:]...)
	out, err := c.CombinedOutput()
	return string(out), err
}

func main() {
	titulo("Monitor de Processos")
	exe, err := os.Executable()
	if err != nil {
		pausa("Não consegui descobrir onde o programa está: " + err.Error())
		os.Exit(1)
	}
	raiz := filepath.Dir(exe)
	app := filepath.Join(raiz, "app")
	if _, err := os.Stat(filepath.Join(app, "app.py")); err != nil {
		pausa("Não encontrei a pasta \"app\" ao lado deste programa.\nExecute o instalador (Instalar-MonitorProcessos) de novo: ele repõe os arquivos sem mexer nos seus dados.")
		os.Exit(1)
	}
	py := inst.EncontrarPython(executa, pythonsConhecidos())
	if py == nil {
		pausa("O Python 3.9 ou superior não foi encontrado neste computador.\n\n1) Instale em https://www.python.org/downloads/\n2) Na primeira tela do instalador do Python, marque \"Add python.exe to PATH\"\n3) Abra o Monitor de Processos de novo.")
		os.Exit(1)
	}
	args := append(append([]string{}, py[1:]...), "-X", "utf8", "app.py")
	cmd := exec.Command(py[0], args...)
	cmd.Dir = app
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	cmd.Env = append(os.Environ(), "PYTHONUTF8=1", "PYTHONIOENCODING=utf-8")
	if err := cmd.Run(); err != nil {
		pausa("O programa terminou com erro: " + err.Error() + "\nSe a mensagem acima não ajudar, envie uma captura desta janela a quem faz a manutenção.")
		os.Exit(1)
	}
}
