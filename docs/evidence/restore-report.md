# Histórico de ensaios de restauração

O gerenciador de backups próprio foi retirado em 28/09/2026 por decisão
posterior de simplificar o projeto. Este documento registra ensaios anteriores,
sem constituir um procedimento atual.

Em 22/09/2026 um snapshot real foi copiado para o desktop por SSH.
Restic check terminou sem erros; um arquivo de identidade de release foi
restaurado isoladamente e conferido. Esse ensaio parcial não comprovava
restauração completa da biblioteca.

Em 28/09/2026, após corrigir exclusões de logs/cache, backup real, check e
restore isolado de **2,9724 GB de estado**, sem mídia, passaram com Restic.
O repositório criptografado ocupou aproximadamente 186 MB.
A cópia independente dessa nova captura para o desktop não foi comprovada.

O [registro histórico de aceite](productization-acceptance.md) apresenta os
limites e o contexto. A stack atual usa [Compose](../installation.md);
dados e cópias existentes não são apagados pela retirada do gerenciador.
