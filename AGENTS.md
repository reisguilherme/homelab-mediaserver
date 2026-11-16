# HomeServer — guia do agente

## Migração para Proxmox

A etapa atual de playback usa somente `compose.playback.yaml`, conforme o
[runbook NFS](docs/runbooks/playback-nfs.md). Nesse destino, o proprietário
determinou mídia somente leitura; a regra histórica de Jellyfin RW abaixo
descreve o monólito anterior. Não iniciar o Compose monolítico nas novas VMs.
Preservar os gatilhos de scan suspensos enquanto o acervo estiver parcial.
Storage físico/exports e coordenação USB pertencem à plataforma; o consumidor
verifica mounts e contém sua aplicação. Não interpretar mount presente como
saúde física ou recuperação automática. Backup, cópias de referência e estado
privado ficam fora do Git.

Projeto pessoal executado pelo `compose.yaml` da raiz. Parâmetros de comportamento
e credenciais ficam no `.env` privado; defaults técnicos ficam no código/Compose.

## Comandos

- `docker compose up -d --build`: construir e iniciar a stack.
- `docker compose ps` e `docker compose logs --tail=100`: acompanhar serviços.
- `make lint`: Ruff, compilação, sintaxe Bash e ShellCheck quando instalado.
- `make test-unit`, `make test-contract`, `make test-integration`: checks locais.
- `make compose-check` e `make smoke`: Compose e verificações locais.

Use Python 3.12 e `uv sync --frozen` em Linux/WSL2 para desenvolvimento.
Windows não substitui testes de filesystem, GPU, rede e reprodução no servidor.

## Configuração e dados

- `.env` guarda credenciais diretamente, sem arquivos de segredo `*_FILE`.
- Layout padrão: `/srv/appdata`, `/srv/data`, `/srv/transcode` e `/srv/appdata/control` (runtime `/run/homeserver` nos containers).
- Fixtures usam diretórios temporários; nunca apontar testes para dados reais.
- Nunca colocar `.env`, bancos, tokens, mídia ou inventário bruto no Git.

## Limites operacionais

Não formatar/particionar discos nem alterar montagens sem autorização explícita
para os dispositivos e o escopo da operação. A expansão mergerfs opt-in está
documentada em `docs/storage.md`; o instalador só verifica mounts, prepara pastas
do projeto e testa fixtures, sem formatar, montar ou editar `fstab`.
Não ativar RTX, substituir `fstab`, reescrever o serviço Lenovo, abrir portas
no roteador ou publicar diretamente a porta administrativa nativa qBit.
O proprietário autorizou controle completo nos painéis autenticados: o proxy
administrativo qBit em loopback/Tailscale permite ações manuais. Downloads
automatizados continuam sujeitos ao gateway e à capacidade real. Não apagar
mídia automaticamente. Jellyfin tem escrita para edições e legendas; exclusões
de filmes, episódios e temporadas continuam coordenadas pelo controlador.

O projeto atual não tem CI/CD, releases próprias, gerenciador de backups ou
units do host. Não reintroduzir essas camadas. CYD/MQTT exclusivo foram retirados.
Documentos antigos de produtização são históricos; use README e guias atuais.
Informações declaradas não comprovam testes físicos, Tailscale ou reprodução.
