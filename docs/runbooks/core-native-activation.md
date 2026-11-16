# Execução explícita de aplicativos pelo guard do core

Este incremento acrescenta `--run` ao consumidor de storage. O Compose e
`core-entrypoint.sh` continuam na preparação com inicialização negada; este
documento não comprova que as aplicações restauradas foram readmitidas.

O mesmo `core-storage-guard.py` mantém a verificação sem inicialização:

```sh
python3 core-storage-guard.py --check --filesystem-uuid UUID_ESPERADO --data-mode rw
```

A execução explícita seleciona um papel fixo:

```sh
python3 core-storage-guard.py --run sonarr --filesystem-uuid UUID_ESPERADO \
  --activation-sha256 SHA256_REAL_DO_RECIBO
```

Sem modo explícito, UUID, pin e todas as provas, a aplicação não inicia. Não há
comando livre, shell, override de caminho, modo fixture ou bypass de lease.
`--data-mode` pertence somente a `--check`; durante execução o papel determina
essa política. O ambiente do filho é uma lista técnica fixa, sem herdar variáveis
como `QBT_DAEMON`, `LD_PRELOAD` ou credenciais do processo supervisor.

| Papel | Comando fixo | Dados |
| --- | --- | --- |
| sonarr | `/app/sonarr/bin/Sonarr -nobrowser -data=/config` | `/data` RW |
| radarr | `/app/radarr/bin/Radarr -nobrowser -data=/config` | `/data` RW |
| prowlarr | `/app/prowlarr/bin/Prowlarr -nobrowser -data=/config` | sem `/data` |
| bazarr | `/lsiopy/bin/python3 /app/bazarr/bin/bazarr.py --no-update --no-tasks --no-signalr --config /config` | `/data` RW |
| qbittorrent | `/usr/bin/qbittorrent-nox --profile=/config --webui-port=8080 --confirm-legal-notice` | `/data` RW |
| seerr | `/usr/local/bin/node /app/dist/index.js` | sem `/data` |
| byparr | `/app/.venv/bin/python /app/main.py` | sem `/data` |

Arr usa seu diretório `bin`; Bazarr usa `/app/bazarr/bin`; qBit usa `/`;
Seerr/Byparr usam `/app`. Seerr recebe `NODE_ENV=production` e
`CONFIG_DIRECTORY=/app/config`. O conjunto completo de argv, cwd, ambiente e
política de dados tem um digest calculado por `native_plan_digest(native_plan(role))`.
Uma alteração desse conjunto exige um pin operacional correspondente.

Control API, worker, gateway e qbit-monitor não fazem parte desta lista. Seus
contratos de import, SQLite, identidade de storage e permissões precisam de
integração própria antes de iniciar.

## Recibo operacional fixado pelo operador

O arquivo é configuração operacional escrita pela operação já autorizada.
Não é uma nova aprovação humana, um certificado gerado por HTTP ou uma
declaração automática de que todas as automações estão contidas.

Origem no SSD da VM: `/srv/appdata/core-activation/ROLE`, diretório root 0755,
com `receipt.json` root 0444. Montar o diretório **RO**, com
`create_host_path: false`, em `/run/core-activation`. Ele deve vir do mesmo
filesystem do root da VM. Recusar symlinks/hardlinks, mounts empilhados ou filhos,
arquivos grandes, conteúdo alterado e caminhos de outro papel.

O JSON tem exatamente estes campos, sem chaves duplicadas ou extras:

| Campo | Contrato |
| --- | --- |
| `schema` | inteiro `1` |
| `scope` | `production-core-activation` |
| `role` | um dos sete papéis, igual ao argumento |
| `filesystem_uuid` | UUID ext4 esperado, igual ao argumento |
| `data_mode` | `rw` ou `none`, conforme o papel |
| `image_id` | ID literal `sha256:` e 64 hex minúsculos da imagem final |
| `native_command_sha256` | digest do plano nativo fixo deste código |
| `restore_receipt_sha256` | hash real do recibo de restauração/proveniência auditado |
| `configuration_receipt_sha256` | hash real da evidência da configuração operacional por versão |
| `native_process_group_receipt_sha256` | hash real do ensaio sob o supervisor e grupo de processos |

O argumento `--activation-sha256` fixa os bytes completos desse arquivo,
incluindo sua serialização. Os quatro campos de hash usam 64 hex minúsculos.
Não preencher hashes fictícios, `pending` ou hashes de evidências que não foram
produzidas. No caso sem estado persistente, a evidência deve descrever de fato
essa condição e a proveniência da imagem; não inventar uma restauração de banco.

O guard verifica o arquivo fixado e a identidade dos binds. Os hashes de outros
recibos são referências auditadas pela operação: o guard não abre essas evidências,
não examina bancos e não comprova uma configuração por conter um hash com formato
válido. O deploy também deve comparar a imagem efetiva com `image_id`; o guard
não tem Docker socket para fazer essa comparação dentro do container.

O recibo de `rehearse-core-native.py`, mesmo com PASS/HTTP ready, tem outro
contrato e é recusado aqui. Ele não prova o grupo de processos sob o guard,
restauração ou comportamento das automações. Isolamento de rede não substitui
a evidência da configuração nativa. Em especial, tarefas Arr podem recriar
intervalos no startup, e o pai Bazarr pode não propagar TERM ao filho.

## Lease, encerramento e parada manual

Permanecem obrigatórios os binds e a lease core-writer v2 documentados em
[core-migration.md](core-migration.md): guest boot, host mountinfo, marcador
ext4, diretório de admissão RO no SSD e `/data` exato quando o papel o exige.
Uma lease de playback não é aceita. A presença do mount não é saúde física.

Cada check lê novamente o recibo fixado e verifica storage. A identidade do
mount do recibo e seu hash entram no token acompanhado pelo supervisor.
O identificador opaco `physical_generation` também permanece fixo durante a
execução; uma sequência maior não autoriza trocar essa identidade física.
São feitas duas verificações antes do filho. Alteração do recibo, HOLD,
expiração, troca de identidade ou leitor bloqueado causam contenção pelo
supervisor já existente: TERM no grupo, depois KILL quando necessário, com
verificação de descendentes. O prazo usa `CLOCK_BOOTTIME`, incluindo suspensão.

SIGTERM/SIGINT do operador encerram o grupo e retornam sucesso quando a contenção
foi comprovada. O guard não reinicia, não remove pausa manual, não escreve lease,
recibo ou configuração e não oferece `resume`. Uma nova invocação explícita
refaz as provas; a política externa de restart não deve apagar a parada manual.
Falha de admissão/contenção retorna 78; saída nativa preserva seu status.

Os testes locais usam subprocessos e arquivos temporários, com observações de
mount sintéticas. Não substituem os ensaios nas imagens e no filesystem da VM.
Antes de integrar uma aplicação restaurada, ainda são necessários seu runtime
Python comprovado, ensaio sob o grupo do guard, configuração por versão e
recibos reais. Este incremento não altera Compose, mounts ou aplicações.
