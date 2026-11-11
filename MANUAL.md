MONITOR DE PROCESSOS — MANUAL DE USO (v0.3.1)

1. O QUE É
Programa local que consulta periodicamente a API Pública do DataJud (CNJ) e o DJEN (publicações, via Comunica PJe)
e mostra, num painel, as movimentações e publicações novas dos processos que você cadastrou. Pode avisar por e-mail.

2. O QUE NÃO É
Não substitui intimações, a conferência da publicação oficial, o Domicílio Judicial Eletrônico nem o controle de prazos.
O DataJud é uma base de metadados: tem defasagem em relação ao tribunal, não traz o teor das peças
e decisões, e não mostra processos sigilosos. Nunca conte prazo a partir de um alerta deste programa: confira sempre a publicação/intimação oficial.

3. REQUISITOS
Windows com Python 3.9 ou superior (python.org; marque "Add python.exe to PATH").
Não é preciso instalar mais nada.

4. COMO INICIAR
Dê dois cliques em iniciar.bat. O painel abre no navegador em http://127.0.0.1:8765.
Mantenha a janela preta aberta: ela é o programa. Para encerrar, feche-a.
Acesso só pelo próprio computador.

5. PASSO A PASSO
a) Aba "Adicionar": cole um ou vários números CNJ (NNNNNNN-DD.AAAA.J.TR.OOOO). O dígito verificador é conferido.
b) O programa consulta na hora. Na 1ª consulta, o histórico existente vira base de comparação:
   só movimentações posteriores geram "nova(s)".
c) No Painel, "Movimentos" mostra o histórico; "Marcar como visto" zera o contador de novidades.
d) Aba "Configurações": defina o intervalo das consultas automáticas e o e-mail de alerta.

6. TRIBUNAIS
O tribunal é identificado pelo próprio número (campos J.TR). Cobre, em tese, STJ, TST, TRFs, TRTs, TJs,
TREs e os TJMs. O STF não tem índice na API pública. Os nomes dos índices seguem o padrão da wiki do
DataJud e foram conferidos apenas para alguns tribunais: se aparecer "Índice não encontrado (404)",
informe o tribunal ao desenvolvedor. Processos do STJ costumam ter numeração própria; confira no primeiro teste.

7. CHAVE DA API
A chave é pública e fornecida pelo CNJ (datajud-wiki.cnj.jus.br/api-publica/acesso). O CNJ pode trocá-la
sem aviso. Se surgir erro 401/403, copie a chave nova e cole em Configurações.

8. E-MAIL
Informe servidor SMTP, usuário, senha de aplicativo e destinatário. No Gmail, crie uma "senha de app"
exclusiva (exige verificação em duas etapas) e nunca use a senha da conta. A senha fica em texto simples
em dados/config.json: proteja o computador e não compartilhe essa pasta.

9. ONDE FICAM OS DADOS
Pasta "dados" ao lado do programa: processos.db (processos e histórico) e config.json.
Para fazer backup, copie essa pasta. Para apagar tudo, feche o programa e apague a pasta.

10. PROBLEMAS COMUNS
- "Não encontrado": processo sigiloso, ainda não sincronizado com o CNJ, ou tribunal sem dados na API.
- "Sem conexão": verifique internet/firewall.
- "Porta em uso": já há outra janela do programa aberta.

11. FILA, LIMITE DE REQUISIÇÕES E TENTATIVAS AUTOMÁTICAS (novo na v0.1.1)
- Todo pedido de consulta entra numa fila; nada é descartado se já houver consulta em andamento.
- Processos do mesmo tribunal são consultados em lote (menos requisições ao CNJ).
- Erro 429 (limite de requisições): o programa espera e repete; se persistir, pausa ~15 minutos e o restante
  fica "Na fila", sem ser marcado como erro. A pausa aparece no painel.
- Processos com erro ou na fila são reenviados automaticamente, com espera progressiva (2, 5, 15, 30, 60 min).
- "Consultar agora" ignora a pausa e tenta de novo imediatamente.
- Aba Sobre > "Registro de diagnóstico (log)": mostra o que o programa fez (tribunal, tempo, erro HTTP).
  O arquivo fica em dados/monitor.log e não contém a chave da API nem senhas. Se algo falhar, envie-o ao desenvolvedor.
- Atualização de uma versão anterior: copie a pasta "dados" para dentro da nova pasta; o banco é migrado sozinho.

12. RESPOSTAS INCOMPLETAS DO CNJ E "NÃO ENCONTRADO" (novo na v0.1.2)
- O DataJud, quando sobrecarregado, pode devolver uma resposta INCOMPLETA sem avisar com erro. A partir da v0.1.2
  o programa detecta isso, repete a consulta e, se preciso, consulta o processo individualmente.
- "Não constam no DataJud" só aparece depois de confirmação em consulta individual. Causa mais comum:
  processo em segredo de justiça (não aparece na API pública); também pode não ter sido enviado pelo tribunal.
- Se um processo que já foi encontrado deixar de aparecer, o programa mantém os dados anteriores e mostra um aviso,
  em vez de trocar para "não encontrado".
- Movimentações com data muito anterior (mais de 7 dias) à mais recente já conhecida, que surjam depois, entram
  como "vistas" e não geram alerta (evita falsos alertas quando o histórico completo só aparece mais tarde).
