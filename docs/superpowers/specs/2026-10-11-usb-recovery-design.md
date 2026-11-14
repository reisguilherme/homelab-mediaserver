# Recuperação automática dos volumes USB

O proprietário pediu que os HDs de mídia de 1 TB e 4 TB sejam montados novamente
e que a fila existente do qBittorrent retome quando os dispositivos retornarem.
Essa autorização abrange somente os dois volumes já cadastrados, suas montagens
existentes e os consumidores da stack HomeServer. Não abrange formatação,
particionamento, substituição de fstab ou escolha/adição/exclusão de fontes.

## Execução

Usar o cron já ativo no host, a cada minuto, sem nova unit. Instalar código e
configuração com propriedade root fora do checkout. Um lock impede concorrência.
O estado privado fica no SSD, com gravação atômica e fsync. Um comando de
manutenção desabilita a recuperação e um comando de status mostra seu resumo.

## Identidade e retorno

Capturar durante a instalação os UUIDs do registry, serial da ponte, tamanhos
da partição e do disco, número da partição e fstab existente. Resolver o nome
/dev dinamicamente. Exigir ext4 RW, SSD correto, identidade única e guards.
Registrar a geração USB para detectar reconexão mesmo com major/minor reutilizado.

Uma ausência ou montagem/bind inválido produz evento durável antes de parar o
agendador e os consumidores de mídia anteriormente ativos. Não iniciar serviços
parados pelo operador. Não remover RECOVERY_MODE preexistente. Esperar os dois
volumes cadastrados, confirmar estabilidade e restaurar apenas montagens
necessárias usando as units já geradas do fstab, sem umount forçado ou fsck.
Restaurar a união mergerfs e verificar branches/opções/guards antes de iniciar.

## Torrents e containers

Guardar a fila nativa em memória/estado privado antes de quiescer. A prova
conservadora antes do startup cruza snapshot, permits confirmados, artefatos,
destinos exclusivos e capacidade física atual incluindo a reserva existente.
Depois do startup, validar os namespaces de todos os consumidores e a fila
nativa atual (hashes v1/híbridos, descritores, seleção, prioridades e capacidade).
Erros que se resolvam naturalmente não recebem POST de controle. Erros restantes
somente nas fontes confirmadas do evento podem ser parados para limpar o estado;
o agendador existente retoma as fontes pelos seus próprios guards e limite 50.
Fontes concluídas previamente ativas podem voltar a seed com prova íntegra;
fontes paradas fora da gestão do agendador permanecem paradas.

Não implementar nova admissão, novo gerenciador de fila, pausas manuais do lote,
mudanças de prioridades, recheck massivo, move, delete ou forceStart. A semântica
atual do agendador de 50 downloads do acervo é preservada.

## Falhas e observabilidade

Identidade divergente, filesystem RO, mount ocupado, falta de capacidade,
metadados divergentes, serviços substituídos ou APIs indisponíveis mantêm hold.
Reconciliar efeitos após crash por readback, nunca executar argv vindo de estado.
Limitar os comandos por timeout e repetir em próximo tick. Logs contêm somente
fases, contagens e códigos sanitizados. Ao recuperar, atualizar o índice Jellyfin
uma vez sem substituir nomes/capas ou usuários.

## Verificação

Fixtures Linux para renumeração, coldboot, dois volumes ausentes, UUID estranho,
RO, crash por fase, bind antigo, capacidade insuficiente, hash híbrido e pausa de
fontes fora do lote. Testar instalação idempotente sem units/fstab novos. No host,
verificar tick saudável sem efeitos e executar manutenção coordenada dos volumes
autorizados com serviços quiescidos, sem desconectar a energia ou usar dados reais
como fixture. Documentar separadamente provas reais e simulações.
