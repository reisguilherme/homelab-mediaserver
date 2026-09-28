# Histórico de mudanças

## 2026-09-28 — Produtização

- Configuração tipada por `.env`, catálogo gerado e reconciliação das
  preferências nativas de qBittorrent, Arr, Prowlarr, Bazarr, Jellyfin e Seerr.
- Instalação fresh/adopt com autenticação inicial, persistência de IDs,
  caminhos configuráveis, CPU como padrão e Intel opcional.
- Busca por fonte melhor após cinco minutos de lentidão; avaliação mede
  velocidade e tempo restante, preservando a fonte atual e a sequência de séries.
- Pisos de tamanho do vídeo por minuto e resolução, editáveis no `.env`,
  compartilhados pelo worker e Arr; aquisição e failover não somam samples.
- Idioma de áudio preferido pelos metadados declarados, com contexto para
  `original`; dispensa de legenda só para original pt-BR comprovado, desativável.
- Política única de legenda pt-BR/inglês, release e edição compatível.
- Importação coordenada pelo worker, com Completed Download Handling Arr
  desabilitado e hardlinks habilitados; proteção persistente também no gateway.
- Retirada de firmware CYD, MQTT exclusivo e telemetria obsoleta; painel HTTP
  com métricas do host, capacidade e links dos serviços.
- Heartbeat de ciclos bem-sucedidos, supervisão com orçamento de reinícios,
  rotação de logs, guarda de montagem e manutenção coordenada.
- Backup Restic consistente, cópia independente e restauração isolada;
  release por digest com migração e rollback compatíveis com o schema.
- Guias de instalação, operação, arquitetura e diagnóstico; CI com scanners,
  build e testes de configuração, instalação e recuperação.

O registro de aceite distingue implementação, fixtures e verificações reais.
Reprodução em TV, Intel e instalação numa VM nova têm evidências separadas.
