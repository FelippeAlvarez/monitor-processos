POLÍTICA DE PRIVACIDADE — MONITOR DE PROCESSOS
*** MINUTA — revisar antes de distribuir a terceiros (controlador, encarregado, contato) ***

1. Funcionamento. O programa roda no computador ou servidor escolhido pelo usuário/escritório. Não há servidor do desenvolvedor,
   telemetria nem envio de dados ao desenvolvedor. A partir da v0.4 há contas de usuário da própria equipe, criadas e geridas
   pelo administrador, sobre uma base de dados comum.

2. Dados tratados. (a) Números de processos cadastrados e rótulos opcionais (apelido, cliente, pasta);
   (b) metadados processuais públicos retornados pelo DataJud (classe, órgão julgador, movimentações) e publicações
   do DJEN retornadas pela API Comunica PJe (texto, órgão, destinatários e advogados nomeados, que podem conter dados pessoais);
   (c) configurações, incluindo a chave pública da API e, se o alerta por e-mail for ativado,
   servidor, usuário, senha de aplicativo e e-mails de remetente e destinatário;
   (d) (v0.4) contas dos usuários da equipe (nome, login, e-mail, perfil e hash da senha — nunca a senha), sessões de acesso e um
   registro de atividades (quem fez login, tentativas falhas e alterações relevantes, com data, hora e endereço IP).

3. Conexões externas. Apenas (i) à API Pública do DataJud (CNJ) e (ii) à API Comunica PJe
   (comunicaapi.pje.jus.br), enviando em ambas o número do processo consultado, e (iii) ao servidor SMTP configurado
   pelo usuário, para enviar os alertas, que podem conter trechos do texto de publicações. A consulta ao DJEN pode ser
   desligada em Configurações.

4. Armazenamento. Tudo fica na pasta de dados (por padrão, "dados" ao lado do programa; configurável, inclusive em pasta do Dropbox)
   (processos.db e config.json). A senha de e-mail é gravada em texto simples no config.json; o acesso ao computador/servidor deve ser
   protegido. Backups diários do banco (sem o config.json) são gravados na pasta escolhida pelo administrador e contêm os dados dos
   processos e os hashes das senhas: a pasta de backup deve ter o mesmo nível de proteção do banco. Se a pasta de dados ou de backup
   estiver em serviço de nuvem (ex.: Dropbox), os dados passam a ficar também nos servidores desse serviço, sob os termos dele.

5. Terceiros. Os rótulos "cliente" e "pasta" podem identificar pessoas; o usuário é responsável pelo
   tratamento desses dados conforme a LGPD e o sigilo profissional.

5-A. Filtros de pesquisa (OAB, CPF/CNPJ, texto). Ao cadastrar filtros, o programa guarda localmente publicações do DJEN em que ela figura como
   advogado destinatário, inclusive de processos de terceiros (partes, advogados e texto), e pode enviá-las em trechos por
   e-mail. O usuário responde por essa finalidade (LGPD) e por proteger a máquina e a caixa de e-mail.

6. Exclusão. Remover um processo no painel apaga seu histórico. Para apagar tudo, feche o programa e
   exclua a pasta "dados".

7. Titular do programa: Felippe Alvarez de Sá. O tratamento de dados ocorre no computador do usuário, que é o responsável
   (controlador) pelos dados que cadastrar. Canal de contato: [PENDENTE DE DEFINIÇÃO]
