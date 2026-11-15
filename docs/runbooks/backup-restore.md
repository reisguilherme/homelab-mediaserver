# Histórico — backup do serviço retirado

O gerenciador Restic, agenda, transferência e restore próprios foram retirados
em 28/09/2026 para simplificar a instalação pessoal. Esses comandos e parâmetros
não fazem parte do projeto atual. Dados e cópias existentes não são apagados
por essa decisão.

A persistência atual fica em `/srv/appdata` e `/srv/data`; veja
[instalação](../installation.md) e [operação](../operator-guide.md).
As provas históricas de captura/restauração estão no
[registro de ensaio](../evidence/restore-report.md).
O [ensaio manual de checkpoint nativo](native-checkpoint-rehearsal.md) usa apenas
configurações novas e mídia sintética no Linux local. Ele não reinstala o
gerenciador retirado nem constitui o checkpoint operacional do servidor.
A [preparação do checkpoint operacional](operational-checkpoint-preparation.md)
registra o levantamento real, capacidade e procedimento manual proposto para
a janela da migração; captura e restauração reais ainda estão pendentes.
O procedimento anterior permanece no
[histórico Git](https://github.com/reisguilherme/homelab-mediaserver/blob/89c84f26e4426d2f025573ce3728c5fa320f6267/docs/runbooks/backup-restore.md).
