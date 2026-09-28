# HomeServer — plano atual de simplificação

A decisão do operador em 28/09/2026 substitui o plano P01–P13 anterior por
operação pessoal com Compose. O plano completo anterior está no
[histórico Git](https://github.com/reisguilherme/homelab-mediaserver/blob/89c84f26e4426d2f025573ce3728c5fa320f6267/docs/superpowers/plans/2026-09-28-homeserver-productization.md).
O [design atual](../specs/2026-09-28-homeserver-productization-design.md)
define o escopo.

## Entregas

| Trabalho | Resultado esperado |
|---|---|
| Compose raiz | Build/início direto, persistência fixa e APIs internas preservadas |
| .env simples | Preferências mutáveis e credenciais; sem parâmetros estruturais ou secret files |
| Bootstrap/operator | Configuração ausente preparada, sete APIs reconciliadas, IDs preservados |
| Métricas | Coleta do host em container e cards para os painéis nativos |
| Retirada de camadas | Sem CI/CD, releases, instalador, rollback e backup próprios |
| Documentação | Preparar Ubuntu/Docker/Tailscale, configurar e operar outra máquina |
| Verificação | Lint, suítes locais, Compose e primeiro boot real isolado após a simplificação |

## Verificação e ativação

Execute [os checks locais](../../development.md) e um primeiro boot isolado
com diretórios novos. Confirme preservação de credenciais literais, apply
repetível, portas, capacidade, downloads de episódios em paralelo, importação
sequencial de temporadas/episódios, qualidade, legenda, importação e
seeding. Não reutilize o resultado da suíte anterior como prova da stack nova.

O fluxo de instalação é `docker compose up -d --build`, operator apply e
restart dos consumidores. Não exige GitHub Actions, registry próprio ou
manifesto de deploy. Instalação em outra máquina, rede Tailscale, GPU e reprodução
exigem suas próprias medições.

Preserve os dados da stack existente durante a transição. Não faça exclusão,
troca de fontes protegidas, alteração de disco/roteador ou bypass do gateway
como consequência de uma atualização de infraestrutura.

Consulte [README](../../../README.md),
[instalação](../../installation.md) e
[registro histórico](../../evidence/productization-acceptance.md).
