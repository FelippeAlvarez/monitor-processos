MONITOR DE PROCESSOS — MANUAL DE USO (v0.5.1)

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

16. USAR PELO CELULAR (atualizado na v0.4.0)
- Dê dois cliques em iniciar-celular.bat (em vez de iniciar.bat). A janela mostra o endereço do tipo http://192.168.x.x:8765.
  Com o celular no MESMO Wi-Fi do computador, abra esse endereço e entre com seu usuário e senha (seção 17).
- O computador precisa estar ligado, com a janela aberta. Se o Windows perguntar sobre o Firewall, permita em "Redes privadas".
- Fora de casa: Tailscale (gratuito) no computador e no celular, ou um servidor na nuvem com HTTPS (seção 18).
- Em Wi-Fi público não use o modo rede sem HTTPS: a senha viajaria sem criptografia.

17. USUÁRIOS, LOGIN E PERFIS (novo na v0.4.0)
- Todo acesso exige usuário e senha. O código de acesso da v0.3.3 foi abolido.
- Primeiro acesso: a janela do programa mostra um "código de configuração". Abra o endereço, preencha o formulário de
  primeiro acesso (nome, usuário, senha) e esse código. Isso cria o administrador. O código vale só enquanto não houver usuários.
- Perfis: ADMINISTRADOR (gerencia usuários, filtros de pesquisa, configurações, backup e vê o registro de atividades; faz também
  tudo o que o usuário faz) e USUÁRIO (processos, publicações, consultas). O perfil CLIENTE está previsto para a v0.9.
- Aba "Equipe" (só administrador): criar usuários, mudar o perfil, desativar/reativar e redefinir a senha. Usuários não são
  apagados, só desativados, para preservar o histórico. Quem recebe senha provisória é obrigado a trocá-la no primeiro acesso.
- Senhas: mínimo de 10 caracteres, guardadas só como hash (scrypt). Após 5 senhas erradas seguidas a conta fica bloqueada por 15 minutos.
  A sessão dura até 14 dias sem uso; "Sair" encerra a sessão. Desativar um usuário ou redefinir sua senha o desconecta na hora.
- O sistema não deixa desativar nem rebaixar o último administrador, nem a própria conta.
- Registro de atividades (aba Equipe): logins (e tentativas falhas), alterações de usuários e configurações, processos e filtros
  incluídos/removidos, backups. As senhas nunca são registradas.
- E-mails de alerta: o campo "Destinatário" aceita vários endereços separados por vírgula. (O envio por responsável vem na v0.6.)

18. PASTA DE DADOS, DROPBOX, BACKUP E SERVIDOR (novo na v0.4.0)
- A pasta de dados (banco, configuração e log) fica por padrão na pasta "dados" ao lado do programa. Para usar outra pasta,
  inclusive uma pasta do Dropbox, crie ao lado de app.py o arquivo local.json (modelo em local.json.exemplo):
      {"pasta_dados": "C:\\Users\\SeuNome\\Dropbox\\Monitor"}
  (no JSON, cada barra invertida é escrita em dobro). Outra opção é definir a variável de ambiente MONITOR_DADOS.
- ATENÇÃO — REGRA DE OURO: só UM computador pode executar o programa sobre a mesma pasta de dados. O Dropbox sincroniza o
  arquivo inteiro e não sabe mesclar: dois computadores gravando no mesmo banco geram cópias "em conflito" e podem corromper os
  dados. O programa impede isso (arquivo servidor.lock) e se recusa a abrir se outro computador estiver usando a pasta.
  O desenho recomendado: UM computador/servidor roda o programa; os demais usam pelo navegador, com login.
- Mesmo com um só computador, o Dropbox pode enviar o processos.db enquanto o programa o usa; por isso o backup diário (arquivo
  novo e consistente) é a cópia confiável, e o arquivo vivo não deve ser aberto em outro lugar.
- Dropbox como destino de backup é seguro (cada backup é um arquivo novo). Aba Equipe > Backup: defina a pasta (ex.: uma pasta
  do Dropbox), a hora (padrão 02:00) e quantos backups guardar (padrão 30). O backup é uma cópia consistente do banco; não inclui
  config.json (que tem a senha do e-mail). Ele inclui os hashes das senhas dos usuários e os dados dos processos: trate a pasta
  de backup com o mesmo cuidado do banco. Para restaurar: feche o programa, copie o backup desejado sobre processos.db (na pasta de
  dados) e abra de novo.
