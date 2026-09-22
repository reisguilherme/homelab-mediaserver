# HomeServer — guia do agente e do operador

## Comandos

- `make lint`: Ruff, sintaxe Bash e ShellCheck quando instalado.
- `make test-unit`: testes unitários sem rede.
- `make test-contract`: contratos HTTP/API com fixtures locais.
- `make test-integration`: SQLite, filesystem temporário e containers quando disponíveis.
- `make compose-check`: valida o Compose dev sem segredos de produção.
- `make smoke`: healthchecks locais e guarda de montagem em modo fixture.

Use Python 3.12 e `uv sync --frozen` no ambiente Linux/WSL2. O desktop Windows não substitui os testes de filesystem, GPU, rede, energia, Docker e reprodução no Legion.

## Separação de ambientes

- Desenvolvimento usa `.runtime/dev`, fixtures, portas loopback e credenciais locais.
- Produção usa `/srv/data`, `/srv/appdata`, `/srv/transcode` e `/srv/backup-staging` somente após a guarda de UUID.
- Nunca copie bancos, mídia, tokens ou inventário bruto do servidor para o Git.
- O pipeline de produção é manual (`workflow_dispatch`) e deve usar o mesmo `scripts/deploy.sh` validado localmente.

## Operações proibidas nesta versão

Não formatar ou particionar discos, ativar mergerfs, reativar a RTX, substituir `fstab`, reescrever o serviço Lenovo, abrir portas no roteador, publicar a API do qBittorrent na LAN, liberar downloads sem reserva/gateway ou apagar mídia automaticamente.

## Estado externo

Os scripts de auditoria e deploy registram `reported`, `verified`, `missing` e `unknown`. Não transformar uma informação fornecida pelo usuário em comprovação física. Validações SSH/Tailscale, Intel UHD, ESP32, SFTP e reprodução exigem o ambiente real.
