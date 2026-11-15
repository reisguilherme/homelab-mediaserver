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
registra o levantamento real, capacidade e procedimento manual.
A [execução do checkpoint](operational-checkpoint-execution.md) começou após
autorização de indisponibilidade em 15/11/2026. A captura foi selada no HD e os
21 serviços originais retornaram. O arquivo de imagens já passou pela cópia
Windows, retorno ao Linux e importação isolada. A segunda cópia completa foi
verificada, e a restauração dos arquivos em ext4 passou pela comparação de
conteúdo e metadados, com 11 bancos SQLite íntegros. Nenhuma aplicação
restaurada foi iniciada; o ensaio funcional continua pendente, assim como a
reconstrução de três workers históricos e a integridade semântica de uma
persistência legada Mosquitto. Os detalhes e limites constam no registro.
O procedimento anterior permanece no
[histórico Git](https://github.com/reisguilherme/homelab-mediaserver/blob/89c84f26e4426d2f025573ce3728c5fa320f6267/docs/runbooks/backup-restore.md).