- Servidor na internet (necessário para clientes e para acesso de qualquer lugar): defina MONITOR_REDE=1, MONITOR_PROXY=1 e
  MONITOR_ENDERECO=127.0.0.1, e coloque na frente um servidor web com HTTPS (por exemplo o Caddy, que obtém o certificado sozinho:
  "seudominio.com.br { reverse_proxy 127.0.0.1:8765 }"). Sem HTTPS, não exponha o programa à internet. Com MONITOR_PROXY=1 o
  programa confia nos cabeçalhos X-Forwarded-For/Proto do proxy; só ligue isso se o programa for acessível apenas por ele.

19. VARREDURA DIÁRIA EM BLOCOS (novo na v0.5.0)
- Para carteiras grandes (centenas ou milhares de processos), o programa não consulta tudo de uma vez: a cada N minutos ele pega um
  BLOCO de processos ainda não consultados HOJE e consulta o DataJud e o DJEN de cada um. Cada processo é consultado uma vez por dia.
  Os nunca consultados vão primeiro; depois, os de consulta mais antiga. No dia seguinte recomeça.
- Configurações > "Varredura diária em blocos": tamanho do bloco (padrão 100), minutos entre blocos (padrão 10) e HORA DE INÍCIO
  (padrão 02:00, de madrugada, quando o CNJ tem menos acessos). Não há horário final: a varredura segue, bloco após bloco, até
  consultar todos os processos do dia. Exemplo: 1.400 processos em blocos de 100 a cada 10 min = 14 blocos, cerca de 2 a 3 horas.
- Computador desligado ou em repouso às 02:00? A varredura do dia roda assim que o programa estiver aberto e o computador ligado
  (retoma de onde parou, sem repetir quem já foi consultado). Desative a suspensão automática do Windows se quiser a varredura de madrugada.
- Painel: barra "Varredura diária" mostra quantos já foram consultados hoje. Se a API reclamar de excesso de requisições (429), o
  programa pausa sozinho e o restante segue no bloco seguinte. Processos com erro voltam por nova tentativa automática.
- Os filtros de pesquisa (OAB, CPF/CNPJ, texto) continuam rodando a cada "intervalo" (Configurações; padrão 6 h) — é a forma mais
  rápida de saber de publicações novas, inclusive em processos ainda não cadastrados.
- O horário de início é o do relógio do computador/servidor onde o programa roda. Num servidor na nuvem, confira o fuso horário
  (muitos vêm em UTC, 3 horas à frente de Brasília) e ajuste o fuso do servidor para America/Sao_Paulo.
- Desligando a varredura em blocos, o programa volta ao modo clássico (consulta todos os processos a cada intervalo).
- Alertas: movimentação ou publicação nova aparecem no Painel/Publicações e, se o e-mail estiver ligado, são enviados por bloco.

20. ORGANIZAR A CARTEIRA (novo na v0.5.0)
- Importar de planilha (aba Adicionar): aceita .xlsx, .csv, .txt ou relatório exportado como página web. Procura números CNJ em qualquer
  célula; se houver cabeçalho com "Cliente", "Pasta" ou "Apelido", essas colunas acompanham cada processo. Mostra uma prévia (novos,
  repetidos, rejeitados) antes de cadastrar. Em lotes grandes, a primeira consulta de cada processo é feita pela varredura diária.
  Arquivo .xls antigo (Excel 97-2003) não é lido: salve como .xlsx ou copie e cole as células na caixa de texto da aba Adicionar.
- Descobrir processos pela OAB (aba Filtros): em um filtro de OAB, "Descobrir processos" busca no DJEN os processos que tiveram ao menos
  uma publicação nos últimos N meses (padrão 12). "Ver descobertos" lista número, tribunal, classe, última publicação e quantas
  publicações; processos já cadastrados ou arquivados são sinalizados, e você cadastra os novos de uma vez ou só os marcados.
  Só guarda número, tribunal, classe, órgão e datas (não guarda o texto). Se o resultado vier parcial, o programa avisa (a API limita
  a quantidade por consulta). Limite: só enxerga processos com publicação no DJEN em nome desse filtro; não é um inventário completo.
- Arquivar: o botão "Arquivar" tira o processo das consultas sem apagá-lo (e "Reativar" o devolve). "Arquivar parados…" arquiva de uma vez
  os processos sem NENHUMA movimentação nem publicação há mais de N meses (padrão 12), com prévia e confirmação. Processos nunca
  consultados ou sem datas conhecidas não são arquivados. Se um processo arquivado voltar a aparecer numa publicação de um filtro, você é avisado.
- Painel: busca por número, apelido, cliente ou pasta (sem diferenciar acentos) e filtro Ativos/Só com novidades/Com erro/Arquivados.
