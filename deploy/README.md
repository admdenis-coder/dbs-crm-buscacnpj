# Publicar o CRM com domínio e HTTPS

O GitHub recebe somente o código. O banco comercial, ZIPs da Receita, backups e senhas ficam fora do repositório e da imagem Docker. A aplicação é executada em uma única instância com disco persistente. Não usar hospedagem somente de arquivos estáticos para este projeto.

## Estrutura preparada

- Docker Compose: serviço Python e gateway Caddy, com reinício após falha/reinício do servidor.
- Apenas as portas 80 e 443 são publicadas. A porta 8765 não deve ser liberada no firewall nem publicada no Docker.
- Caddy exige usuário e senha em todas as páginas e APIs e emite/renova o certificado HTTPS.
- O serviço Python exige um segredo interno inserido pelo gateway e verifica domínio, origem e proteção das alterações.
- Volumes permanentes guardam dados do CRM, arquivos e certificados. Não executar `docker compose down -v` em produção.
- GitHub Actions executa os testes e valida a construção da imagem a cada alteração.

O acesso inicial usa uma credencial administrativa no gateway. Ainda não há contas individuais, níveis de permissão ou auditoria por vendedor. O servidor Python é o piloto existente, protegido pelo gateway; testes de capacidade e autenticação individual devem preceder ampliação para muitas pessoas. Não há implantação pública concluída apenas por estes arquivos existirem.

## Configurar no servidor Linux

1. Instalar Docker Engine e Docker Compose, clonar o repositório privado e entrar na pasta.
2. Copiar `.env.example` para `.env`, restringir com `chmod 600 .env` e preencher domínio, usuário, hash da senha e segredo de gateway. Não usar os exemplos literalmente.
3. Gerar o hash de senha interativamente (a senha não aparece no histórico):

   `docker run --rm -it caddy:2-alpine caddy hash-password`

   Colocar o hash em `CRM_PASSWORD_HASH`, entre aspas simples para preservar os caracteres `$`.
4. Gerar `DBS_GATEWAY_TOKEN` aleatório com pelo menos 32 caracteres, usando o comando indicado em `.env.example`. Não compartilhar a senha nem o token no chat ou no GitHub.
5. Criar o registro DNS A do subdomínio escolhido apontando ao IPv4 do servidor. Só criar AAAA se houver IPv6 funcional. Liberar portas TCP 80/443. A emissão de HTTPS depende desse DNS.
6. Validar e iniciar:

   ```sh
   docker compose config --quiet
   docker compose run --rm --no-deps gateway caddy validate --config /etc/caddy/Caddyfile
   docker compose up -d --build
   docker compose ps
   ```

7. No endereço HTTPS, verificar: senha exigida; senha errada negada; credencial correta abre a plataforma; dados/exports negados sem login; novas anotações persistem após reinício; domínio e certificado válidos.

Não imprimir `docker compose config` sem `--quiet`, pois a saída inclui os segredos resolvidos. Se a hospedagem escolhida não suportar Docker Compose com disco persistente, adaptar o deployment antes de publicar.

## Migrar os dados locais sem perder coletas

A transferência de dados é separada do Git. Esperar a coleta local concluir e encerrar o serviço local antes de copiar a pasta `data` inteira. Isso inclui `crm.sqlite3`, eventual WAL, demonstração, `registry-cache` e arquivos da Receita preservados. Transferir por SSH/SFTP para um local privado no servidor.

Com a cópia íntegra no servidor, em uma instalação nova:

```sh
docker compose create crm
docker compose cp /caminho/privado/data/. crm:/data/
docker compose run --rm --user root --no-deps crm chown -R 10001:10001 /data
docker compose up -d
```

Não sobrescrever um banco de produção já usado. Comparar contagem de cadastros, cidades, notas e arquivos antes de liberar a equipe. Manter o original local até validar a migração. A primeira inicialização retoma a rotina automática se ela já estiver habilitada nas configurações migradas.

## Backup e manutenção

Antes de atualizar, aguardar a coleta terminar, parar os serviços e fazer backup consistente do volume `crm_data` e da configuração privada `.env`. Guardar cópia fora do servidor e testar a restauração. Nunca enviar o backup ao GitHub. Após o backup, atualizar o código e executar `docker compose up -d --build`.

Ainda é necessário configurar backup automático no provedor escolhido e monitorar espaço livre. Cada publicação arquivada ocupa vários GB; a versão atual não apaga publicações antigas automaticamente.
