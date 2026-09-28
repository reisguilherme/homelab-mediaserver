# Histórico de mudanças

## 2026-09-28 — HomeServer pessoal com Compose

- Operação por `docker compose up -d --build` no Compose raiz.
- Preferências mutáveis e credenciais diretamente no `.env`; paths, portas,
  UID/GID e URLs internas mantidos como defaults técnicos.
- Configuração nativa por operator temporário, com plan/apply/read-back e IDs
  preservados; bootstrap cria somente configurações ausentes.
- Métricas coletadas em container e painel web com cards dos serviços.
- Guias de Ubuntu, Docker, Tailscale, configuração, operação e diagnóstico
  reescritos para esse fluxo.
- Retirados CI/CD, automações GitHub, instalador de infraestrutura, releases,
  rollback e gerenciador de backups. Registros anteriores são históricos.

## 2026-09-28 — Melhorias do fluxo de mídia

- Downloads, seeding, banda, conexões e slots parametrizados.
- Qualidade, resolução, Dolby Vision/Atmos e pisos MiB/min do vídeo principal
  compartilhados pelo worker e Arr; sem teto estático por arquivo.
- Busca após cinco minutos lento, com avaliação concorrente de velocidade/ETA
  mantendo a fonte atual.
- Download de episódios em paralelo em janela configurável por série; importação
  preserva estritamente a ordem de temporada/episódio no Jellyfin.
- Preferência de áudio pelos metadados declarados, original com contexto Arr;
  dispensa de legenda somente para original pt-BR comprovado e desativável.
- Legenda pt-BR/inglês por release e edição compatível, com margem para créditos.
- Importação pelo worker com Completed Download Handling desabilitado e
  hardlinks habilitados, preservando seeding.
- Vínculo persistente de tags entre proxy Byparr e indexadores Prowlarr.
- Retirada de CYD/MQTT exclusivo, mantendo métricas e painel HTTP.

## Histórico da produtização anterior

O plano anterior chegou a implementar instalação fresh/adopt, units, releases
por manifesto e backup Restic. Essas camadas foram retiradas por decisão
posterior de simplificar o projeto. As provas daquela etapa permanecem no
[registro histórico](docs/evidence/productization-acceptance.md);
não são instruções para a stack atual nem comprovação de ativação dela.
