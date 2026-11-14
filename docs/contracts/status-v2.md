# Status v2 e snapshots físicos

`GET /api/v1/status` e `/api/v1/telemetry` publicam `schema_version: 2`,
`generated_at`, `host`, `network`, `capacity` e `storage`. A expansão acrescenta
`pools`, sempre com SSD e HD USB separados. Sem cadastro e sem snapshots por pool,
o formato legado e o card único permanecem disponíveis.

Cada pool contém `pool_id` (`ssd` ou `hdd`), `label`, `filesystem_id`, `state`,
`reason`, `measured_at`, `total_bytes`, `used_bytes`, `free_bytes`,
`queue_remaining_bytes`, `active_queue_remaining_bytes`,
`stopped_queue_remaining_bytes`, `min_free_bytes`, `admissible_bytes` e `storage`.

`ready` permite números físicos atuais. `unavailable` e `stale` apresentam os
três números físicos como `null`; a idade pode continuar sendo exibida.
`storage` contém `state`, `measured_at`, `movies_bytes`, `series_bytes`,
`torrents_bytes` e `other_bytes`; números ficam nulos sem prova atual de identidade
e de medição. Os aliases `capacity` e `storage` no nível superior representam
somente SSD, nunca a soma dos discos; `capacity.state` mantém o legado `ok`.

O coletor escreve `host.json` com CPU/memória/rede independentes e a lista de
pools físicos. `capacity.json` contém os mesmos campos físicos por pool, além
dos aliases SSD. `measured_at` nos snapshots de pool é um número Unix em segundos.
Os consumidores também aceitam timestamps ISO para compatibilidade de leitura.
O registro padrão é `/run/homeserver/storage.json`; os coletores aceitam
`--storage-registry CAMINHO`. Registro instalado inválido nunca autoriza fallback
para o filesystem de um diretório comum.

Inspeção usa `StorageRegistry.inspect(pool_id, writable=False)`: o bind do coletor
pode ser somente leitura, mas o estado gravável da montagem real do host é
verificado. UUID, montagem real e dispositivo do bind constituem a prova; total
igual em bytes não identifica um filesystem. Cada inspeção que falha publica
apenas o pool afetado com estado indisponível e números nulos.

`storage-queue.json`, no mesmo diretório de `capacity.json`, é publicado pelo
controlador. Seu DTO é:

```json
{
  "measured_at": 1790900000.0,
  "pools": [
    {
      "pool_id": "ssd",
      "filesystem_id": "fixture-ssd",
      "pending_bytes": 20000,
      "active_pending_bytes": 15000,
      "stopped_pending_bytes": 5000,
      "min_free_bytes": 10000,
      "available_bytes": 30000
    }
  ]
}
```

O status combina esse DTO somente quando o pool físico está `ready`, a identidade
é a mesma, a amostra está dentro de `capacity_max_age_seconds`, os valores são
inteiros não negativos e `available_bytes <= max(0, total_bytes - pending_bytes)`.
Como as amostras de fila e espaço são independentes, o valor exibido de
disponibilidade é
`min(available_bytes, max(0, free_bytes - pending_bytes - min_free_bytes))`.
`min_free_bytes` é a margem de downloads por disco, independente da reserva do
filesystem, e deve ser inteiro não negativo. O default de produção é 10 GiB,
configurável por `HOMESERVER_STORAGE_MIN_FREE_GIB`. O snapshot antigo sem esse
campo é lido com margem zero para compatibilidade; margem inválida mantém a
fila válida mas deixa a disponibilidade nula. Os números físicos não mudam.
Assim, uma gravação entre coletas não apaga uma fila válida nem aumenta o espaço
apresentado. O controlador atualiza a fila em tarefa independente a cada cinco
segundos; uma falha de leitura não renova o timestamp anterior.
Sem prova válida ambos os campos de fila ficam nulos. `pending_bytes` inclui
compromissos restantes, inclusive fila pausada e permissões pendentes; o painel
apenas apresenta a contabilização do controlador, sem estimar ou admitir pedidos.
Os campos adicionais de fila são inteiros não negativos e sua soma deve ser
igual a `pending_bytes`. O status mantém `queue_remaining_bytes` como agregado
para compatibilidade; o painel mostra `active_queue_remaining_bytes` em
**Restante da fila**. `stopped_queue_remaining_bytes` permanece no DTO, sem campo
próprio nos cards dos discos.
Snapshots antigos ou com composição inválida mantêm somente o agregado válido;
os novos campos ficam nulos, sem inventar fila vazia. A disponibilidade continua
usando todos os compromissos até o descarte comprovado das fontes antigas.

Categorias usam blocos físicos e uma única tabela de `(st_dev, st_ino)` por pool,
na ordem filmes, séries e torrents; outros é o restante do usado físico. Symlinks
e outros dispositivos não entram na soma. A telemetria em modo de expansão lê
essas categorias do snapshot; não varre nem soma a visão mergerfs.
