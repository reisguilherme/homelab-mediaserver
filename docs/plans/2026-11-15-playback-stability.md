# Estabilidade da playback e rotação de acesso

> **For agentic workers:** Use superpowers:executing-plans ou subagent-driven-development. O proprietário autorizou continuar a implementação, indisponibilidade e publicação em `refactor`.

**Goal:** substituir o transcode temporário em RAM por SSD limitado, avançar a contenção de falhas do hub e redefinir as senhas Jellyfin conforme solicitado.

**Architecture:** Jellyfin continua sozinho na playback, mídia NFS RO, sem scans e sem automação. A plataforma entrega um filesystem local dedicado para transcode e coordena incidentes físicos; o consumidor valida suas montagens. A rotação é uma operação privada via API nativa.

**Tech Stack:** Proxmox, Ubuntu 24.04, ext4, NFSv4.2 hard, Docker Compose, shell POSIX e Python 3.12.

**Spec:** continuidade do [aceite de playback](../runbooks/playback-nfs.md) e do [desenho da plataforma](https://github.com/reisguilherme/homelab-platform/blob/refactor/docs/architecture/to-be.md). Abyss foi adiado pelo proprietário; nenhum tema é instalado nesta etapa.

**Estado:** disco de transcode aplicado e teste curto por CPU aceito; 18 senhas
redefinidas com login/logout conferidos. A lease obrigatória está implantada;
expiração sintética, HOLD e preservação de parada manual foram aceitos. Reboot
com SSD e lease passou, incluindo recusa do boot anterior e readmissão explícita;
não há promessa de retorno desassistido.

## Decisões e limites

- Transcode em RAM de 512 MiB foi provisório. Um disco virtual novo de 32 GiB no SSD oferece um limite próprio sem consumir a RAM da aplicação nem disputar o filesystem de seu banco. O espaço útil é menor que a capacidade nominal por causa dos metadados ext4. A alocação não certifica dez reproduções.
- O disco é exclusivo da playback, montado por UUID em `/srv/transcode`; a subpasta `data/` é gravável por UID 1000. A raiz e o marcador de identidade pertencem a root. O container recebe somente a pasta de dados e o marcador RO.
- O proprietário autorizou especificamente o novo disco virtual de 32 GiB em `local-lvm`, conectado como `scsi1`; ext4 e mount foram aplicados. Nenhum SSD físico ou disco do acervo foi formatado.
- As 18 senhas receberam exatamente dez caracteres aleatórios, distintos por usuário. A relação fica somente no Windows privado. Nenhuma senha, nome de conta ou caminho privado será publicado ou enviado a terceiros.
- A troca pela API nativa foi conferida com login e logout das 18 contas, sem relaxar políticas. Três contadores de login inválido voltaram naturalmente a zero; isso não comprova revogação de todas as sessões históricas.
- Não reativar Arr, scans, entrada pública, RTX ou recuperação antiga. Nunca remover mídia ou sobrescrever checkpoint. Interrupção física do HD exige coordenação com o proprietário; testes simulados não equivalem à prova física.

## Task 1 — Transcode SSD

**Files:** `compose.playback.yaml`, `scripts/playback-entrypoint.sh`, testes de guard/Compose, `platform.playback.contract.json`; fstab de exemplo e drop-in Docker no repositório da plataforma.

**Interfaces:** `/srv/transcode/data` → `/transcode`; marcador root em `.homelab-volume-id` → `/run/transcode-volume-id` RO; `PLAYBACK_TRANSCODE_ID` fixa o UUID esperado. Mountinfo prova origem da subárvore, dispositivo, ext4 e modo RW; o guard recusa fallback e mudança de identidade.

- [x] Aplicar somente o disco virtual autorizado, formatar ext4 e montar por UUID com marcador de identidade.
- [x] Conferir guard em execução: identidade correta, mount ausente, UUID divergente, escrita UID 1000 e marcador RO.
- [x] Manter mídia RO e limite de 3 GiB; transcode `libx264` de 10,01 s/720p concluído em 1,149 s, com 281.888 bytes e `UserData` estável; removida apenas a saída própria.
- [x] Fechar revisão e checks do código e confirmar retorno após reboot com o novo SSD; não usar o aceite histórico do tmpfs como prova.

## Task 2 — Senhas

**Files:** helpers e recibos operacionais exclusivamente privados; resumo sem credenciais no runbook.

**Interfaces:** `GET /Users`, `POST /Users/Password?userId=...` com `NewPw` e `ResetPassword=false`; autenticação administrativa pela API key existente. Login teste usa a senha nova; token resultante fica somente em memória e é encerrado.

- [x] Manter a relação de 18 senhas distintas de dez caracteres somente no Windows privado, fora do Git.
- [x] Redefinir pela API nativa e conferir os 18 logins e logouts.
- [x] Conferir políticas, com apenas três contadores `InvalidLoginAttemptCount` zerados pelo login correto, e hashes estáveis de 807 `UserData`, 432 `Permissions` e 234 `Preferences`.

Depois dessa comparação, `UserData` chegou a 813 registros: os 807 originais
permaneceram idênticos, com seis adições observadas durante disponibilidade da
aplicação. Adições preservadas, sem rollback; nova conferência após reboot passou.

## Task 3 — Storage e reprodução

**Files:** implementação e fixtures da coordenação no repositório da plataforma; adaptações de contenção no consumidor conforme a interface definida no plano da plataforma.

**Contrato implantado:** lease de até 45 segundos, vinculada ao boot da playback.
Falha mantém `HOLD`; `resume` explícito exige prova nova. O coordenador não inicia
containers, mas a autorização renovada pode permitir retorno pela política
`unless-stopped`; parada manual via Docker foi testada e permaneceu respeitada.
O supervisor usa cron com watch de 58 s e deadline de probe de 15 s. O HOLD
indevido na passagem da janela curta ao cron foi diagnosticado e corrigido;
disputa de lock retorna 75. Reboot do Proxmox exige auditoria do estado.

- [x] Registrar limites de I/O preso em NFS hard e separar contenção de recuperação física.
- [x] Implantar lease obrigatória e contenção, preservando pausa manual; presença do mount não substitui autorização válida.
- [x] Ensaiar expiração sintética (TERM no filho em 11,502 s), HOLD real (HTTP indisponível e processo Jellyfin ausente) e parada manual preservada após READY; start explícito retornou saudável. Sem falha física do HD.
- [x] Ensaiar reboot coordenado com SSD/lease, recusa da lease do boot anterior e readmissão explícita; conferir estado/histórico novamente.
- [x] Inspecionar somente leitura: iGPU Intel encontrada em grupo IOMMU isolado; nenhum passthrough ou teste de GPU realizado. RTX permanece fora do escopo.
- [ ] Revisão independente, checks relevantes, runbooks atualizados e publicação não forçada dos dois repositórios em `refactor`.

## Review Focus

- Ausência do novo SSD não pode deixar o transcode cair no disco do banco.
- Marker isolado não substitui coincidência de mountinfo e dispositivo.
- Limite de filesystem não elimina a necessidade de monitorar o thin pool.
- Erro de rotação não pode deixar senha desconhecida nem remover restrições de uma conta.
- NFS hard pode manter processos em D-state; timeout não comprova parada nem libera uma retomada insegura.
