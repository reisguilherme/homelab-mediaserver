# Operação

Execute os comandos na raiz do checkout, onde ficam `compose.yaml` e o `.env`.
Use `sudo docker` se o usuário não tiver acesso ao daemon Docker.

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 control-worker
```

| Necessidade | Guia |
|---|---|
| Preparar outra máquina e acesso Tailscale | [Instalação](installation.md) |
| Alterar comportamento ou credenciais | [Configuração](configuration.md) |
| Acompanhar pedidos e arquivos | [Guia do operador](operator-guide.md) |
| Conferir serviços nativos | [Serviços](runbooks/service-setup.md) |
| Investigar Requested, peers ou importação | [Diagnóstico](troubleshooting.md) |
| Desenvolver e testar | [Desenvolvimento](development.md) |

Para atualizar, faça `git pull --ff-only` e `docker compose up -d --build`.
Alterações em parâmetros nativos também precisam do comando operator, descrito
no guia de configuração. Para parar os containers, use `docker compose stop`.

Estado e mídia ficam nos diretórios persistentes do host. A operação normal
não remove esses dados. Assistir a um filme/episódio não o exclui; use a exclusão
explícita pelo Jellyfin quando desejar liberar espaço.
