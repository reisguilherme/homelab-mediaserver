# Jellyfin na VM playback

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. A execução desta etapa foi autorizada pelo proprietário.

**Goal:** restaurar seletivamente o Jellyfin com mídia NFS somente leitura e catálogo preservado durante a consolidação parcial.

**Architecture:** Compose independente na playback, sem init, Arr ou proxies do monólito. Configuração e cache locais; `/data/media` mantém os caminhos anteriores e aponta para o export RO do core. A entrada recusa mount ausente ou divergente e supervisiona a identidade das montagens; agendas e realtime ficam desativados na configuração restaurada.

**Tech Stack:** Ubuntu 24.04, Docker Compose, Jellyfin fixado por digest, NFSv4.2 hard, shell POSIX e Python 3.12 para ferramentas offline.

**Spec:** [to-be](../architecture/homelab-2026/to-be.md), contrato de storage e [estado da plataforma](https://github.com/reisguilherme/homelab-platform/blob/refactor/docs/implementation-status.md).

## Global Constraints

- Backup e mídia não são alterados pela restauração; nenhuma formatação.
- Mídia somente leitura, administração privada, edge continua fechado.
- Nenhuma unit nova no host Proxmox; nenhuma retomada do Compose monolítico.
- Dados privados e evidências ficam fora do Git; não importar `/etc`, Docker runtime ou volumes órfãos.
- NFS hard pode bloquear I/O; não usar soft, umount forçado ou lazy para simular recuperação.

## Review Focus

- Diretório vazio local no lugar de NFS deve impedir o processo Jellyfin.
- Fonte, subtree, permissões ou identidade diferentes devem impedir a inicialização.
- Desmontagem no guest deve encerrar o processo, mesmo com bind antigo no container.
- Bibliotecas parcialmente presentes não autorizam scans ou exclusões no catálogo.
- Extração seletiva não pode escapar do staging nem aceitar arquivos divergentes do manifesto.

### Task 1: Compose e guard de playback

**Files:** `compose.playback.yaml`, `scripts/playback-entrypoint.sh`, `tests/unit/test_playback_entrypoint.py`, `tests/contract/test_playback_compose.py`.

**Interfaces:** recebe `PLAYBACK_NFS_SOURCE`, mountinfo do host em `/run/host-mountinfo`, mídia em `/data/media`; executa o comando Jellyfin somente após validar ambos os mounts. O modo `--check` só valida, sem iniciar processos.

- [x] Escrever e executar testes negativos para mount ausente, fonte errada, subtree, RW, troca de device e saída do filho; observar RED.
- [x] Implementar validação e supervisão POSIX, Compose independente com binds explícitos e proteção de recursos; observar GREEN em Linux.
- [x] Conferir o Compose renderizado e registrar commit.

### Task 2: Restauração seletiva e contenção

**Files:** `scripts/prepare-playback-config.py`, `tests/unit/test_playback_config.py`; helpers operacionais privados e runbook `docs/runbooks/playback-nfs.md`.

**Interfaces:** configuração restaurada inativa, manifesto e arquivo de checkpoint; nenhuma escrita no backup. Preparador offline altera apenas a cópia de trabalho e produz resumo sem credenciais.

- [x] Testar XML/JSON inválidos, symlinks, realtime e agendas; observar RED antes da implementação.
- [x] Identificar versão, mounts e arquivos selecionados; verificar hash, tipos e links no staging novo.
- [x] Verificar SQLite, copiar para diretórios finais vazios e aplicar contenção offline.
- [x] Instalar Docker na playback e conferir imagem/versão, binds RO e ausência de dependências do monólito.

### Task 3: Aceite e publicação

**Files:** runbook anterior, status dos dois repositórios e contrato de playback da plataforma.

- [x] Iniciar em rede privada; verificar readiness autenticada, tarefas, bibliotecas, contagens e logs.
- [x] Testar recusa de startup sem mount usando fixture isolada; verificar comportamento real de NFS e reinício sem apagar acervo.
- [x] Verificar leitura de mídia e limites de reprodução; separar evidência de HTTP de teste real de vídeo.
- [x] Revisão independente e checks relevantes antes da publicação.

O fechamento exige publicação não forçada em `refactor` e confirmação do hash
remoto. Os recibos da execução registram esses hashes; o catálogo da plataforma
fixa o commit publicado do consumidor, sem referências circulares.

## Decisões operacionais

Recuperação física automática de USB, GPU, publicação externa e automação privada continuam etapas distintas. A supervisão de mount não certifica saúde física nem elimina processos bloqueados por NFS hard.
