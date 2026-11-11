POLÍTICA DE PRIVACIDADE — MONITOR DE PROCESSOS
*** MINUTA — revisar antes de distribuir a terceiros (controlador, encarregado, contato) ***

1. Funcionamento local. O programa roda no computador do usuário. Não há servidor do desenvolvedor,
   conta de usuário, telemetria nem envio de dados ao desenvolvedor.

2. Dados tratados. (a) Números de processos cadastrados e rótulos opcionais (apelido, cliente, pasta);
   (b) metadados processuais públicos retornados pelo DataJud (classe, órgão julgador, movimentações) e publicações
   do DJEN retornadas pela API Comunica PJe (texto, órgão, destinatários e advogados nomeados, que podem conter dados pessoais);
   (c) configurações, incluindo a chave pública da API e, se o alerta por e-mail for ativado,
   servidor, usuário, senha de aplicativo e e-mails de remetente e destinatário.

3. Conexões externas. Apenas (i) à API Pública do DataJud (CNJ) e (ii) à API Comunica PJe
   (comunicaapi.pje.jus.br), enviando em ambas o número do processo consultado, e (iii) ao servidor SMTP configurado
   pelo usuário, para enviar os alertas, que podem conter trechos do texto de publicações. A consulta ao DJEN pode ser
   desligada em Configurações.

4. Armazenamento. Tudo fica na pasta "dados" ao lado do programa (processos.db e config.json).
   A senha de e-mail é gravada em texto simples nesse arquivo; o acesso ao computador deve ser protegido.

5. Terceiros. Os rótulos "cliente" e "pasta" podem identificar pessoas; o usuário é responsável pelo
   tratamento desses dados conforme a LGPD e o sigilo profissional.

5-A. Filtros de pesquisa (OAB, CPF/CNPJ, texto). Ao cadastrar filtros, o programa guarda localmente publicações do DJEN em que ela figura como
   advogado destinatário, inclusive de processos de terceiros (partes, advogados e texto), e pode enviá-las em trechos por
   e-mail. O usuário responde por essa finalidade (LGPD) e por proteger a máquina e a caixa de e-mail.

6. Exclusão. Remover um processo no painel apaga seu histórico. Para apagar tudo, feche o programa e
   exclua a pasta "dados".

7. Titular do programa: Felippe Alvarez de Sá. O tratamento de dados ocorre no computador do usuário, que é o responsável
   (controlador) pelos dados que cadastrar. Canal de contato: [PENDENTE DE DEFINIÇÃO]
