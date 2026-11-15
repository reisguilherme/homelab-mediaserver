# Arquitetura do homelab

A documentação geral passou para o repositório da plataforma:

[Abrir guia canônico em homelab-platform](https://github.com/reisguilherme/homelab-platform/blob/refactor/docs/architecture/README.md).

Este repositório mantém a aplicação de mídia. Consulte a
[integração com a plataforma](../../platform-integration.md) e o
[registro da preparação operacional](implementation-status.md).
A [execução do checkpoint operacional](../../runbooks/operational-checkpoint-execution.md)
registra a captura no HD, a segunda cópia verificada no Windows, a restauração
dos arquivos em Linux e o retorno dos 21 serviços originais. A validação
funcional posterior e a captura ampliada estão registradas na
[preparação para instalar Proxmox](../../runbooks/proxmox-preinstallation.md),
que distingue resultados comprovados de pendências. A migração do acervo continua aberta.
A [preparação anterior](../../runbooks/operational-checkpoint-preparation.md)
reúne escopo, capacidade e procedimento de restauração.
O [roteiro anterior ao corte](../../runbooks/media-precutover-review.md) registra
as condições de contenção e as pendências da migração.

A separação dos repositórios não alterou o servidor, as montagens nem o acervo.
O desenho anterior permanece no
[histórico desta versão](https://github.com/reisguilherme/homelab-mediaserver/blob/8627cd8ff76411831c95eb0072f88998c27469e0/docs/architecture/homelab-2026/README.md).
