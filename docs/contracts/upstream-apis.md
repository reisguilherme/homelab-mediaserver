# Contratos de integração

Os adaptadores próprios usam subconjuntos explícitos das APIs nativas.
Autenticação, identidade, versão/schema e leitura após escrita precisam ser
compatíveis; uma resposta desconhecida não é tratada como sucesso.

| Integração | Uso | Comportamento em falha |
|---|---|---|
| Seerr | Pedidos paginados, bibliotecas e sincronização de estado | 401/403 ou objeto sem identidade bloqueiam a integração |
| Sonarr/Radarr | Fontes, arquivos, importação e preferências nativas | Escrita com resultado incerto é reconciliada antes de repetir |
| Prowlarr | Definições/indexadores, aplicações e proxy Byparr | Definição ou fields desconhecidos retornam unsupported |
| qBittorrent via gateway | Leituras compatíveis Arr e adições/controle admitidos | Rotas desconhecidas, falta de autorização ou reserva inválida são recusadas |
| Bazarr/SubDL | Legendas do objeto/edição validado | Ausência mantém espera por legenda |
| Jellyfin | Bibliotecas, sessões e sincronização | Snapshot degradado não vira zero |
| Operator | Plan/apply/read-back das preferências gerenciadas | Preserva IDs/estado e informa aplicação parcial ou drift |

## Gateway e monitor qBit

Arr usa o cliente HomeServer Gateway. Admissão verifica identidade do torrent,
destino, categoria, artefato inspecionado, tamanho real, reserva e capacidade
recente. Controlar uma fonte também exige permissão ligada à aquisição e
respeita proteção persistente; não existe proxy genérico de URL/path.

A API nativa qBit não tem porta administrativa publicada no host.
O gateway e os clientes internos autorizados alcançam a rede de transferência.
O operator tem acesso pontual às APIs para configurar preferências.

O monitor público é um proxy separado, em loopback `18080`, com allowlist
de arquivos da UI, GETs de monitoramento e login/logout. Adicionar, excluir,
retomar, pausar ou alterar preferências pelo monitor retorna HTTP 403.
O acesso externo à LAN ocorre dentro da tailnet por
[Tailscale Serve](../installation.md#acesso-pelo-tailscale).

## Regras de mídia

Downloads de episódios podem ocorrer em paralelo dentro da janela por série
e do limite global; importação no Jellyfin segue temporada/episódio em ordem.
CDH Arr permanece desabilitado e hardlinks habilitados, de modo que o worker
coordene validação, legenda e importação sem duplicar os bytes do vídeo.

Fixtures de API ficam em `tests/fixtures/upstream/`, sem tokens, endereços
privados ou nomes da biblioteca. Os contratos do monitor também executam
Caddy real; versões novas precisam de verificação própria.

A fotografia do servidor em 22/09/2026 usava Sonarr 4.0.15.2941,
Radarr 6.4.4.10685 e qBittorrent 5.1.2 (Web API 2.11.4).
Esse registro histórico não substitui versões observadas em outra instalação.
