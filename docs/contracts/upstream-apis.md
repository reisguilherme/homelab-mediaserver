# Contratos externos v1

Este documento registra o subconjunto permitido pelos adaptadores próprios. Os
endpoints são referências de contrato e só podem ser ativados depois de uma
prova contra as versões registradas em `upstream-versions.json`.

| Serviço | Operações permitidas | Regra de falha |
| --- | --- | --- |
| Seerr | `GET /api/v1/request` paginado | 401/403 bloqueiam; resposta sem identidade é incompatível |
| Sonarr/Radarr | `GET /api/v3/release`, `POST /api/v3/command` para grab/import | timeout após POST fica incerto; não repetir cegamente |
| qBittorrent | login, `POST /api/v2/torrents/add`, `GET /api/v2/torrents/info` | somente o gateway possui a credencial; rotas desconhecidas são negadas |
| Bazarr | busca explícita de legenda do objeto validado | ausência mantém `waiting_subtitles` |
| Jellyfin | `GET /Sessions` | snapshot degradado não vira zero |

Os adaptadores usam timeout, cabeçalhos específicos e validação da resposta.
Não existe proxy genérico para URL/path arbitrário. Respostas de exemplo ficam
em `tests/fixtures/upstream/` sem tokens, URLs privadas ou nomes da biblioteca.
