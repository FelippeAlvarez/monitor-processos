# Monitor de Processos

Programa local (Windows/Linux/macOS, Python 3.9+, apenas biblioteca padrão) que acompanha processos judiciais:

- **DataJud (CNJ)**: movimentações dos processos cadastrados (metadados, com defasagem).
- **DJEN / Comunica PJe (CNJ)**: publicações com o texto, por processo.
- **Filtros de pesquisa no DJEN**: OAB, CPF/CNPJ ou trecho de texto, para detectar publicações novas em processos ainda não cadastrados.
- Painel em `http://127.0.0.1:8765`, alertas por e-mail opcionais, dados somente na máquina/servidor do escritório.
- **Monitorar ou não (v0.5.2)**: marcação por processo (com motivo) para a consulta diária; OAB automática (entra/sai do monitoramento conforme a sua OAB apareça nas publicações do processo); lote parcial do DataJud tratado sem sobrecarregar a API.
- **Carteira grande (v0.5)**: varredura diária em blocos (cada processo 1x/dia), importação de planilhas, descoberta de processos pela OAB e arquivamento de processos parados.
- **Equipe (v0.4)**: login por usuário, perfis administrador e usuário, registro de atividades, backup diário (pasta local ou Dropbox) e pasta de dados configurável.

> **Apoio, não substituto.** Este programa não substitui intimações, a conferência da publicação oficial nem o controle de prazos.
> A API do DJEN não tem documentação oficial conferida; o formato foi deduzido de respostas reais.

## Uso (resumo)
1. Instale o Python 3.9+ (marque "Add python.exe to PATH" no Windows).
2. Execute `iniciar.bat` (ou `python app.py`). No primeiro acesso, crie o administrador com o código mostrado na janela (manual, seção 17).
3. Para usar pelo celular na mesma rede, execute `iniciar-celular.bat` (seção 16 do manual); para servidor, Dropbox e backup, veja a seção 18.
4. Veja o manual completo em `MANUAL.md` (também na aba **Sobre** do painel).

## Testes
```
python -m unittest discover -s tests
```
Os testes usam APIs simuladas; não acessam o CNJ.

## Licença
Código **proprietário — todos os direitos reservados** (ver `LICENSE`). Publicado apenas para consulta; não há autorização de uso, cópia, modificação ou distribuição sem permissão escrita do titular.

## Documentos
`MANUAL.md` · `TERMOS_DE_USO.md` · `PRIVACIDADE.md` (minutas, a revisar antes de distribuir a terceiros)

© 2026 Felippe Alvarez de Sá
