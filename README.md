# Plataforma CRM — DBS TELECOM

Piloto local para prospecção de empresas de Rio Verde–GO abertas nos últimos 180 dias.

## Abrir

Com Python 3.9 ou superior instalado, execute `./iniciar.command` (ou `python3 -m dbs_crm.server`) nesta pasta e abra http://127.0.0.1:8765. Não é necessário instalar dependências. O serviço é acessível somente neste computador; encerre com Ctrl+C.

## Funciona nesta versão

- Base SQLite persistente, separada da demonstração fictícia.
- Busca por nome/CNPJ, filtro de etapas e elegibilidade dinâmica por município, UF, situação e abertura.
- Responsável, retorno, cobertura, campanha, motivo de perda, contato validado, cliente atual e bloqueio de abordagem.
- Histórico de alterações e anotações; uma atualização da Receita não substitui esses campos.
- Segmentação por CNAE/atividade e exportação CSV do público elegível, com proteção contra fórmulas em células.
- Coletor da Receita com descoberta da competência completa, leitura em fluxo de ZIP/CSV, seleção do município pelo domínio oficial e junção com razão social.
- Publicação da carga em uma transação: falha de download/layout não produz uma importação parcial no CRM.
- Verificação manual, importação e agendamento diário enquanto o serviço estiver aberto. A coleta automática começa desativada para permitir validar o ambiente e o espaço antes da primeira carga nacional.
- Registros já existentes são atualizados mesmo quando deixam o recorte (baixa, mudança de município ou idade). Registros ausentes são sinalizados para revisão; o histórico é preservado.

## Fonte oficial

- Repositório: https://arquivos.receitafederal.gov.br/index.php/s/gn672Ad4CF8N6TK?dir=/Dados/Cadastros/CNPJ
- Layout: https://www.gov.br/receitafederal/dados/cnpj-metadados.pdf

A descoberta usa WebDAV do compartilhamento **público** disponibilizado pela própria Receita. O identificador público não é credencial de usuário. Nenhum fornecedor privado é usado. Alterações da estrutura do repositório ou layout podem exigir manutenção. O campo de abertura utilizado é `data de início de atividade` do layout oficial; ele não comprova inauguração física nem data de disponibilidade pública.

A primeira carga baixa os 22 arquivos nacionais e os mantém em `data/registry-cache/archives`, identificados pela publicação (competência/ETag/tamanho). O banco de consulta guarda todos os municípios e todas as datas dos estados configurados, incluindo Goiás. A expansão para outro estado lê os arquivos já salvos; não baixa novamente a mesma publicação. Reserve espaço para os ZIPs e o banco local. Publicações anteriores são preservadas; não há remoção automática nesta versão.

**Buscar na base local** consulta cidades e períodos no banco indexado, sem depender da internet quando existe uma base completa. **Atualizar base da Receita** verifica a publicação oficial e prepara uma nova base somente quando necessário. A rotina automática verifica atualizações diariamente enquanto o serviço está aberto. A atualização da fonte é separada da escolha de cidades e períodos.

Cada arquivo processado é confirmado em uma transação. Após interrupção, a preparação retoma as etapas pendentes; arquivos completos são reutilizados. Um download parcial ainda é reiniciado. A base só fica disponível depois de completar e validar a carga. Uma falha preserva a publicação anterior e os dados comerciais. Alterar o recorte atualiza apenas os campos cadastrais, mantendo responsáveis, etapas e anotações.

As cargas feitas pela versão antiga guardavam apenas o recorte e apagavam os ZIPs: é necessária uma preparação inicial da base persistente. Arquivos remanescentes da execução antiga podem ser preservados, mas os já excluídos precisam ser obtidos novamente. A base oficial tem defasagem; consultar localmente não atualiza a publicação da Receita.

## Dados e backup

`data/crm.sqlite3` contém dados reais; `data/demo.sqlite3` contém somente exemplos. Para backup simples, encerre o serviço e copie a pasta `data`. Não publique essa pasta em Git. Não há envio de mensagens, integração Sefaz, consulta automática de cobertura nem importação da carteira IXC nesta versão.

## Antes de colocar a equipe para usar

Esta é uma aplicação **local de validação**, sem autenticação multiusuário. Antes de hospedar: implementar login/perfis, TLS, backups automatizados, supervisão do coletor, limites de recursos e infraestrutura que permaneça ligada. Não exponha o servidor local diretamente à internet. A base real fica vazia até concluir uma importação oficial; a demonstração não representa dados da Receita.

## Testes

`python3 -m unittest discover -s tests -v`

Testes usam arquivos sintéticos do layout oficial. A validação em pequena escala não substitui a primeira execução integral da base nacional.

## Correção de leitura de arquivos (17/11/2026)

O leitor remove bytes NUL antes do processamento CSV, preservando linhas, campos entre aspas e acentuação. O histórico informa o total removido quando a importação termina. Erros de leitura incluem arquivo e linha; erros de execução incluem a etapa em que ocorreram. Falhas transitórias ao abrir a conexão com a Receita têm até três tentativas. Essa repetição não retoma um download interrompido no meio.

Validação: 19 testes passaram, incluindo arquivos ZIP com NUL, campos multilinha, manutenção de histórico e falhas transitórias de conexão. A tentativa real após esta correção não chegou à leitura dos arquivos: o acesso HTTPS à Receita excedeu o tempo de conexão. A carga nacional ainda precisa ser concluída para validar o conjunto real inteiro.

## Cidades, contatos e CEP (18/11/2026)

Em **Coleta da Receita**, escolha a UF, selecione o município na lista do IBGE, clique em **Adicionar cidade** e depois **Salvar configuração**. É possível configurar várias cidades e estados. Execute **Buscar na base local** após a alteração, ou aguarde a rotina automática. A configuração não pode ser alterada durante uma coleta em andamento. Retirar uma cidade não exclui oportunidades existentes.

As abas de vendas e marketing têm filtros por cidade/UF. A lista mostra telefone, e-mail, endereço, município e CEP. CEP é um campo de exibição, não filtro da coleta. A migração mantém dados e histórico existentes, adiciona o campo de CEP vazio e o preenche na próxima importação oficial. A exportação inclui endereço e CEP e respeita cidade e atividade selecionadas. Nomes de cidade são normalizados para comparação sem acentos; UF distingue municípios homônimos.

Foram executados 23 testes, incluindo múltiplas cidades, limites por UF, CEP com zero inicial e preservação de histórico ao remover uma cidade da coleta.
