# HomeServer — primeiro deploy de teste

Data: 22/09/2026. Release de aplicação: `91edaf9169fea4c8a72976f240fed221de17f4b6`. O registro operacional detalhado permanece apenas no ambiente local do operador.

O primeiro deploy de teste iniciou os 12 serviços previstos no Compose. As imagens próprias foram construídas no servidor e passaram na importação; os containers reiniciaram com a unidade de serviço. O healthcheck do Jellyfin passou. As interfaces iniciais dos serviços de mídia responderam, e a conexão MQTT com TLS e ACL foi ensaiada. Um arquivo temporário confirmou que a importação por hardlink é viável nos volumes usados; o arquivo foi removido.

Este resultado ainda não comprova operação completa. A API do controlador responde em liveness, mas a readiness permanece 503 porque a capacidade real e o ciclo de aquisição não estão integrados. A telemetria responde em liveness, mas o endpoint de dados permanece 503 porque falta produzir o snapshot agregado. O gateway de download continua sem adapter de produção configurado. Download automático, exclusão de mídia e timers de backup ficaram desabilitados. Não houve ensaio de reprodução, transcodificação, backup externo, restore ou soak test.

O primeiro deploy exigiu um override local de Compose para rede de saída, volumes persistentes, identidades e binds. A configuração versionada ainda contém um tag de Radarr indisponível e terminações CRLF inadequadas para `source` em Bash. A auditoria do host revelou classificações imprecisas para targets mascarados e checagens agregadas de serviços, além de saída JSON inválida; esses defeitos precisam de correção antes de usar o auditor como evidência de aceite.

Próximos marcos: configurar Jellyfin e Seerr sem ativar buscas/downloads; fechar as falhas prioritárias de autorização, capacidade, telemetria e recuperação; corrigir o Compose e o versionamento; provar backup/restore e o fluxo de aquisição com conteúdo de teste autorizado; então executar a matriz de reprodução e operação no hardware real. O deploy atual é uma base de teste parcial, não uma aprovação dos critérios G0–G6.