- Cada processo mostra "registro do CNJ atualizado em ..." para você enxergar a defasagem da base.
- Se você vinha usando a v0.1.1 e há contadores de "novas" exagerados, clique em "Marcar tudo como visto".

13. PUBLICAÇÕES DO DJEN (novo na v0.2.0)
- Além do DataJud, o programa consulta, para cada processo, as publicações do Diário de Justiça Eletrônico Nacional
  (API Comunica PJe, do CNJ: comunicaapi.pje.jus.br, sem login). Diferente do DataJud, traz o TEXTO da publicação.
- Aba "Publicações": lista, da mais recente à mais antiga, com data, tipo, órgão, destinatários, advogados e texto.
  Pode filtrar só as não vistas e marcar todas como vistas. Publicações canceladas pelo tribunal aparecem marcadas e não alertam.
- Na 1ª consulta de cada processo, só publicações dos últimos 15 dias contam como novas; as mais antigas entram como vistas.
- O alerta por e-mail passa a trazer um trecho (até 700 caracteres) do texto da publicação. Atenção: isso põe conteúdo
  processual no seu e-mail; use uma caixa sua e protegida.
- Processos em segredo de justiça, ausentes do DataJud, podem ou não aparecer no DJEN; teste com os seus.
- O DJEN tem erro e limite próprios: se falhar, o DataJud continua funcionando e o DJEN é repetido sozinho (mesma espera
  progressiva). Em Configurações é possível desligá-lo.
- LIMITAÇÕES: não encontrei documentação oficial dessa API; o formato foi deduzido de respostas reais. A paginação e os limites
  de uso não foram confirmados. A publicação pode não existir no DJEN (intimação por outro meio) ou chegar com atraso.
  A data de disponibilização NÃO é o termo inicial do prazo: a contagem é responsabilidade do advogado.
- (v0.3.1) O DJEN envia o texto em HTML; o programa converte para texto simples. Processos em segredo de justiça aparecem
  no DJEN com o texto da publicação (partes ocultadas), o que o DataJud não mostra.

14. MONITORAMENTO POR OAB (novo na v0.3.0)
- Aba "OAB": cadastre quantas OABs quiser (número e UF; letra final opcional). Qualquer OAB pode ser cadastrada; é só um filtro de pesquisa.
- O programa consulta o DJEN por essa OAB (advogado destinatário) e avisa só de publicações NOVAS em processos que você ainda NÃO
  cadastrou. Processos já cadastrados continuam na aba Publicações (sem alerta em duplicidade).
- Ao cadastrar, valem só as publicações dos últimos 2 dias em diante: o histórico antigo não é buscado nem guardado.
  Cada consulta volta 3 dias para pegar publicações que chegam com atraso (sem repetir alertas).
- Em cada publicação, "Cadastrar processo" inclui o processo no monitor com um clique.
- A OAB é comparada como texto exato pela API do DJEN: se o tribunal grava o número com sufixo (-A, -O), cadastre também a variante.
- Se aparecer o aviso "não foi possível carregar todas as publicações", há mais itens do que a API entregou; confira no portal do DJEN.
- A consulta por OAB não é prova de que todas as publicações foram captadas. Não conte prazo por este aviso.
- Remover uma OAB apaga as publicações guardadas dela.


15. FILTROS POR CPF/CNPJ E POR TEXTO (novo na v0.3.1)
- Na aba "Filtros" (antes "OAB") escolha o tipo: OAB, CPF/CNPJ ou trecho de texto. Valem as mesmas regras da OAB:
  só publicações novas, só de processos ainda não cadastrados, botão "Cadastrar processo".
- CPF/CNPJ: o DJEN não tem filtro próprio; o programa procura o número NO TEXTO das publicações, nas duas formas
  (com pontuação e só dígitos). Os dígitos verificadores são conferidos ao cadastrar.
- LIMITAÇÃO IMPORTANTE: só aparecem publicações que citam o documento no texto. Despachos e atos ordinatórios curtos,
  em geral, trazem só o nome da parte. Por isso este filtro pode perder publicações; use como alerta adicional.
- Para uma parte com muitas ações (ex.: uma cooperativa grande) o volume pode ser alto: use com critério.
- Os textos podem conter CPF/CNPJ e dados de terceiros: tratamento local, sob sua responsabilidade (LGPD).

16. USAR PELO CELULAR (novo na v0.3.3)
- Dê dois cliques em iniciar-celular.bat (em vez de iniciar.bat). A janela mostra um endereço do tipo
  http://192.168.x.x:8765/?codigo=XXXX. Com o celular no MESMO Wi-Fi do computador, abra esse endereço uma vez no navegador do celular:
  o aparelho fica autorizado e depois basta abrir http://192.168.x.x:8765.
- O computador precisa estar ligado, com a janela aberta. Se o Windows perguntar sobre o Firewall, permita em "Redes privadas".
- O código de acesso fica em dados/config.json. Para trocá-lo, apague a linha "codigo_acesso" e reinicie.
- Fora de casa: instale o Tailscale (gratuito) no computador e no celular; use o endereço 100.x.x.x do computador, sem abrir nada para a internet.
- Em Wi-Fi público não use o modo rede: o código viaja sem criptografia (HTTP).
