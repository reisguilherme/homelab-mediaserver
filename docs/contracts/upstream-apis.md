# Contratos externos v1

Este documento registra o subconjunto permitido pelos adaptadores próprios. Os
endpoints são referências de contrato e só podem ser ativados depois de uma
prova contra as versões registradas em `upstream-versions.json`.

| Serviço | Operações permitidas | Regra de falha |
| --- | --- | --- |
| Seerr | `GET /api/v1/request` paginado | 401/403 bloqueiam; resposta sem identidade é incompatível |
| Sonarr/Radarr | `GET /api/v3/release`, `POST /api/v3/command` para grab/import | timeout após POST fica incerto; não repetir cegamente |
| qBittorrent | login, versão, preferências, categorias, lista/propriedades/arquivos e `POST /api/v2/torrents/add` | somente o gateway possui a credencial; rotas desconhecidas são negadas |
| Bazarr | busca explícita de legenda do objeto validado | ausência mantém `waiting_subtitles` |
| Jellyfin | `GET /Sessions` | snapshot degradado não vira zero |

Os adaptadores usam timeout, cabeçalhos específicos e validação da resposta.
Não existe proxy genérico para URL/path arbitrário. Respostas de exemplo ficam
em `tests/fixtures/upstream/` sem tokens, URLs privadas ou nomes da biblioteca.

Em 22/09/2026 o servidor executava Sonarr 4.0.15.2941, Radarr 6.4.4.10685 e
qBittorrent 5.1.2 (Web API 2.11.4). O código-fonte dessas versões confirma que
os Arr usam `/api/v2/auth/login`, `app/webapiVersion`, `app/version`,
`app/preferences`, `torrents/categories`, `torrents/info`, `torrents/properties`,
`torrents/files` e `torrents/add`. O gateway responde apenas a esse subconjunto
de leitura e ao `add` admitido. `delete`, `setCategory`, `createCategory`,
`setShareLimits` e `setForceStart` permanecem bloqueados. `topPrio` não é exposto
à sessão dos Arr; somente a rotina interna autenticada do gateway pode usá-lo
para ordenar filmes admitidos por seeds e preservar a precedência dos episódios
elegíveis, sempre com permissão confirmada e identidade conferida no qBittorrent.

O caminho de adição implementado aceita somente upload de `.torrent` v1. O
gateway reinterpreta os bytes, verifica infohash, digest completo, arquivos,
tamanho, categoria, destino e permit ligado a uma reserva existente antes de
encaminhar os mesmos bytes ao qBittorrent. Magnet e URL de torrent são negados
até que exista obtenção e inspeção prévia segura. A seleção automática de
releases e a importação validada dependem de reserva, inspeção do torrent,
verificação de capacidade e permissão confirmada pelo gateway.

Em produção, qBittorrent usa `transfer` + `egress_transfer`; Sonarr/Radarr e
Prowlarr usam `apps` + `egress`. O gateway é o único serviço ligado a `apps`
e `transfer`. A porta de administração do qBittorrent fica ligada somente ao
loopback do host para uso do operador, sem publicação na LAN.
