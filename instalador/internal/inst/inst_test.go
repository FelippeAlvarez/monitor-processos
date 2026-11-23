package inst

import (
	"archive/zip"
	"bytes"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func zipDe(t *testing.T, arquivos map[string]string) []byte {
	var b bytes.Buffer
	w := zip.NewWriter(&b)
	for n, c := range arquivos {
		f, _ := w.Create(n)
		f.Write([]byte(c))
	}
	w.Close()
	return b.Bytes()
}

func TestExtractZip(t *testing.T) {
	d := t.TempDir()
	z := zipDe(t, map[string]string{
		"MonitorProcessos/app.py":             "print(1)",
		"MonitorProcessos/static/index.html":  "<html>",
		"MonitorProcessos/dados/processos.db": "NAO",
		"MonitorProcessos/local.json":         "NAO",
		"MonitorProcessos/__pycache__/x.pyc":  "NAO",
	})
	if err := ExtractZip(z, filepath.Join(d, "novo")); err != nil {
		t.Fatal(err)
	}
	for _, ok := range []string{"app.py", "static/index.html"} {
		if _, err := os.Stat(filepath.Join(d, "novo", filepath.FromSlash(ok))); err != nil {
			t.Errorf("faltou %s", ok)
		}
	}
	for _, no := range []string{"dados", "local.json", "__pycache__"} {
		if _, err := os.Stat(filepath.Join(d, "novo", no)); err == nil {
			t.Errorf("não devia extrair %s", no)
		}
	}
}

func TestExtractZipSlip(t *testing.T) {
	d := t.TempDir()
	z := zipDe(t, map[string]string{"../fora.txt": "x"})
	if err := ExtractZip(z, filepath.Join(d, "novo")); err == nil {
		t.Fatal("devia recusar caminho que sai da pasta")
	}
	if _, err := os.Stat(filepath.Join(d, "fora.txt")); err == nil {
		t.Fatal("escreveu fora da pasta")
	}
}

func escreve(t *testing.T, p, c string) {
	os.MkdirAll(filepath.Dir(p), 0o755)
	if err := os.WriteFile(p, []byte(c), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestSwapAppPreservaDadosEGuardaAnterior(t *testing.T) {
	r := t.TempDir()
	escreve(t, filepath.Join(r, "app", "app.py"), "v1")
	escreve(t, filepath.Join(r, "dados", "processos.db"), "BANCO")
	escreve(t, filepath.Join(r, "app.novo", "app.py"), "v2")
	if err := SwapApp(r, filepath.Join(r, "app.novo")); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(filepath.Join(r, "app", "app.py"))
	ant, _ := os.ReadFile(filepath.Join(r, "app.anterior", "app.py"))
	db, _ := os.ReadFile(filepath.Join(r, "dados", "processos.db"))
	if string(got) != "v2" || string(ant) != "v1" || string(db) != "BANCO" {
		t.Fatalf("troca errada: app=%s anterior=%s db=%s", got, ant, db)
	}
	// segunda atualização: a anterior vira v2
	escreve(t, filepath.Join(r, "app.novo", "app.py"), "v3")
	if err := SwapApp(r, filepath.Join(r, "app.novo")); err != nil {
		t.Fatal(err)
	}
	ant, _ = os.ReadFile(filepath.Join(r, "app.anterior", "app.py"))
	if string(ant) != "v2" {
		t.Fatalf("anterior=%s", ant)
	}
}

func TestSwapAppPacoteInvalidoNaoMexe(t *testing.T) {
	r := t.TempDir()
	escreve(t, filepath.Join(r, "app", "app.py"), "v1")
	os.MkdirAll(filepath.Join(r, "app.novo"), 0o755) // sem app.py
	if err := SwapApp(r, filepath.Join(r, "app.novo")); err == nil {
		t.Fatal("devia falhar")
	}
	got, _ := os.ReadFile(filepath.Join(r, "app", "app.py"))
	if string(got) != "v1" {
		t.Fatal("versão atual foi alterada")
	}
}

func TestPythonOK(t *testing.T) {
	casos := map[string]bool{"Python 3.12.1": true, "Python 3.9.0": true, "Python 3.8.10": false, "Python 2.7.18": false, "": false,
		"Python was not found; run without arguments to install": false}
	for s, want := range casos {
		if PythonOK(s) != want {
			t.Errorf("%q: esperado %v", s, want)
		}
	}
}

func TestEncontrarPython(t *testing.T) {
	exec := func(args []string) (string, error) {
		if args[0] == "python3" {
			return "Python 3.11.4", nil
		}
		if args[0] == "python" {
			return "Python was not found", nil // stub da Store
		}
		return "", os.ErrNotExist
	}
	got := EncontrarPython(exec, nil)
	if len(got) != 1 || got[0] != "python3" {
		t.Fatalf("got %v", got)
	}
	if EncontrarPython(func([]string) (string, error) { return "", os.ErrNotExist }, nil) != nil {
		t.Fatal("devia ser nil")
	}
}

func TestEscreverSeDiferente(t *testing.T) {
	p := filepath.Join(t.TempDir(), "a.exe")
	if ok, err := EscreverSeDiferente(p, []byte("x"), 0o755); !ok || err != nil {
		t.Fatal(ok, err)
	}
	if ok, _ := EscreverSeDiferente(p, []byte("x"), 0o755); ok {
		t.Fatal("não devia regravar igual")
	}
	if ok, _ := EscreverSeDiferente(p, []byte("y"), 0o755); !ok {
		t.Fatal("devia regravar diferente")
	}
}

func TestCopiarPastaNaoSobrescreve(t *testing.T) {
	o, d := t.TempDir(), t.TempDir()
	escreve(t, filepath.Join(o, "processos.db"), "ORIGEM")
	escreve(t, filepath.Join(o, "sub", "x.txt"), "X")
	escreve(t, filepath.Join(d, "processos.db"), "DESTINO")
	if err := CopiarPasta(o, d); err != nil {
		t.Fatal(err)
	}
	a, _ := os.ReadFile(filepath.Join(d, "processos.db"))
	b, _ := os.ReadFile(filepath.Join(d, "sub", "x.txt"))
	if string(a) != "DESTINO" || string(b) != "X" {
		t.Fatalf("a=%s b=%s", a, b)
	}
	if _, err := os.Stat(filepath.Join(o, "processos.db")); err != nil {
		t.Fatal("origem foi alterada")
	}
}

func TestMelhorDados(t *testing.T) {
	d := t.TempDir()
	a, b := filepath.Join(d, "a.db"), filepath.Join(d, "b.db")
	escreve(t, a, "1")
	escreve(t, b, "2")
	os.Chtimes(a, time.Now(), time.Now().Add(-time.Hour))
	if MelhorDados([]string{a, b, filepath.Join(d, "nao.db")}) != b {
		t.Fatal("devia escolher o mais recente")
	}
	if MelhorDados(nil) != "" {
		t.Fatal("vazio")
	}
}
